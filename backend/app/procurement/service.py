"""Supplier health: probes (admin "Test connection") and the periodic balance refresh.

Database sessions are never held open across a supplier HTTP call: read the row, close,
call the supplier, then write the result in a fresh tenant-pinned transaction.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update

from app.core.audit import audit
from app.core.db import tenant_session
from app.models import SupplierAccount, SupplierOffer, Tenant
from app.procurement import topups
from app.procurement.context import ProcurementContext
from app.suppliers.base import (
    Balance,
    FundingPolicy,
    OfferSpec,
    ProbeReport,
    Supplier,
    SupplierError,
    clean_detail,
)
from app.suppliers.registry import SupplierConfigError, build_supplier

log = logging.getLogger("app.procurement")

PLATFORM = "platform"


class SupplierNotFound(LookupError):
    pass


@dataclass(frozen=True)
class SupplierRef:
    id: uuid.UUID
    tenant_id: uuid.UUID
    code: str
    adapter: str
    env: str
    health: str
    enabled: bool = True


def make_supplier(ctx: ProcurementContext, ref: SupplierRef) -> Supplier:
    if ctx.supplier_factory is not None:
        return ctx.supplier_factory(ref)
    return build_supplier(
        code=ref.code,
        adapter=ref.adapter,
        env=ref.env,
        environ=ctx.environ,
        app_env=ctx.settings.env,
        allow_production_writes=ctx.settings.procurement_allow_production_writes,
        http=ctx.http,
    )


async def load_ref(
    ctx: ProcurementContext, tenant_id: uuid.UUID, supplier_id: uuid.UUID
) -> SupplierRef:
    """The supplier row of ``tenant_id``. Secrets in the env file belong to the platform, so a
    supplier of any other tenant is never probed with them (reported as not found)."""
    async with tenant_session(ctx.maker, tenant_id) as db:
        kind = await db.scalar(select(Tenant.kind).where(Tenant.id == tenant_id))
        if kind != PLATFORM:
            raise SupplierNotFound
        row = await db.scalar(
            select(SupplierAccount).where(
                SupplierAccount.tenant_id == tenant_id, SupplierAccount.id == supplier_id
            )
        )
        if row is None:
            raise SupplierNotFound
        return SupplierRef(
            row.id, row.tenant_id, row.code, row.adapter, row.env, row.health, row.enabled
        )


def _health_of(report: ProbeReport) -> tuple[str, str | None]:
    if report.ok:
        return "ok", None
    detail = report.errors[0][:64] if report.errors else "probe_failed"
    if report.balance is not None:  # authenticated, something secondary failed
        return "degraded", detail
    return "down", detail


async def record_health(
    ctx: ProcurementContext,
    ref: SupplierRef,
    health: str,
    detail: str | None,
    *,
    balance: Balance | None,
    probed: bool,
) -> None:
    now = datetime.now(timezone.utc)
    detail = clean_detail(detail) if detail is not None else None
    values: dict[str, object] = {"health": health, "health_detail": detail}
    if probed:
        values["last_probe_at"] = now
    if balance is not None:
        # last_balance_at (the freshness gate for NEW jobs) is stamped by ``stamp_balance``
        # only after the breaker evaluated this reading (N4)
        values["last_balance"] = balance.spendable
    async with tenant_session(ctx.maker, ref.tenant_id) as db:
        await db.execute(
            update(SupplierAccount)
            .where(SupplierAccount.tenant_id == ref.tenant_id, SupplierAccount.id == ref.id)
            .values(**values)
        )
        if health != ref.health:
            await audit(
                db,
                tenant_id=ref.tenant_id,
                action="supplier.health_changed",
                target=f"supplier:{ref.id}",
                before={"health": ref.health},
                after={"health": health, "detail": detail},
                severity="warning" if health == "down" else "info",
            )
        await db.commit()


async def stamp_balance(ctx: ProcurementContext, ref: SupplierRef) -> None:
    """Mark the balance reading as fresh; called only once the breaker evaluated it."""
    async with tenant_session(ctx.maker, ref.tenant_id) as db:
        await db.execute(
            update(SupplierAccount)
            .where(SupplierAccount.tenant_id == ref.tenant_id, SupplierAccount.id == ref.id)
            .values(last_balance_at=datetime.now(timezone.utc))
        )
        await db.commit()


async def probe(
    ctx: ProcurementContext, tenant_id: uuid.UUID, supplier_id: uuid.UUID
) -> ProbeReport:
    ref = await load_ref(ctx, tenant_id, supplier_id)
    report = await run_probe(ctx, ref)
    health, detail = _health_of(report)
    await record_health(ctx, ref, health, detail, balance=report.balance, probed=True)
    if report.balance is not None:
        # a probe's reading also becomes "fresh", but only through the breaker evaluation (N4)
        await refresh_balance(ctx, replace(ref, health=health))
    return report


async def run_probe(ctx: ProcurementContext, ref: SupplierRef) -> ProbeReport:
    now = datetime.now(timezone.utc)
    try:
        supplier = make_supplier(ctx, ref)
    except SupplierConfigError:
        log.warning("supplier %s (%s) is not configured in this process", ref.code, ref.env)
        return ProbeReport(ref.code, ref.env, ok=False, errors=("not_configured",), checked_at=now)
    try:
        return await asyncio.wait_for(supplier.probe(), ctx.settings.probe_timeout_s)
    except asyncio.TimeoutError:
        return ProbeReport(ref.code, ref.env, ok=False, errors=("probe_timeout",), checked_at=now)
    except SupplierError as exc:
        return ProbeReport(ref.code, ref.env, ok=False, errors=(exc.reason,), checked_at=now)
    finally:
        await supplier.aclose()


async def enabled_suppliers(ctx: ProcurementContext) -> list[SupplierRef]:
    """Enabled suppliers of platform tenants (``procurement_scan`` policy: SELECT only)."""
    async with tenant_session(ctx.maker, None) as db:
        rows = (
            await db.execute(
                select(SupplierAccount)
                .join(Tenant, Tenant.id == SupplierAccount.tenant_id)
                .where(SupplierAccount.enabled.is_(True), Tenant.kind == PLATFORM)
                .order_by(SupplierAccount.priority, SupplierAccount.created_at)
                .limit(100)
            )
        ).scalars()
        return [SupplierRef(r.id, r.tenant_id, r.code, r.adapter, r.env, r.health) for r in rows]


async def refresh_balance(ctx: ProcurementContext, ref: SupplierRef) -> Balance | None:
    """One balance refresh (GET only). Errors become health, never exceptions."""
    try:
        supplier = make_supplier(ctx, ref)
    except SupplierConfigError:
        await record_health(ctx, ref, "down", "not_configured", balance=None, probed=False)
        return None
    try:
        try:
            bal = await asyncio.wait_for(supplier.balance(), ctx.settings.probe_timeout_s)
        except asyncio.TimeoutError:
            await record_health(ctx, ref, "down", "timeout", balance=None, probed=False)
            return None
        except SupplierError as exc:
            health = "down" if exc.supplier_stop else "degraded"
            await record_health(ctx, ref, health, exc.reason, balance=None, probed=False)
            return None
        await record_health(ctx, ref, "ok", None, balance=bal, probed=False)
        from app.procurement.breaker import evaluate  # lazy: breaker -> service import cycle

        try:
            await evaluate(ctx, ref, bal, supplier)
            await stamp_balance(ctx, ref)
        except Exception as exc:  # a failed evaluation leaves the freshness gate stale (N4)
            log.error("breaker evaluation failed supplier=%s error=%s", ref.id, type(exc).__name__)
        return bal
    finally:
        await supplier.aclose()


async def _cost_known(supplier: Supplier, spec: OfferSpec, currency: str) -> bool:
    """The supplier itself names a redemption cost (in the balance currency) for this offer:
    the breaker can then reserve it without a configured ``cost_estimate``."""
    fn = getattr(supplier, "redemption_cost", None)
    if fn is None:
        return False
    try:
        money = await asyncio.wait_for(fn(spec), 10)
    except Exception:
        return False
    return money is not None and money.currency == currency


async def _mask_clash(ctx: ProcurementContext, tenant_id: uuid.UUID, mask: str | None) -> bool:
    from app.procurement.funding import funding_mask_clash

    async with tenant_session(ctx.maker, tenant_id) as db:
        return await funding_mask_clash(db, tenant_id, mask)


def offer_spec(
    kind: str,
    params: Any,
    cost: Decimal | None,
    currency: str | None,
    policy: FundingPolicy | None,
) -> OfferSpec:
    """An offer row as the adapter sees it: its parameters, its configured USD cost (a direct
    plan quoted in another currency is budgeted from it) and, for a direct offer, the funding
    policy (ADR-048)."""
    return OfferSpec(
        kind=kind,  # type: ignore[arg-type]
        params=dict(params or {}),
        cost_usd=cost if currency == "USD" else None,
        funding=policy if kind == "direct" else None,
    )


async def refresh_offers(ctx: ProcurementContext, ref: SupplierRef) -> int:
    """Availability check of every enabled offer of one supplier (GET only). The storefront shows
    an upstream variant in stock only while its last check passed (review-v12 §4)."""
    async with tenant_session(ctx.maker, ref.tenant_id) as db:
        offers = (
            await db.execute(
                select(
                    SupplierOffer.id,
                    SupplierOffer.kind,
                    SupplierOffer.params,
                    SupplierOffer.cost_estimate,
                    SupplierOffer.currency,
                ).where(
                    SupplierOffer.tenant_id == ref.tenant_id,
                    SupplierOffer.supplier_id == ref.id,
                    SupplierOffer.enabled.is_(True),
                )
            )
        ).all()
        # ADR-048: a direct plan is in stock while a card, or an allowed top-up, can pay it
        policy = await topups.policy(db, ref.tenant_id, ref.id, datetime.now(timezone.utc))
    if not offers:
        return 0
    try:
        supplier = make_supplier(ctx, ref)
    except SupplierConfigError:
        return 0
    async with tenant_session(ctx.maker, ref.tenant_id) as db:
        currency = await db.scalar(
            select(SupplierAccount.currency).where(SupplierAccount.id == ref.id)
        )
    results: dict[uuid.UUID, bool] = {}
    known: dict[uuid.UUID, bool] = {}
    try:
        for oid, kind, params, cost, cur in offers:
            spec = offer_spec(kind, params, cost, cur, policy)
            try:
                av = await asyncio.wait_for(
                    supplier.availability(spec),
                    ctx.settings.probe_timeout_s,
                )
                results[oid] = av.available is True
                if (
                    results[oid]
                    and kind == "direct"
                    and await _mask_clash(ctx, ref.tenant_id, av.funding_mask)
                ):
                    results[oid] = False
                known[oid] = await _cost_known(supplier, spec, currency or "USD")
            except Exception:  # an unknown answer is "not available"
                results[oid] = False
    finally:
        await supplier.aclose()
    now = datetime.now(timezone.utc)
    async with tenant_session(ctx.maker, ref.tenant_id) as db:
        for oid, ok in results.items():
            await db.execute(
                update(SupplierOffer)
                .where(SupplierOffer.tenant_id == ref.tenant_id, SupplierOffer.id == oid)
                .values(
                    last_check_ok=ok,
                    last_check_at=now,
                    **({"cost_known": known[oid]} if oid in known else {}),
                )
            )
        await db.commit()
    return len(results)


async def check_offer(
    ctx: ProcurementContext, tenant_id: uuid.UUID, offer_id: uuid.UUID
) -> dict[str, object]:
    """Staff "check availability" of ONE offer (GET only), enabled or not, so a source can be
    tested before it is switched on. Stores ``last_check_ok`` like the periodic refresh."""
    async with tenant_session(ctx.maker, tenant_id) as db:
        kind = await db.scalar(select(Tenant.kind).where(Tenant.id == tenant_id))
        if kind != PLATFORM:
            raise SupplierNotFound
        row = (
            await db.execute(
                select(
                    SupplierOffer.kind,
                    SupplierOffer.params,
                    SupplierOffer.cost_estimate,
                    SupplierOffer.currency.label("offer_currency"),
                    SupplierAccount.id,
                    SupplierAccount.code,
                    SupplierAccount.adapter,
                    SupplierAccount.env,
                    SupplierAccount.health,
                    SupplierAccount.currency,
                )
                .join(SupplierAccount, SupplierAccount.id == SupplierOffer.supplier_id)
                .where(SupplierOffer.tenant_id == tenant_id, SupplierOffer.id == offer_id)
            )
        ).first()
        if row is not None:
            policy = await topups.policy(db, tenant_id, row.id, datetime.now(timezone.utc))
    if row is None:
        raise SupplierNotFound
    ref = SupplierRef(row.id, tenant_id, row.code, row.adapter, row.env, row.health)
    spec = offer_spec(row.kind, row.params, row.cost_estimate, row.offer_currency, policy)
    ok, reason = False, "not_configured"
    cost_known: bool | None = None
    try:
        supplier = make_supplier(ctx, ref)
    except SupplierConfigError:
        supplier = None
    if supplier is not None:
        try:
            av = await asyncio.wait_for(
                supplier.availability(spec),
                ctx.settings.probe_timeout_s,
            )
            ok, reason = av.available is True, (None if av.available is True else av.reason)
            if ok and row.kind == "direct" and await _mask_clash(ctx, tenant_id, av.funding_mask):
                ok, reason = False, "funding_card_invalid"
            cost_known = await _cost_known(supplier, spec, row.currency or "USD")
        except asyncio.TimeoutError:
            reason = "timeout"
        except SupplierError as exc:
            reason = exc.reason
        except Exception:  # an unknown answer is "not available"
            reason = "check_failed"
        finally:
            await supplier.aclose()
    now = datetime.now(timezone.utc)
    async with tenant_session(ctx.maker, tenant_id) as db:
        await db.execute(
            update(SupplierOffer)
            .where(SupplierOffer.tenant_id == tenant_id, SupplierOffer.id == offer_id)
            .values(
                last_check_ok=ok,
                last_check_at=now,
                **({"cost_known": cost_known} if cost_known is not None else {}),
            )
        )
        await db.commit()
    return {
        "available": ok,
        "reason": clean_detail(reason) if reason else None,
        "checked_at": now.isoformat(),
    }


async def refresh_all(ctx: ProcurementContext) -> int:
    refs = await enabled_suppliers(ctx)
    for ref in refs:
        try:
            if await refresh_balance(ctx, ref) is not None:
                await refresh_offers(ctx, ref)
        except Exception:  # one broken supplier must not stop the others
            log.exception("balance refresh failed for supplier %s", ref.id)
    return len(refs)


async def backfill_ledgers(ctx: ProcurementContext) -> int:
    """``GET /balance-logs`` backfill of every enabled supplier into the cost ledger."""
    from app.procurement.ledger import backfill

    n = 0
    for ref in await enabled_suppliers(ctx):
        try:
            async with tenant_session(ctx.maker, ref.tenant_id) as db:
                cur = await db.scalar(
                    select(SupplierAccount.currency).where(SupplierAccount.id == ref.id)
                )
            n += await backfill(ctx, ref, cur or "USD")
        except Exception as exc:  # one broken supplier must not stop the others
            log.error("ledger backfill failed supplier=%s error=%s", ref.id, type(exc).__name__)
    return n


async def run_loop(ctx: ProcurementContext, stop: asyncio.Event) -> None:
    last_ledger = float("-inf")
    while not stop.is_set():
        try:
            await refresh_all(ctx)
        except Exception:
            log.exception("supplier refresh pass failed")
        if time.monotonic() - last_ledger >= ctx.settings.ledger_interval_s - 1:
            last_ledger = time.monotonic()
            try:
                await backfill_ledgers(ctx)
            except Exception:
                log.exception("ledger backfill pass failed")
        try:
            await asyncio.wait_for(stop.wait(), ctx.settings.balance_interval_s)
        except asyncio.TimeoutError:
            continue


async def _zovo_supplier(
    ctx: ProcurementContext, tenant_id: uuid.UUID
) -> tuple[SupplierRef, Supplier]:
    """The first enabled ZovoCard supplier of the platform tenant, built (never probed here)."""
    async with tenant_session(ctx.maker, tenant_id) as db:
        kind = await db.scalar(select(Tenant.kind).where(Tenant.id == tenant_id))
        if kind != PLATFORM:
            raise SupplierNotFound
        row = await db.scalar(
            select(SupplierAccount)
            .where(
                SupplierAccount.tenant_id == tenant_id,
                SupplierAccount.enabled.is_(True),
                SupplierAccount.adapter == "zovocard",
            )
            .order_by(SupplierAccount.priority, SupplierAccount.created_at)
            .limit(1)
        )
        if row is None:
            raise SupplierNotFound
        ref = SupplierRef(row.id, row.tenant_id, row.code, row.adapter, row.env, row.health)
    try:
        return ref, make_supplier(ctx, ref)
    except SupplierConfigError:
        raise SupplierNotFound from None


async def payment_regions(
    ctx: ProcurementContext, tenant_id: uuid.UUID, product: str
) -> list[dict[str, str]]:
    """The vendor's current payment regions for ``product`` (GET only, cached by the adapter).
    An unreachable vendor raises :class:`SupplierError`; the caller shows "unavailable"."""
    _ref, supplier = await _zovo_supplier(ctx, tenant_id)
    try:
        fn = getattr(supplier, "payment_regions", None)
        if fn is None:
            return []
        pairs = await asyncio.wait_for(fn(product), ctx.settings.probe_timeout_s)
    finally:
        await supplier.aclose()
    return [{"country": c, "currency": cur} for c, cur in pairs]
