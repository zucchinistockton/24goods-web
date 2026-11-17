"""Decline-rate monitor of our direct orders (issue #29 part 2, review-v12 §21.8).

The vendor watches our decline / chargeback rate against two thresholds (the cost model's
``chargeback_tier1_rate`` 15 % and ``chargeback_tier2_rate`` 30 %). We compute OUR side of it from
``procurement_jobs``: of the direct orders that were sent to the vendor in a window, how many ended
declined. Pure ``Decimal`` maths plus two tenant-scoped queries; framework-light so the API (console
and hourly check) and tests share it. No card number is read or returned: cards are ids.

A "sent" job is a direct job created in the window that reached the vendor (``upstream_ref``,
``first_sent_at`` or ``maybe_sent``). "Declined" is the recorded outcome ``error_code`` in
:data:`DECLINE_CODES` (the engine's own list). "In flight" is a job still submitting / pending.
Chargebacks themselves are NOT readable from the vendor API (see review-v12 §21.8), so this is a
proxy for the vendor's own rate, never the number the vendor bills.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProcurementJob, SupplierOffer

DECLINE_CODES = ("order_declined", "order_failed_precharge")
IN_FLIGHT = ("submitting", "accepted_pending")
WARN_AT = Decimal("0.8")  # a warning badge at 80 % of a threshold
MIN_SAMPLE = 10  # fewer sent orders than this: the rate is shown but never warns (noise)
WINDOWS = (7, 30)
RATE_Q = Decimal("0.0001")
J = ProcurementJob


def rate(declined: int, sent: int) -> Decimal | None:
    """``declined / sent`` as a Decimal fraction (4 places); ``None`` for an empty period."""
    if sent <= 0 or declined < 0:
        return None
    return (Decimal(declined) / Decimal(sent)).quantize(RATE_Q, rounding=ROUND_HALF_UP)


def state_of(r: Decimal | None, sent: int, limit: Decimal) -> str:
    """``ok`` | ``near`` (>= 80 % of ``limit``) | ``over`` (>= ``limit``); few orders: ``ok``."""
    if r is None or sent < MIN_SAMPLE or limit <= 0:
        return "ok"
    if r >= limit:
        return "over"
    if r >= limit * WARN_AT:
        return "near"
    return "ok"


def level_of(r: Decimal | None, sent: int, tier1: Decimal, tier2: Decimal) -> str:
    """``ok`` | ``warning`` (near a threshold) | ``critical`` (over one)."""
    states = (state_of(r, sent, tier1), state_of(r, sent, tier2))
    if "over" in states:
        return "critical"
    return "warning" if "near" in states else "ok"


@dataclass(frozen=True)
class Thresholds:
    tier1: Decimal
    tier2: Decimal
    tier1_min_tx: int


def thresholds_of(cost_settings: dict[str, Any]) -> Thresholds:
    """From the normalised cost-model settings (``core.costmodel.from_tenant``)."""
    return Thresholds(
        Decimal(str(cost_settings["chargeback_tier1_rate"])),
        Decimal(str(cost_settings["chargeback_tier2_rate"])),
        int(cost_settings["chargeback_tier1_min_tx"]),
    )


def view(declined: int, sent: int, in_flight: int, th: Thresholds) -> dict[str, Any]:
    r = rate(declined, sent)
    return {
        "sent": sent,
        "declined": declined,
        "in_flight": in_flight,
        "rate": None if r is None else str(r),
        "level": level_of(r, sent, th.tier1, th.tier2),
        "tier1": state_of(r, sent, th.tier1),
        "tier2": state_of(r, sent, th.tier2),
    }


def _card_key() -> Any:
    """The card WE sent: the job's own record (auto modes), else the offer's fixed card."""
    return func.coalesce(J.card_id, SupplierOffer.params["card_id"].astext)


async def counts(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    days: int,
    *,
    supplier_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> tuple[tuple[int, int, int], dict[str, tuple[int, int, int]]]:
    """``((sent, declined, in_flight), {card_id: (sent, declined, in_flight)})`` of the direct
    jobs of ``tenant_id`` created in the last ``days`` days. Every row filters by tenant."""
    since = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    sent = or_(J.upstream_ref.is_not(None), J.first_sent_at.is_not(None), J.maybe_sent.is_(True))
    declined = and_(sent, J.error_code.in_(DECLINE_CODES))
    flight = J.state.in_(IN_FLIGHT)

    def agg() -> list[Any]:
        return [
            func.count(J.id).filter(sent),
            func.count(J.id).filter(declined),
            func.count(J.id).filter(flight),
        ]

    base = (
        J.tenant_id == tenant_id,
        J.kind == "direct",
        J.created_at >= since,
    )
    sup = (J.supplier_id == supplier_id,) if supplier_id is not None else ()
    total = (await db.execute(select(*agg()).where(*base, *sup))).one()
    key = _card_key()
    rows = await db.execute(
        select(key, *agg())
        .select_from(J)
        .outerjoin(
            SupplierOffer,
            and_(SupplierOffer.id == J.offer_id, SupplierOffer.tenant_id == J.tenant_id),
        )
        .where(*base, *sup, key.is_not(None))
        .group_by(key)
    )
    per_card = {str(r[0]): (int(r[1]), int(r[2]), int(r[3])) for r in rows}
    return (int(total[0]), int(total[1]), int(total[2])), per_card
