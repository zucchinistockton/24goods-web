"""Funding-card rules for ``upstream_direct`` (payment audit H1, M2).

The supplier pays a direct order with a card of OUR vendor account (``card_id`` of the offer). It
must never be a card that was bought for a buyer (``upstream_vcc``): that card is the buyer's money
and secret. Shared by the console (source save) and the engine (before every first send).
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProcurementJob, SupplierOffer

IN_FLIGHT_STATES = ("submitting", "accepted_pending")


async def funding_card_clash(
    db: AsyncSession, tenant_id: uuid.UUID, supplier_id: uuid.UUID, card_id: int | str
) -> bool:
    """True when ``card_id`` is the vendor card of ANY ``vcc`` job of this supplier (any state: a
    voided card id is never reused as a funding card either)."""
    n = await db.scalar(
        select(func.count(ProcurementJob.id)).where(
            ProcurementJob.tenant_id == tenant_id,
            # a hand-pasted card job has no supplier: its vendor card id counts for every supplier
            (ProcurementJob.supplier_id == supplier_id) | ProcurementJob.supplier_id.is_(None),
            ProcurementJob.kind == "vcc",
            ProcurementJob.upstream_ref == str(card_id),
        )
    )
    return bool(n)


async def funding_mask_clash(db: AsyncSession, tenant_id: uuid.UUID, mask: str | None) -> bool:
    """True when the funding card's masked number equals the ``card_mask`` of any non-voided card
    job of the tenant (catches a card pasted by hand whose vendor id was never recorded)."""
    if not mask:
        return False
    n = await db.scalar(
        select(func.count(ProcurementJob.id)).where(
            ProcurementJob.tenant_id == tenant_id,
            ProcurementJob.kind == "vcc",
            ProcurementJob.card_mask == mask,
            ProcurementJob.state != "voided",
        )
    )
    return bool(n)


async def lock_card(db: AsyncSession, supplier_id: uuid.UUID, card_id: int | str) -> None:
    """Serialise the in-flight check of ONE funding card across concurrent leases."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"dcard:{supplier_id}:{card_id}"}
    )


async def card_in_flight(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    supplier_id: uuid.UUID,
    card_id: int | str,
    exclude: uuid.UUID,
) -> int:
    """Other direct jobs of this supplier paying with the same card that may be at the vendor."""
    return int(
        await db.scalar(
            select(func.count(ProcurementJob.id))
            .join(SupplierOffer, SupplierOffer.id == ProcurementJob.offer_id)
            .where(
                ProcurementJob.tenant_id == tenant_id,
                ProcurementJob.supplier_id == supplier_id,
                ProcurementJob.kind == "direct",
                ProcurementJob.id != exclude,
                ProcurementJob.state.in_(IN_FLIGHT_STATES),
                ProcurementJob.maybe_sent.is_(True),
                SupplierOffer.params["card_id"].astext == str(card_id),
            )
        )
        or 0
    )
