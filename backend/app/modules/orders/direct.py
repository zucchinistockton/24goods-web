"""``upstream_direct`` checkout side (review-v12 §18): the buyer's account session.

The session is a live credential for the buyer's account. Here it is validated (size, charset, shape
for the vendor product the target's sources buy), sealed AT ONCE with the procurement PUBLIC key and
inserted into ``procurement_credentials``; the API role can write that table and read only its
non-secret columns, so nothing in this process (or a database read by it) can ever open it again.
``orders.buyer_input`` (plaintext, shown to staff) is NOT used for it and stays empty.

Nothing in this module logs, audits or returns the session; ``DirectSeal`` hides it from ``repr``.
"""

from __future__ import annotations

import base64
import logging
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.credvault import CredentialSealer
from app.core.errors import Conflict, InvalidState, NotFound, Unavailable, Validation
from app.core.ratelimit import enforce
from app.models import HalfPriceCheck, Order, ProcurementCredential, ProcurementJob, SupplierOffer
from app.modules.catalog import upstream
from app.modules.common.modes import MAX_DIRECT_QTY
from app.suppliers import direct_progress as dp
from app.suppliers.direct_credential import CredentialError, validate_credential

log = logging.getLogger("app.orders.direct")


@dataclass(frozen=True)
class DirectSeal:
    """A validated session, not yet sealed. ``text`` is the credential: repr-hidden."""

    product: str
    email_masked: str | None
    text: str = field(repr=False)
    # the account pre-check before payment (review-v12 §24): ``passed`` or not yet run
    preflight: str | None = None
    # the buyer agreed that support may see the session if the top-up fails (review-v12 §26)
    share: bool = False
    # the pre-check found an UPGRADE of the account's running plan (owner 2026-11-22): the
    # vendor's quote {usd, amount, currency, from_plan}; the order is priced from it
    upgrade: dict[str, str] | None = None


STALL_MARGIN = timedelta(minutes=30)  # past the 6 h ceiling before checkout is paused
MAX_AGE = timedelta(hours=6)


async def procurement_stalled(db: AsyncSession, redis: Any) -> bool:
    """The oldest sealed session is older than its ceiling plus a margin, or the API's own backstop
    purge found overdue ones lately (its marker in Redis)."""
    if redis is not None:
        try:
            if await redis.get("proc:sessions-overdue"):
                return True
        except Exception as exc:  # Redis down: the database check still runs
            log.warning("sessions-overdue marker unreadable error=%s", type(exc).__name__)
    oldest = await db.scalar(select(func.min(ProcurementCredential.created_at)))
    return oldest is not None and datetime.now(timezone.utc) - oldest > MAX_AGE + STALL_MARGIN


async def prepare(
    db: AsyncSession,
    sealer: CredentialSealer | None,
    redis: Any = None,
    prod: bool = False,
    *,
    product_id: uuid.UUID,
    variant_id: uuid.UUID | None,
    qty: int,
    session: str | None,
    share: bool = False,
) -> DirectSeal:
    """Everything that can be refused BEFORE an order exists. Raises specific error codes:
    ``direct_qty_one``, ``session_required``, ``direct_unavailable`` (no key configured),
    ``out_of_stock`` (no usable source), and ``session_<reason>`` for a credential that cannot
    work (``too_large``, ``too_short``, ``invalid_format``, ``not_json``, ``access_token_only``,
    ``missing_session_token``, ``missing_access_token``, ``malformed_access_token``, ``expired``,
    ``missing_account``, ``account_mismatch``)."""
    if qty != MAX_DIRECT_QTY:
        raise Validation("This product is bought one account at a time", code="direct_qty_one")
    if not (session or "").strip():
        raise Validation("The account session is required", code="session_required")
    if sealer is None:
        raise Unavailable("This product cannot be bought right now", code="direct_unavailable")
    if await procurement_stalled(db, redis):
        # procurement is evidently not cleaning up sealed sessions: keep no new ones (S-M1c)
        raise Unavailable("This product cannot be bought right now", code="direct_unavailable")
    vendor_product = await upstream.direct_target_product(
        db, product_id, variant_id, exclude_sandbox=prod
    )
    try:
        checked = validate_credential(vendor_product, session)
    except CredentialError as exc:
        raise Validation("The account session cannot be used", code=f"session_{exc.code}") from None
    return DirectSeal(vendor_product, checked.email_masked, checked.canonical, share=share)


