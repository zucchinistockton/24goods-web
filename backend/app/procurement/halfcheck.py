"""Half-price eligibility checks, procurement side (issue #27, review-v12 §20.4).

The API sealed the buyer's account session into ``half_price_checks`` (AAD = tenant + check id) and
asks here to read eligibility. This is the ONLY place the session is opened. It lives in memory for
the one read, the row's ``blob`` is nulled afterwards (also on every error path), and nothing about
the session is logged, stored in ``detail`` or returned.

Methods (owner setting ``half_price_check``, stored on the row at creation):

* ``vendor_preflight``: the supplier's preflight (``ZovoCardSupplier.half_price_check``).
* ``chatgpt_direct``: :mod:`app.procurement.chatgpt_check` (plain read of the buyer's own account
  check; rate limited per buyer and globally; ``unavailable`` when ChatGPT refuses).
* ``manual``: never reaches this module (staff decide in the console).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import undefer

from app.core.db import tenant_session
from app.core.ratelimit import hit
from app.models import HalfPriceCheck, SupplierAccount, SupplierOffer, Tenant
from app.procurement import chatgpt_check, service
from app.procurement.context import ProcurementContext
from app.suppliers.base import SupplierError
from app.suppliers.direct_credential import CredentialError, validate_credential
from app.suppliers.registry import SupplierConfigError

log = logging.getLogger("app.procurement.halfcheck")

MAX_AGE = timedelta(minutes=15)  # a sealed session older than this is wiped, never used
PER_BUYER = (3, 600)  # chatgpt_direct reads per buyer per 10 minutes
GLOBAL = (60, 3600)  # chatgpt_direct reads per hour PER TENANT (the egress IP is shared)


@dataclass(frozen=True)
class Outcome:
    status: str  # passed | ineligible | unavailable | session_invalid
    reason: str = ""
    detail: dict[str, Any] | None = None
    account_mask: str | None = None


async def _finish(
    ctx: ProcurementContext, tenant_id: uuid.UUID, check_id: uuid.UUID, out: Outcome
) -> None:
    """Record the outcome and wipe the sealed session in the same statement."""
    async with tenant_session(ctx.maker, tenant_id) as db:
        values: dict[str, Any] = {
            "status": out.status,
            "reason": out.reason[:48] or None,
            "detail": out.detail,
            "blob": None,
            "decided_at": datetime.now(timezone.utc),
        }
        if out.account_mask:
            # keep the mask derived from the sealed session itself (the API binds the order's
            # session to it); the vendor's own mask only fills a row that has none
            values["email_masked"] = func.coalesce(
                HalfPriceCheck.__table__.c.email_masked, out.account_mask
            )
        await db.execute(
            update(HalfPriceCheck.__table__)
            .where(
                HalfPriceCheck.__table__.c.id == check_id,
                HalfPriceCheck.__table__.c.tenant_id == tenant_id,
            )
            .values(**values)
        )
        await db.commit()


async def _source(
    ctx: ProcurementContext, tenant_id: uuid.UUID, row: Any
) -> tuple[service.SupplierRef, dict[str, Any]] | None:
    """The primary enabled source of the target (same rule as the API's ``requirement``)."""
    async with tenant_session(ctx.maker, tenant_id) as db:
        match = (
            SupplierOffer.variant_id == row.variant_id
            if row.variant_id is not None
            else SupplierOffer.variant_id.is_(None)
        )
        rows = (
            await db.execute(
                select(SupplierOffer.params, SupplierAccount)
                .join(SupplierAccount, SupplierAccount.id == SupplierOffer.supplier_id)
                .where(
                    SupplierOffer.tenant_id == tenant_id,
                    SupplierOffer.product_id == row.product_id,
                    match,
                    SupplierOffer.enabled.is_(True),
                    SupplierOffer.kind.in_(("cdk", "direct")),
                    SupplierAccount.enabled.is_(True),
                    SupplierAccount.adapter == "zovocard",
                )
                .order_by(SupplierOffer.priority, SupplierOffer.created_at)
            )
        ).all()
        # the source must still sell the plan / country the check was made for
        r = next(
            (
                x
                for x in rows
                if (x.params or {}).get("plan") == row.plan
                and (x.params or {}).get("payment_country") == row.country
            ),
            None,
        )
        kind = await db.scalar(select(Tenant.kind).where(Tenant.id == tenant_id))
    if r is None or kind != service.PLATFORM:
        return None
    params, s = r
    ref = service.SupplierRef(s.id, s.tenant_id, s.code, s.adapter, s.env, s.health)
    return ref, dict(params or {})


def _quote_view(quote: tuple[int, str, int] | None) -> dict[str, Any] | None:
    if quote is None:
        return None
    minor, currency, exp = quote
    return {"amount_minor": minor, "currency": currency, "exponent": exp}


async def _via_preflight(
    ctx: ProcurementContext, tenant_id: uuid.UUID, row: Any, credential: str
) -> Outcome:
    found = await _source(ctx, tenant_id, row)
    if found is None:
        return Outcome("unavailable", "no_source")
    ref, params = found
    try:
        supplier = service.make_supplier(ctx, ref)
    except SupplierConfigError:
        return Outcome("unavailable", "not_configured")
    try:
        fn = getattr(supplier, "half_price_check", None)
        if fn is None:
            return Outcome("unavailable", "not_supported")
        try:
            res = await asyncio.wait_for(
                fn(
                    row.vendor_product,
                    row.plan,
                    params.get("payment_country"),
                    params.get("payment_currency"),
                    credential,
                ),
                ctx.settings.probe_timeout_s,
            )
        except asyncio.TimeoutError:
            return Outcome("unavailable", "timeout")
        except SupplierError as exc:
            return Outcome("unavailable", exc.reason)
    finally:
        await supplier.aclose()
    detail = {
        "quote": _quote_view(res.quote),
        "current_plan": res.current_plan,
        "promotions": list(res.promotions),
    }
    if res.status == "eligible":
        return Outcome("passed", "", detail, res.account_mask)
    if res.status == "ineligible":
        return Outcome("ineligible", res.reason or "not_eligible", detail, res.account_mask)
    return Outcome(res.status, res.reason, None)


async def _via_chatgpt(
    ctx: ProcurementContext, tenant_id: uuid.UUID, row: Any, credential: str
) -> Outcome:
    if row.vendor_product != "gpt":
        return Outcome("unavailable", "gpt_only")
    ok, _ = await hit(ctx.redis, "proc:halfcheck-user", f"{tenant_id}:{row.user_id}", *PER_BUYER)
    if not ok:
        return Outcome("unavailable", "rate_limited")
    try:
        token, account_id = chatgpt_check.session_fields(credential)
    except ValueError:  # the sealed document was not JSON: cannot happen after validation
        return Outcome("unavailable", "unreadable_session")
    # the per-tenant budget is spent only by a read that really goes to ChatGPT (a session that
    # fails locally costs nothing), and one tenant cannot exhaust another's
    ok, _ = await hit(ctx.redis, "proc:halfcheck-global", str(tenant_id), *GLOBAL)
    if not ok:
        await _signal_limit(ctx, tenant_id)
        return Outcome("unavailable", "rate_limited")
    try:
        parsed = await chatgpt_check.read_eligibility(ctx.chatgpt_http, token, account_id)
    except chatgpt_check.CheckUnavailable as exc:
        if exc.reason == "session_expired":
            return Outcome("session_invalid", "session_expired")
        return Outcome("unavailable", exc.reason)
    eligible, why = chatgpt_check.half_eligible(parsed)
    detail = {
        "plan_type": parsed["plan_type"],
        "campaigns": [c["key"] for c in parsed["campaigns"]],
    }
    return Outcome("passed" if eligible else "ineligible", why, detail)


async def _signal_limit(ctx: ProcurementContext, tenant_id: uuid.UUID) -> None:
    """Staff signal (at most one per hour): the ChatGPT read budget is spent, so buyers are being
    refused; switch the check method to ``manual`` / ``vendor_preflight`` in Regions."""
    first, _ = await hit(ctx.redis, "proc:halfcheck-signal", str(tenant_id), 1, 3600)
    if not first:
        return
    from app.core.audit import audit

    async with tenant_session(ctx.maker, tenant_id) as db:
        await audit(
            db,
            tenant_id=tenant_id,
            action="half_check.limit_hit",
            target="tenant:procurement_options",
            detail={"method": "chatgpt_direct", "per_hour": GLOBAL[0], "hint": "switch_to_manual"},
            severity="warning",
        )
        await db.commit()


async def run_check(ctx: ProcurementContext, tenant_id: uuid.UUID, check_id: uuid.UUID) -> Outcome:
    """Read the check, open the session, evaluate, record, wipe. Never raises for a vendor or
    network failure (it becomes ``unavailable``)."""
    async with tenant_session(ctx.maker, tenant_id) as db:
        row = (
            await db.execute(
                select(HalfPriceCheck)
                .options(undefer(HalfPriceCheck.blob))
                .where(HalfPriceCheck.id == check_id, HalfPriceCheck.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise LookupError("check")
        blob = None
        if row.status == "pending" and row.blob is not None:
            blob = bytes(row.blob)
        method = row.method
        age = datetime.now(timezone.utc) - row.created_at
        db.expunge(row)
    if blob is None or method == "manual":
        return Outcome(row.status, row.reason or "")
    out = Outcome("unavailable", "check_failed")
    credential: str | None = None
    try:
        if age > MAX_AGE:
            out = Outcome("unavailable", "check_expired")
        elif ctx.cred_opener is None:
            out = Outcome("unavailable", "not_configured")
        else:
            try:
                credential = ctx.cred_opener.open(
                    blob, tenant_id=str(tenant_id), order_id=str(check_id)
                )
                validate_credential(row.vendor_product, credential)
            except CredentialError:
                out = Outcome("session_invalid", "session_rejected")
            except Exception:  # an unreadable blob (wrong key / tampered): never echo detail
                out = Outcome("unavailable", "unreadable_session")
            else:
                if method == "chatgpt_direct":
                    out = await _via_chatgpt(ctx, tenant_id, row, credential)
                else:
                    out = await _via_preflight(ctx, tenant_id, row, credential)
    except Exception as exc:  # any failure is "unavailable"; the exception text is not logged
        log.warning("half-check check=%s failed error=%s", check_id, type(exc).__name__)
        out = Outcome("unavailable", "check_failed")
    finally:
        credential = None
        blob = None
        await _finish(ctx, tenant_id, check_id, out)
    log.info(
        "half-check check=%s method=%s outcome=%s reason=%s",
        check_id,
        method,
        out.status,
        out.reason,
    )
    return out


async def wipe_stale(ctx: ProcurementContext) -> int:
    """Sweeper: null every sealed check session older than the age bound (procurement died between
    the API's insert and the read) and drop unused checks older than two days."""
    n = 0
    async with tenant_session(ctx.maker, None) as db:
        res = await db.execute(
            text(
                "UPDATE half_price_checks SET blob = NULL, "
                "status = CASE WHEN status = 'pending' THEN 'unavailable' ELSE status END, "
                "reason = CASE WHEN status = 'pending' THEN 'check_expired' ELSE reason END "
                "WHERE blob IS NOT NULL AND created_at < now() - make_interval(secs => :age)"
            ),
            {"age": MAX_AGE.total_seconds()},
        )
        n += int(res.rowcount or 0)  # type: ignore[attr-defined]
        res = await db.execute(
            text(
                "DELETE FROM half_price_checks WHERE order_id IS NULL "
                "AND created_at < now() - interval '2 days'"
            )
        )
        n += int(res.rowcount or 0)  # type: ignore[attr-defined]
        await db.commit()
    return n
