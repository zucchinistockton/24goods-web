"""SMS verification code orders, API side (ADR-057).

The API never talks to HeroSMS: it reads the session / attempt rows procurement writes and
forwards the buyer's "Get number" to procurement's internal API. Codes are not here at all: the
buyer reads the delivered code through the normal reveal path.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, InvalidState, NotFound, OutOfStock, Unavailable, Validation
from app.models import Order, SmsAttempt, SmsProduct, SmsSession, SupplierAccount, User
from app.modules.common.modes import MAX_SMS_QTY
from app.suppliers.herosms import params as P

MODE = "upstream_sms"
# orders that count against the buyer's daily limit (everything but cancelled / expired)
_LIVE_STATES = ("pending_payment", "paid", "delivered", "completed", "manual_review", "refunded")
# refusal codes procurement may answer; anything else is "unavailable"
REFUSALS = frozenset(
    {
        "sms_not_found",
        "sms_not_ready",
        "sms_delivered",
        "sms_failed",
        "sms_window_over",
        "sms_attempts_exhausted",
        "sms_too_early",
        "sms_busy",
        "sms_unavailable",
    }
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- availability
def _usable():  # type: ignore[no-untyped-def]
    return (
        SmsProduct.enabled.is_(True),
        SmsProduct.last_check_ok.is_(True),
        SupplierAccount.enabled.is_(True),
        SupplierAccount.breaker_open.is_(False),
    )


async def stock(db: AsyncSession, product_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """``{product_id: shown stock}`` for every product that HAS an SMS configuration (0 = sold
    out). Products without one are not in the result."""
    if not product_ids:
        return {}
    configured = set(
        await db.scalars(
            select(SmsProduct.product_id).where(SmsProduct.product_id.in_(product_ids))
        )
    )
    if not configured:
        return {}
    rows = await db.execute(
        select(SmsProduct.product_id, SmsProduct.stock_hint)
        .join(SupplierAccount, SupplierAccount.id == SmsProduct.supplier_id)
        .where(SmsProduct.product_id.in_(list(configured)), *_usable())
    )
    out = {pid: 0 for pid in configured}
    for pid, hint in rows.all():
        out[pid] = max(0, int(hint or 0))
    return out


async def checkout_guard(db: AsyncSession, buyer: User, product_id: uuid.UUID, qty: int) -> None:
    """Every path that creates an SMS order: qty 1, in stock, the buyer's daily limit."""
    if qty != MAX_SMS_QTY:
        raise Validation("Only one code per order", code="sms_qty_one")
    cfg = await db.scalar(
        select(SmsProduct)
        .join(SupplierAccount, SupplierAccount.id == SmsProduct.supplier_id)
        .where(
            SmsProduct.tenant_id == buyer.tenant_id,
            SmsProduct.product_id == product_id,
            *_usable(),
        )
    )
    if cfg is None or cfg.stock_hint <= 0:
        raise OutOfStock("Not available right now", extra={"product_id": str(product_id)})
    params = P.normalize(cfg.params)
    day0 = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    mine = await db.scalar(
        select(func.count())
        .select_from(Order)
        .where(
            Order.tenant_id == buyer.tenant_id,
            Order.buyer_id == buyer.id,
            Order.delivery_mode == MODE,
            Order.created_at >= day0,
            Order.status.in_(_LIVE_STATES),
        )
    )
    if int(mine or 0) >= params["per_buyer_daily"]:
        raise Conflict("Daily limit for verification codes reached", code="sms_daily_limit")


