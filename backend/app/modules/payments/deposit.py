"""Per-gateway merchant deposit rule (owner request 2026-11-26).

Some EPay operators (Xiaodou) demand a deposit of ``max(floor, highest single payment of the last
30 days x pct)``. A payment that raises the due amount above what was paid starts a 48 h grace
period; after it the gateway refuses new orders until the deposit is paid in full. The gateway's
rule is not ours: these helpers only let routing avoid triggering it and let staff see the number.

Pure integer arithmetic on CNY minor units (never float). All three settings NULL on a gateway
means the guard is off for it (the behaviour before migration 0043).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Checkout, Order, Payment, PaymentAttempt

WINDOW = timedelta(days=30)
G = TypeVar("G")


def required_minor(max30_minor: int, pct: int, floor_minor: int) -> int:
    """``max(floor, ceil(max30 * pct / 100))``: the deposit the gateway wants right now."""
    return max(int(floor_minor), -(-int(max30_minor) * int(pct) // 100))


def cap_minor(deposit_minor: int, pct: int) -> int:
    """``floor(deposit * 100 / pct)``: the largest single payment that keeps the paid deposit
    sufficient (a bigger one would raise the amount due above what was paid)."""
    return int(deposit_minor) * 100 // int(pct)


def guarded(row: Any) -> bool:
    """The guard needs the paid deposit and the percentage (the floor only affects ``required``)."""
    return (
        getattr(row, "deposit_minor", None) is not None
        and getattr(row, "deposit_pct", None) is not None
    )


def within_cap(rows: Sequence[G], amount_minor: int) -> list[G]:
    """Gateways whose paid deposit covers a payment of ``amount_minor``; unguarded ones always do.

    Order is kept. When EVERY gateway would be skipped the full list is returned unchanged: a
    payment that may raise a deposit is better than no payment (the alert on settlement then
    tells staff to top the deposit up within the gateway's 48 h grace)."""
    ok = [
        r
        for r in rows
        if not guarded(r) or amount_minor <= cap_minor(r.deposit_minor, r.deposit_pct)  # type: ignore[attr-defined]
    ]
    return ok or list(rows)


def _paid_payable(model: Any, col: Any, link: Any) -> Any:
    """EXISTS: the attempt's payable (order or checkout, same tenant) has a succeeded payment."""
    return exists().where(
        col == PaymentAttempt.local_ref,
        model.tenant_id == PaymentAttempt.tenant_id,
        Payment.tenant_id == PaymentAttempt.tenant_id,
        link == model.id,
        Payment.status == "succeeded",
    )


async def max30_minor(
    db: AsyncSession,
    tid: uuid.UUID,
    pid: uuid.UUID,
    *,
    exclude_ref: str | None = None,
    now: datetime | None = None,
) -> int:
    """Largest single payment (minor) received through gateway ``pid`` in the last 30 days.

    Counts attempts the reconciler marked ``settled`` plus attempts still ``ready`` whose payable
    has a succeeded payment: the notify path settles the order without touching the attempt row.
    ``exclude_ref`` leaves one attempt (by gateway reference) out, to compare a payment that has
    just settled with what came before it."""
    since = (now or datetime.now(timezone.utc)) - WINDOW
    paid = _paid_payable(Order, Order.order_no, Payment.order_id) | _paid_payable(
        Checkout, Checkout.checkout_no, Payment.checkout_id
    )
    stmt = select(func.max(PaymentAttempt.amount_minor)).where(
        PaymentAttempt.tenant_id == tid,
        PaymentAttempt.provider_id == pid,
        PaymentAttempt.created_at >= since,
        (PaymentAttempt.status == "settled") | ((PaymentAttempt.status == "ready") & paid),
    )
    if exclude_ref is not None:
        stmt = stmt.where(PaymentAttempt.gateway_ref != exclude_ref)
    return int(await db.scalar(stmt) or 0)