PREFLIGHT_LIMIT = (12, 600)  # account pre-checks per buyer per 10 minutes (each is a vendor read)
# what a buyer is told: a reason KEY only (the vendor's text and the rest of the account state stay
# in procurement). The buyer pasted this account's session a moment ago, so its own plan state is
# no secret to them; the per-buyer budget bounds its use as an oracle on other sessions.
PREFLIGHT_BLOCKS = frozenset(
    {"session_invalid", "plan_active", "purchase_not_yet", "account_blocked", "upgrade_unavailable"}
)
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.+Z-]{4,32}$")
_MONEY_RE = re.compile(r"^\d{1,7}(\.\d{1,4})?$")


def _upgrade_of(out: dict[str, Any] | None) -> dict[str, str] | None:
    """The procurement answer's upgrade quote, strictly shaped (amounts as decimal strings)."""
    up = (out or {}).get("upgrade")
    if not isinstance(up, dict):
        return None
    usd, amount, cur = str(up.get("usd") or ""), str(up.get("amount") or ""), up.get("currency")
    if not (_MONEY_RE.fullmatch(usd) and _MONEY_RE.fullmatch(amount)):
        return None
    if not (isinstance(cur, str) and re.fullmatch(r"[A-Z]{3}", cur)) or Decimal(usd) <= 0:
        return None
    cp = up.get("current_plan")
    return {
        "usd": usd,
        "amount": amount,
        "currency": cur,
        "from_plan": cp[:40] if isinstance(cp, str) else "",
    }


async def preflight(
    db: AsyncSession,
    sealer: CredentialSealer,
    procurement: httpx.AsyncClient | None,
    token: str,
    redis: Any,
    *,
    user_id: uuid.UUID,
    user_role: str,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    variant_id: uuid.UUID | None,
    seal: DirectSeal,
    ip: str | None,
    order_id: uuid.UUID | None = None,
) -> DirectSeal:
    """The account pre-check BEFORE payment (review-v12 §24), for every direct order: the vendor's
    preflight, read by procurement with the session sealed for this one call (AAD
    ``preflight:<nonce>``; nothing is stored). Returns the seal marked ``passed``; otherwise raises
    ``preflight_<reason>`` (422: ``session_invalid`` / ``plan_active`` / ``purchase_not_yet`` /
    ``account_blocked``) or ``preflight_unavailable`` (503: procurement or the vendor could not
    answer; fail closed, the buyer tries again). A refusal is audited (reason only)."""
    if redis is not None:
        await enforce(redis, "order:preflight", str(user_id), *PREFLIGHT_LIMIT)
    nonce = uuid.uuid4()
    blob = sealer.seal(seal.text, tenant_id=str(tenant_id), order_id=f"preflight:{nonce}")
    out: dict[str, Any] | None = None
    if procurement is not None and len(token) >= 32:
        try:
            resp = await procurement.post(
                "/internal/direct-preflight",
                json={
                    "tenant_id": str(tenant_id),
                    "product_id": str(product_id),
                    "variant_id": str(variant_id) if variant_id else None,
                    "nonce": str(nonce),
                    "blob": base64.b64encode(blob).decode(),
                },
                headers={"X-Internal-Token": token},
                timeout=40.0,
            )
            if resp.status_code == 200 and isinstance(resp.json(), dict):
                out = resp.json()
        except (httpx.HTTPError, ValueError):
            out = None
    del blob
    status = str((out or {}).get("status") or "unavailable")
    reason = str((out or {}).get("reason") or "")
    if status == "passed":
        return replace(seal, preflight="passed", upgrade=_upgrade_of(out))
    if status == "blocked":
        reason = reason if reason in PREFLIGHT_BLOCKS else "account_blocked"
    else:
        reason = "unavailable"
    await audit(
        db,
        tenant_id=tenant_id,
        action="order.direct_preflight_refused",
        actor_id=user_id,
        actor_role=user_role,
        target=f"order:{order_id}" if order_id else f"product:{product_id}",
        detail={
            "reason": reason,
            "vendor_reason": str((out or {}).get("reason") or "")[:48] or None,
            "variant_id": str(variant_id) if variant_id else None,
        },  # never the session
        ip=ip,
    )
    await db.commit()
    plan = (out or {}).get("current_plan")
    extra: dict[str, Any] | None = None
    if status == "blocked":
        # owner 2026-11-22: tell the buyer WHICH account was checked, what it holds and from when
        # it can buy again (a wrong session pasted shows at once); the buyer pasted it a moment ago
        buy_from = str((out or {}).get("can_purchase_at") or "")
        acct = (out or {}).get("account")
        extra = {
            k: v
            for k, v in {
                "current_plan": str(plan)[:40] if plan else None,
                "can_purchase_at": buy_from if _ISO_RE.fullmatch(buy_from) else None,
                "account": str(acct)[:80] if isinstance(acct, str) and "***" in acct else None,
            }.items()
            if v
        } or None
    if reason == "unavailable":
        raise Unavailable(
            "The account check is not available right now", code="preflight_unavailable"
        )
    raise Validation(
        "This account cannot take the plan now", code=f"preflight_{reason}", extra=extra
    )


