"""Balance breaker (review-v12 §6).

``spendable_balance - sum(reserved redemption cost of issued, unredeemed CDKs) - sum(in-flight
jobs) < suppliers.min_spendable`` -> the supplier is unavailable for NEW jobs and staff are
alerted (critical audit row; the API reconciler turns every open breaker into one staff
notification). Unlike the hard breaker a supplier refusal opens (``breaker_kind = 'hard'``,
manual reset only), this one (``breaker_kind = 'balance'``) closes by itself as soon as the
balance is back above the threshold.

Reserve per CDK = the supplier's own estimate of the redemption cost (ZovoCard: the cached plan's
``expectedAmountMinor`` + buffers + service fee when the plan currency is USD) or, when the
supplier cannot say, the offer's configured ``cost_estimate`` in the balance currency.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from app.core.audit import audit
from app.core.db import tenant_session
from app.models import ProcurementJob, SupplierAccount, SupplierOffer
from app.procurement.context import ProcurementContext
from app.suppliers.base import Balance, OfferSpec, Supplier

log = logging.getLogger("app.procurement.breaker")

REASON = "balance_low"
KIND_BALANCE = "balance"
KIND_HARD = "hard"
_REDEEMED = ("consumed", "disabled")  # CDK statuses whose cost already hit the balance / is gone
IN_FLIGHT = ("queued", "submitting", "accepted_pending")


@dataclass(frozen=True)
class Exposure:
    reserve: Decimal
    issued: int
    in_flight: int
    unknown_cost: int  # CDKs/jobs whose offer has no usable cost (reserved as 0)


async def _exposure(
    ctx: ProcurementContext, ref_tenant: uuid.UUID, supplier_id: uuid.UUID, currency: str,
    supplier: Supplier | None,
) -> Exposure:  # fmt: skip
    async with tenant_session(ctx.maker, ref_tenant) as db:
        issued_rows = (
            await db.execute(
                select(ProcurementJob.offer_id, func.count())
                .where(
                    ProcurementJob.tenant_id == ref_tenant,
                    ProcurementJob.supplier_id == supplier_id,
                    ProcurementJob.state == "fulfilled",
                    ProcurementJob.upstream_ref.is_not(None),
                    ProcurementJob.upstream_status.not_in(_REDEEMED)
                    | ProcurementJob.upstream_status.is_(None),
                )
                .group_by(ProcurementJob.offer_id)
            )
        ).all()
        flight_rows = (
            await db.execute(
                select(ProcurementJob.offer_id, func.count())
                .where(
                    ProcurementJob.tenant_id == ref_tenant,
                    ProcurementJob.supplier_id == supplier_id,
                    ProcurementJob.state.in_(IN_FLIGHT),
                )
                .group_by(ProcurementJob.offer_id)
            )
        ).all()
        offers = {
            r.id: r
            for r in (
                await db.execute(
                    select(
                        SupplierOffer.id,
                        SupplierOffer.kind,
                        SupplierOffer.params,
                        SupplierOffer.cost_estimate,
                        SupplierOffer.currency,
                    ).where(
                        SupplierOffer.tenant_id == ref_tenant,
                        SupplierOffer.supplier_id == supplier_id,
                    )
                )
            ).all()
        }
    counts: Counter[Any] = Counter()
    for oid, n in issued_rows:
        counts[oid] += n
    for oid, n in flight_rows:
        counts[oid] += n
    reserve, unknown = Decimal(0), 0
    for oid, n in counts.items():
        off = offers.get(oid)
        unit: Decimal | None = None
        if off is not None and supplier is not None:
            fn = getattr(supplier, "redemption_cost", None)
            if fn is not None:
                try:
                    money = await fn(OfferSpec(kind="cdk", params=dict(off.params or {})))
                except Exception as exc:  # an unreadable catalog falls back to the configured cost
                    log.warning("redemption cost unavailable error=%s", type(exc).__name__)
                    money = None
                if money is not None and money.currency == currency:
                    unit = money.amount
        if (
            unit is None
            and off is not None
            and off.cost_estimate is not None
            and off.currency == currency
        ):
            unit = off.cost_estimate
        if unit is None:
            unknown += n
            continue
        reserve += unit * n
    return Exposure(
        reserve,
        sum(n for _, n in issued_rows),
        sum(n for _, n in flight_rows),
        unknown,
    )


async def evaluate(
    ctx: ProcurementContext, ref: Any, balance: Balance, supplier: Supplier | None
) -> None:
    """Open or (balance kind only) close the breaker from a fresh balance."""
    async with tenant_session(ctx.maker, ref.tenant_id) as db:
        row = (
            await db.execute(
                select(
                    SupplierAccount.min_spendable,
                    SupplierAccount.breaker_open,
                    SupplierAccount.breaker_kind,
                ).where(SupplierAccount.tenant_id == ref.tenant_id, SupplierAccount.id == ref.id)
            )
        ).first()
    if row is None:
        return
    exp = await _exposure(ctx, ref.tenant_id, ref.id, balance.currency, supplier)
    available = balance.spendable - exp.reserve
    low = available < row.min_spendable
    detail = {
        "spendable": str(balance.spendable),
        "reserved": str(exp.reserve),
        "available": str(available),
        "min_spendable": str(row.min_spendable),
        "currency": balance.currency,
        "issued_unredeemed": exp.issued,
        "in_flight": exp.in_flight,
        "unknown_cost": exp.unknown_cost,
    }
    if low == (row.breaker_open and row.breaker_kind == KIND_BALANCE):
        return  # nothing changes (a hard breaker is never touched here)
    if row.breaker_open and row.breaker_kind != KIND_BALANCE:
        return
    async with tenant_session(ctx.maker, ref.tenant_id) as db:
        sup = await db.scalar(
            select(SupplierAccount)
            .where(SupplierAccount.tenant_id == ref.tenant_id, SupplierAccount.id == ref.id)
            .with_for_update()
        )
        if sup is None:
            return
        if low and not sup.breaker_open:
            sup.breaker_open, sup.breaker_kind = True, KIND_BALANCE
            sup.breaker_reason, sup.breaker_at = REASON, datetime.now(timezone.utc)
            await audit(
                db,
                tenant_id=ref.tenant_id,
                action="supplier.breaker_opened",
                target=f"supplier:{sup.id}",
                after={"breaker_open": True, "reason": REASON, "kind": KIND_BALANCE},
                detail=detail,
                severity="critical",
            )
        elif not low and sup.breaker_open and sup.breaker_kind == KIND_BALANCE:
            sup.breaker_open, sup.breaker_kind = False, None
            sup.breaker_reason, sup.breaker_at = None, None
            await audit(
                db,
                tenant_id=ref.tenant_id,
                action="supplier.breaker_closed",
                target=f"supplier:{sup.id}",
                before={"breaker_open": True, "reason": REASON, "kind": KIND_BALANCE},
                after={"breaker_open": False},
                detail=detail,
                severity="info",
            )
        await db.commit()