# --------------------------------------------------------------------------- buyer view
async def buyer_view(db: AsyncSession, order: Order) -> dict[str, Any]:
    if order.delivery_mode != MODE:
        raise NotFound("Order not found")
    sess = await db.scalar(
        select(SmsSession).where(
            SmsSession.tenant_id == order.tenant_id, SmsSession.order_id == order.id
        )
    )
    now = utcnow()
    if sess is None:
        return {
            "state": "pending" if order.status == "paid" else "none",
            "order_status": order.status,
        }
    params = P.normalize(sess.params)
    current = await db.scalar(
        select(SmsAttempt)
        .where(
            SmsAttempt.tenant_id == order.tenant_id,
            SmsAttempt.session_id == sess.id,
            SmsAttempt.state == "waiting",
        )
        .order_by(SmsAttempt.seq.desc())
        .limit(1)
    )
    cur = None
    swap_in = 0
    if current is not None:
        cur = {
            "phone": current.phone,
            "bought_at": current.bought_at.isoformat(),
            "expires_at": current.expires_at.isoformat() if current.expires_at else None,
            "wait_s": params["number_wait_s"],
        }
        swap_in = max(0, int(params["min_swap_s"] - (now - current.bought_at).total_seconds()))
    return {
        "state": sess.state,
        "order_status": order.status,
        "attempts_used": sess.attempts_used,
        "max_attempts": params["max_attempts"],
        "can_swap_in_s": swap_in,
        "window_ends_at": sess.window_ends_at.isoformat() if sess.window_ends_at else None,
        "start_deadline": sess.start_deadline.isoformat(),
        "current": cur,
    }


async def next_number(procurement: httpx.AsyncClient | None, token: str, order: Order) -> None:
    """Forward "Get number" to procurement. Raises a buyer-facing error on refusal."""
    if order.delivery_mode != MODE:
        raise NotFound("Order not found")
    if order.status != "paid":
        raise InvalidState("This order cannot get a number now", code="sms_not_ready")
    if procurement is None or len(token) < 32:
        raise Unavailable("Please try again in a moment", code="sms_unavailable")
    try:
        resp = await procurement.post(
            "/internal/sms/next",
            json={"tenant_id": str(order.tenant_id), "order_id": str(order.id)},
            headers={"X-Internal-Token": token},
            timeout=75.0,
        )
    except httpx.HTTPError:
        raise Unavailable("Please try again in a moment", code="sms_unavailable") from None
    if resp.status_code == 200:
        return
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code == 429:
        raise Conflict("Too many requests, please wait", code="sms_busy")
    code = str((body or {}).get("refused") or "")
    if resp.status_code == 409 and code in REFUSALS:
        extra = {}
        if code == "sms_too_early" and isinstance(body.get("seconds"), int):
            extra["seconds"] = str(body["seconds"])
        raise Conflict(code, code=code, extra=extra)
    raise Unavailable("Please try again in a moment", code="sms_unavailable")


# --------------------------------------------------------------------------- staff view
def mask_phone(phone: str) -> str:
    d = "".join(ch for ch in phone if ch.isdigit())
    if len(d) <= 6:
        return "*" * len(d)
    return f"{d[:4]}{'*' * (len(d) - 6)}{d[-2:]}"


async def staff_view(db: AsyncSession, tenant_id: uuid.UUID, order_id: uuid.UUID) -> dict[str, Any]:
    sess = await db.scalar(
        select(SmsSession).where(SmsSession.tenant_id == tenant_id, SmsSession.order_id == order_id)
    )
    if sess is None:
        return {"session": None, "attempts": []}
    atts = (
        await db.scalars(
            select(SmsAttempt)
            .where(SmsAttempt.tenant_id == tenant_id, SmsAttempt.session_id == sess.id)
            .order_by(SmsAttempt.seq)
        )
    ).all()
    return {
        "session": {
            "state": sess.state,
            "attempts_used": sess.attempts_used,
            "fail_reason": sess.fail_reason,
            "started_at": sess.started_at.isoformat() if sess.started_at else None,
            "window_ends_at": sess.window_ends_at.isoformat() if sess.window_ends_at else None,
            "start_deadline": sess.start_deadline.isoformat(),
            "finished_at": sess.finished_at.isoformat() if sess.finished_at else None,
        },
        "attempts": [
            {
                "seq": a.seq,
                "country": a.country,
                "operator": a.operator,
                "phone": mask_phone(a.phone),
                "cost": str(a.cost) if a.cost is not None else None,
                "state": a.state,
                "bought_at": a.bought_at.isoformat(),
                "code_at": a.code_at.isoformat() if a.code_at else None,
                "closed_at": a.closed_at.isoformat() if a.closed_at else None,
            }
            for a in atts
        ],
    }
