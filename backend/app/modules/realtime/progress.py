"""API side of the direct-order progress notification (issue #30, ADR-043).

The procurement process may publish ONE thing, ``{"tenant_id", "order_id"}`` on
``rt:procprogress``. It holds no buyer id and cannot publish on any user channel (its Redis ACL
allows that single channel). This handler runs in the API: it treats the message as a hint,
re-reads the order under that tenant (a mismatching tenant finds nothing), resolves the buyer
itself, recomputes the coarse progress from the database and publishes ``order_progress`` on the
buyer's channel. Malformed messages are ignored."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import tenant_session
from app.models import Order, ProcurementJob, SupplierOffer
from app.modules.realtime.publish import publish_order_progress, publish_staff_progress
from app.suppliers import direct_progress as dp

log = logging.getLogger("app.realtime")

MAX_MESSAGE = 512


def parse(raw: bytes | str) -> tuple[uuid.UUID, uuid.UUID] | None:
    if len(raw) > MAX_MESSAGE:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return uuid.UUID(str(data["tenant_id"])), uuid.UUID(str(data["order_id"]))
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


async def coarse_progress(
    db: AsyncSession, tenant_id: uuid.UUID, order_id: uuid.UUID
) -> tuple[uuid.UUID, dict[str, Any]] | None:
    """(buyer id, coarse progress) of a direct order of this tenant, else None."""
    order = (
        await db.execute(
            select(Order.buyer_id, Order.status).where(
                Order.id == order_id, Order.tenant_id == tenant_id
            )
        )
    ).first()
    if order is None:
        return None
    row = (
        await db.execute(
            select(ProcurementJob, SupplierOffer.params)
            .outerjoin(SupplierOffer, SupplierOffer.id == ProcurementJob.offer_id)
            .where(
                ProcurementJob.tenant_id == tenant_id,
                ProcurementJob.order_id == order_id,
                ProcurementJob.kind == "direct",
            )
            .order_by(ProcurementJob.created_at)
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    job, params = row
    p = dp.from_job(job, half=dp.is_half(params), order_refunded=order.status == "refunded")
    return order.buyer_id, {
        "order_id": str(order_id),
        "step": p.step,
        "percent": p.percent,
        "outcome": p.outcome,
        "reason": p.reason,
    }


async def handle_message(
    maker: async_sessionmaker[AsyncSession], redis: Redis, raw: bytes | str
) -> bool:
    """True when a buyer event was published. Never raises."""
    ids = parse(raw)
    if ids is None:
        return False
    tenant_id, order_id = ids
    try:
        async with tenant_session(maker, tenant_id) as db:
            found = await coarse_progress(db, tenant_id, order_id)
        if found is None:
            return False
        buyer_id, data = found
        await publish_order_progress(redis, buyer_id, data)
        await publish_staff_progress(redis, tenant_id, data)
        return True
    except Exception as exc:
        log.error("progress relay failed error=%s", type(exc).__name__)
        return False
