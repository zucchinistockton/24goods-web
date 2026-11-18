"""Support's view of a buyer-consented session (owner request 2026-11-18, review-v12 §26).

The API never opens a session: ``status`` reads the non-secret columns of ``support_sessions`` (and
the consent flag of a credential still waiting in procurement), ``reveal`` asks procurement to open
the sealed copy for ONE answer. Every reveal needs a staff role and a written reason, is rate
limited per staff member and leaves a critical audit row (who, why, which order; never the
session). The
plaintext goes back to the console with ``no-store`` and nowhere else.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import NotFound, Unavailable, Validation
from app.core.ratelimit import enforce
from app.core.resources import Resources
from app.models import Order, ProcurementCredential, SupportSession, User

log = logging.getLogger("app.orders.support_session")

REVEALS_PER_HOUR = 20  # per staff member


async def _order(db: AsyncSession, tenant_id: uuid.UUID, order_id: uuid.UUID) -> Order:
    order = await db.scalar(select(Order).where(Order.id == order_id, Order.tenant_id == tenant_id))
    if order is None or order.delivery_mode != "upstream_direct":
        raise NotFound("Order not found")
    return order


async def status(db: AsyncSession, staff: User, order_id: uuid.UUID) -> dict[str, Any]:
    order = await _order(db, staff.tenant_id, order_id)
    kept = (
        await db.execute(
            select(
                SupportSession.email_masked,
                SupportSession.reason,
                SupportSession.created_at,
                SupportSession.revealed_count,
                SupportSession.last_revealed_at,
            ).where(
                SupportSession.tenant_id == order.tenant_id, SupportSession.order_id == order.id
            )
        )
    ).first()
    consent = await db.scalar(
        select(ProcurementCredential.share_with_support).where(
            ProcurementCredential.tenant_id == order.tenant_id,
            ProcurementCredential.order_id == order.id,
        )
    )
    return {
        "available": kept is not None,
        # the buyer agreed and the session is still with procurement: it is copied for support
        # as soon as the order goes to staff or fails
        "consented": bool(consent) or kept is not None,
        "account": kept.email_masked if kept else None,
        "reason": kept.reason if kept else None,
        "kept_since": kept.created_at.isoformat() if kept else None,
        "revealed_count": kept.revealed_count if kept else 0,
        "last_revealed_at": kept.last_revealed_at.isoformat()
        if kept and kept.last_revealed_at
        else None,
    }


async def reveal(
    db: AsyncSession,
    res: Resources,
    staff: User,
    order_id: uuid.UUID,
    reason: str,
    ip: str | None,
) -> dict[str, Any]:
    clean = (reason or "").strip()
    if len(clean) < 8:
        raise Validation("Say why you need the session", code="reason_required")
    await enforce(res.redis, "admin:support-session", str(staff.id), REVEALS_PER_HOUR, 3600)
    order = await _order(db, staff.tenant_id, order_id)
    token = res.settings.procurement_token
    if len(token) < 32:
        raise Unavailable("Not available right now", code="support_session_unavailable")
    try:
        resp = await res.procurement.post(
            "/internal/support-session",
            json={"tenant_id": str(order.tenant_id), "order_id": str(order.id)},
            headers={"X-Internal-Token": token},
            timeout=15.0,
        )
    except httpx.HTTPError:
        raise Unavailable("Not available right now", code="support_session_unavailable") from None
    if resp.status_code == 404:
        raise NotFound("No shared session for this order", code="support_session_none")
    if resp.status_code != 200:
        raise Unavailable("Not available right now", code="support_session_unavailable")
    out = resp.json()
    await audit(
        db,
        tenant_id=order.tenant_id,
        action="order.support_session_revealed",
        actor_id=staff.id,
        actor_role=staff.role,
        target=f"order:{order.id}",
        detail={"reason": clean[:200], "order_no": order.order_no},  # never the session
        ip=ip,
        severity="critical",
    )
    await db.commit()
    return {
        "session": out.get("session"),
        "account": out.get("account"),
        "reason": out.get("reason"),
        "kept_since": out.get("kept_since"),
    }