async def store(db: AsyncSession, sealer: CredentialSealer, order: Order, seal: DirectSeal) -> None:
    """Seal and insert (Core insert, no RETURNING: the API cannot read ``blob`` back)."""
    blob = sealer.seal(seal.text, tenant_id=str(order.tenant_id), order_id=str(order.id))
    try:
        async with db.begin_nested():
            await db.execute(
                insert(ProcurementCredential.__table__).values(
                    id=uuid.uuid4(),
                    tenant_id=order.tenant_id,
                    order_id=order.id,
                    product=seal.product,
                    kid=sealer.active_kid,
                    blob=blob,
                    email_masked=seal.email_masked,
                    share_with_support=seal.share,
                )
            )
    except IntegrityError:
        raise Conflict(
            "A session is already stored for this order", code="session_exists"
        ) from None


def info_view(
    order: Order,
    job_mask: str | None,
    cred_mask: str | None,
    plan: str | None,
    state: str | None,
    waiting: bool = False,
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """What the buyer may see about a direct order: the masked account and the plan, never more.
    ``waiting_for_supplier`` is the coarse status of a job that has no funding card yet (issue
    #29): no card id, no reason, no internal detail. ``progress`` (issue #30) is the coarse step
    list from :func:`app.suppliers.direct_progress.buyer_view`: step keys, percent, outcome and a
    reason KEY only, never a vendor code or message."""
    return {
        "order_id": str(order.id),
        "account": job_mask or cred_mask,
        "plan": plan,
        "state": state,
        "waiting_for_supplier": waiting,
        "progress": progress,
    }


async def owner_info(db: AsyncSession, order: Order) -> dict[str, Any]:
    if order.delivery_mode != "upstream_direct":
        raise NotFound("Order not found")
    cred_mask = await db.scalar(
        select(ProcurementCredential.email_masked).where(
            ProcurementCredential.tenant_id == order.tenant_id,
            ProcurementCredential.order_id == order.id,
        )
    )
    row = (
        await db.execute(
            select(
                ProcurementJob.account_mask,
                ProcurementJob.state,
                ProcurementJob.error_code,
                ProcurementJob.upstream_ref,
                ProcurementJob.upstream_status,
                ProcurementJob.first_sent_at,
                ProcurementJob.card_wait_since,
                ProcurementJob.progress,
                SupplierOffer.params,
            )
            .outerjoin(SupplierOffer, SupplierOffer.id == ProcurementJob.offer_id)
            .where(ProcurementJob.tenant_id == order.tenant_id, ProcurementJob.order_id == order.id)
            .order_by(ProcurementJob.created_at)
            .limit(1)
        )
    ).first()
    params = row.params if row is not None and row.params else None
    if params is None:  # no job yet: the plan of the target's first enabled direct source
        params = await db.scalar(
            select(SupplierOffer.params)
            .where(
                SupplierOffer.tenant_id == order.tenant_id,
                SupplierOffer.product_id == order.product_id,
                SupplierOffer.variant_id == order.variant_id
                if order.variant_id is not None
                else SupplierOffer.variant_id.is_(None),
                SupplierOffer.kind == "direct",
                SupplierOffer.enabled.is_(True),
            )
            .order_by(SupplierOffer.priority, SupplierOffer.created_at)
            .limit(1)
        )
    plan = f"{params.get('product')}/{params.get('plan')}" if params else None
    waiting = bool(row and row.state == "queued" and row.error_code == "waiting_for_card")
    return info_view(
        order,
        row.account_mask if row else None,
        cred_mask,
        plan,
        row.state if row else None,
        waiting,
        await _progress(db, order, row, params),
    )


async def _progress(db: AsyncSession, order: Order, row: Any, params: Any) -> dict[str, Any]:
    """The buyer's step list. Our own steps come from our rows, the vendor steps from what the
    engine recorded; the mapping never raises and only ever shows a coarse reason key."""
    half = dp.is_half(params)
    done = (row.progress or {}) if row is not None else {}
    job = _JobFacts(row) if row is not None else None
    p = (
        dp.from_job(job, half=half, order_refunded=order.status == "refunded")
        if job is not None
        else dp.map_progress(job_state=None, half=half, order_refunded=order.status == "refunded")
    )
    checked = await db.scalar(
        select(HalfPriceCheck.decided_at).where(
            HalfPriceCheck.tenant_id == order.tenant_id, HalfPriceCheck.order_id == order.id
        )
    )
    times: dict[str, Any] = dict(done.get("reached") or {})
    for key, value in (
        ("paid", order.paid_at),
        ("half_check", checked),
        ("waiting_supplier", row.card_wait_since if row else None),
        ("sent", row.first_sent_at if row else None),
    ):
        if value is not None:
            times[key] = value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    waited = bool(row and row.card_wait_since)
    return dp.buyer_view(p, half=half, waited=waited, at=times)


class _JobFacts:
    """The few job columns the mapping reads (a result row of the owner query)."""

    def __init__(self, row: Any) -> None:
        self.state, self.error_code = row.state, row.error_code
        self.upstream_ref, self.upstream_status = row.upstream_ref, row.upstream_status
        self.first_sent_at, self.progress = row.first_sent_at, row.progress


async def resupply(
    db: AsyncSession,
    sealer: CredentialSealer | None,
    order: Order,
    session: str | None,
    *,
    actor_id: uuid.UUID,
    ip: str | None,
    procurement: httpx.AsyncClient | None = None,
    token: str = "",
    redis: Any = None,
    share: bool = False,
) -> None:
    """The buyer replaces a session that expired or was rejected (the stored one is gone: the
    sweeper deletes it at a terminal state or after its maximum age). Only for the buyer's own
    ``paid`` / ``manual_review`` direct order that has no stored session; staff then retry the
    job. Same validation and sealing as at checkout."""
    if order.delivery_mode != "upstream_direct":
        raise NotFound("Order not found")
    if order.status not in ("paid", "manual_review"):
        raise InvalidState("This order does not need a new session", code="session_not_needed")
    if not (session or "").strip():
        raise Validation("The account session is required", code="session_required")
    if sealer is None:
        raise Unavailable("This product cannot be bought right now", code="direct_unavailable")
    vendor_product = await db.scalar(
        select(SupplierOffer.params["product"].astext)
        .join(ProcurementJob, ProcurementJob.offer_id == SupplierOffer.id)
        .where(ProcurementJob.tenant_id == order.tenant_id, ProcurementJob.order_id == order.id)
        .limit(1)
    )
    if vendor_product is None:
        vendor_product = await upstream.direct_target_product(
            db, order.product_id, order.variant_id
        )
    try:
        checked = validate_credential(vendor_product, session)
    except CredentialError as exc:
        raise Validation("The account session cannot be used", code=f"session_{exc.code}") from None
    first_mask = await db.scalar(
        select(ProcurementJob.account_mask)
        .where(
            ProcurementJob.tenant_id == order.tenant_id,
            ProcurementJob.order_id == order.id,
            ProcurementJob.account_mask.is_not(None),
        )
        .limit(1)
    )
    if first_mask and checked.email_masked and first_mask != checked.email_masked:
        # the subscription goes to the account the order was first checked against
        raise Conflict(
            "This session belongs to another account than the first one",
            code="session_other_account",
        )
    seal = DirectSeal(vendor_product, checked.email_masked, checked.canonical, share=share)
    if order.product_id is not None:
        # the new session gets the same account pre-check as at checkout (review-v12 §24)
        await preflight(
            db,
            sealer,
            procurement,
            token,
            redis,
            user_id=actor_id,
            user_role="buyer",
            tenant_id=order.tenant_id,
            product_id=order.product_id,
            variant_id=order.variant_id,
            seal=seal,
            ip=ip,
            order_id=order.id,
        )
    await store(db, sealer, order, seal)
    await audit(
        db,
        tenant_id=order.tenant_id,
        action="order.direct_session_resupplied",
        actor_id=actor_id,
        actor_role="buyer",
        target=f"order:{order.id}",
        detail={"product": vendor_product},  # never the session
        ip=ip,
    )
