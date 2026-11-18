"""Buyer-consented session sharing with support (owner request 2026-11-18, review-v12 §26).

When the buyer agreed at checkout (``procurement_credentials.share_with_support``) and the direct
order goes to staff (``needs_manual``) or fails, the SEALED session is copied into
``support_sessions`` (same blob, same AAD: tenant + order id) so that it outlives the 6 h ceiling
of the credential table for :data:`SUPPORT_WINDOW`. Support reads it through
``/internal/support-session``: this module opens it in memory for that one answer, counts the
read, and never logs or stores the plaintext. It is deleted after the window, or as soon as the
order was delivered (the problem is solved).
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import tenant_session
from app.procurement.context import ProcurementContext

log = logging.getLogger("app.procurement.support_session")

SUPPORT_WINDOW = timedelta(hours=72)
SOLVED = ("delivered", "completed")  # the order went through after all: nothing to investigate

_COPY = (
    "INSERT INTO support_sessions (id, tenant_id, order_id, product, kid, blob, email_masked, "
    "reason) "
    "SELECT gen_random_uuid(), c.tenant_id, c.order_id, c.product, c.kid, c.blob, c.email_masked, "
    "left((SELECT j.error_code FROM procurement_jobs j WHERE j.order_id = c.order_id "
    "AND j.tenant_id = c.tenant_id AND j.kind = 'direct' ORDER BY j.updated_at DESC LIMIT 1), 48) "
    "FROM procurement_credentials c "
    "WHERE c.tenant_id = :t AND c.share_with_support {extra} "
    "ON CONFLICT (order_id) DO NOTHING"
)
_FAILING = (
    "AND (EXISTS (SELECT 1 FROM orders o WHERE o.id = c.order_id AND o.tenant_id = c.tenant_id "
    "AND o.status = 'manual_review') OR EXISTS (SELECT 1 FROM procurement_jobs j "
    "WHERE j.order_id = c.order_id AND j.tenant_id = c.tenant_id AND j.kind = 'direct' "
    "AND j.state IN ('needs_manual', 'failed', 'voided')))"
)


async def keep_for_order(db: AsyncSession, tenant_id: uuid.UUID, order_id: uuid.UUID) -> None:
    """Engine hook (tenant-pinned session of the job's transaction): the job of this order is
    going to staff. Copies the sealed session if, and only if, the buyer consented."""
    await db.execute(
        text(_COPY.format(extra="AND c.order_id = :o")), {"t": tenant_id, "o": order_id}
    )


async def keep_failing(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Sweeper (tenant-pinned, BEFORE the credentials are deleted): every consented session of an
    order in review or with a failed / held direct job."""
    res = await db.execute(text(_COPY.format(extra=_FAILING)), {"t": tenant_id})
    return int(res.rowcount or 0)  # type: ignore[attr-defined]


async def sweep(ctx: ProcurementContext, tenants: list[uuid.UUID]) -> int:
    """Delete support copies past the window (unpinned, the sweep policy) and those of orders that
    were delivered after all (tenant-pinned: orders are RLS-protected)."""
    n = 0
    async with tenant_session(ctx.maker, None) as db:
        res = await db.execute(
            text(
                "DELETE FROM support_sessions "
                "WHERE created_at < now() - make_interval(secs => :age)"
            ),
            {"age": SUPPORT_WINDOW.total_seconds()},
        )
        n += int(res.rowcount or 0)  # type: ignore[attr-defined]
        await db.commit()
    for tid in tenants:
        async with tenant_session(ctx.maker, tid) as db:
            res = await db.execute(
                text(
                    "DELETE FROM support_sessions s USING orders o "
                    "WHERE o.id = s.order_id AND o.tenant_id = s.tenant_id "
                    "AND o.status = ANY(:solved)"
                ),
                {"solved": list(SOLVED)},
            )
            n += int(res.rowcount or 0)  # type: ignore[attr-defined]
            await db.commit()
    return n


async def reveal(
    ctx: ProcurementContext, tenant_id: uuid.UUID, order_id: uuid.UUID
) -> dict[str, Any]:
    """Open the support copy of one order for support (the API audits who and why). Raises
    ``LookupError`` when there is none (no consent, not failing, swept, other tenant)."""
    if ctx.cred_opener is None:
        raise LookupError("no_key")
    async with tenant_session(ctx.maker, tenant_id) as db:
        row = (
            await db.execute(
                text(
                    "SELECT blob, email_masked, reason, created_at FROM support_sessions "
                    "WHERE tenant_id = :t AND order_id = :o "
                    "AND created_at >= now() - make_interval(secs => :age)"
                ),
                {"t": tenant_id, "o": order_id, "age": SUPPORT_WINDOW.total_seconds()},
            )
        ).first()
        if row is None:
            raise LookupError("none")
        try:
            session = ctx.cred_opener.open(
                bytes(row.blob), tenant_id=str(tenant_id), order_id=str(order_id)
            )
        except Exception:  # wrong key / tampered: never echo detail
            raise LookupError("unreadable") from None
        await db.execute(
            text(
                "UPDATE support_sessions SET revealed_count = revealed_count + 1, "
                "last_revealed_at = now() WHERE tenant_id = :t AND order_id = :o"
            ),
            {"t": tenant_id, "o": order_id},
        )
        await db.commit()
    log.info("support session read order=%s", order_id)  # never the session
    return {
        "session": session,
        "account": row.email_masked,
        "reason": row.reason,
        "kept_since": row.created_at.isoformat(),
    }
