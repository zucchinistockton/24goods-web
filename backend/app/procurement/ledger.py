"""Supplier cost ledger (review-v12 §6): ``balance_change`` rows only.

ONE insert path (:func:`record_rows`, ``ON CONFLICT DO NOTHING`` on ``(supplier_id,
external_id)`` = the vendor's balance-log id) is shared by the webhook consumer and the
10-minute ``GET /balance-logs`` backfill, so a row that arrives both ways is booked once.
``card_fee`` events are never booked (the same money arrives as a ``balance_change``). Amounts
are ``Decimal`` next to a currency; card numbers are masked before they get here.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.db import tenant_session
from app.models import ProcurementJob, SupplierAccount, SupplierLedgerEntry
from app.procurement.context import ProcurementContext
from app.procurement.service import SupplierRef, make_supplier
from app.suppliers.base import LedgerRow, clean_detail, dec
from app.suppliers.registry import SupplierConfigError
from app.suppliers.zovocard.supplier import CARD_ROW_TYPES, parse_time

log = logging.getLogger("app.procurement.ledger")

LINK_TYPE_PREFIX = "gpt_direct"  # ref_id of these rows is the CDK / direct order, not a card id
# card rows (review-v12 §12): the opening charge is booked as ``card_recharge``, the open fee as a
# separate ``open_card`` row. Their ``ref_id`` is the vendor's own and is NOT the card id, so a card
# row links to its job through ``card_id`` (= the job's ``upstream_ref`` of a ``vcc`` job).
CARD_TYPES = CARD_ROW_TYPES


MAX_ABS = Decimal("1000000000000")
BACKFILL_PAGE_SIZE = 100
BACKFILL_MAX_PAGES = 50  # hard cap per pass (5000 rows)
# A pass always covers at least this much history, even when the newest page brings nothing new
# (webhooks may have booked the newest rows while older ones were missed): L5 of the M2 audit.
BACKFILL_LOOKBACK = timedelta(days=2)


def row_from_event(payload: Mapping[str, Any]) -> LedgerRow | None:
    """A stored (already masked, whitelisted) ``balance_change`` payload -> a ledger row."""
    ext = payload.get("balance_log_id")
    if isinstance(ext, bool) or not isinstance(ext, (int, str)) or not str(ext).strip():
        return None
    try:
        amount = dec(payload.get("amount"))
    except ValueError:
        return None
    after = None
    try:
        after = dec(payload.get("balance_after"))
    except ValueError:
        pass
    ref = payload.get("ref_id")
    card = payload.get("card_id")
    card_id = (
        str(card)[:64]
        if isinstance(card, (int, str)) and not isinstance(card, bool) and card != ""
        else None
    )
    mask = payload.get("card_number")
    cur = payload.get("currency")
    return LedgerRow(
        external_id=str(ext).strip()[:64],
        type=str(payload.get("type") or "unknown")[:48],
        amount=amount,
        currency=cur[:3].upper() if isinstance(cur, str) else "",
        occurred_at=parse_time(payload.get("occurred_at")),
        balance_after=after,
        ref_id=str(ref)[:64] if isinstance(ref, (int, str)) and ref != "" else None,
        card_mask=mask[:24] if isinstance(mask, str) else None,
        card_id=card_id,
    )


async def record_rows(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    supplier_id: uuid.UUID,
    default_currency: str,
    rows: list[LedgerRow],
    fallback_time: datetime | None = None,
) -> int:
    """The single unique insert path. Returns the number of rows that were new."""
    new = 0
    for r in rows:
        if abs(r.amount) >= MAX_ABS:
            continue
        job_id = None
        # any row that names a card links by card id (F9); gpt_direct rows by their order ref
        card_row = r.card_id is not None
        link = r.card_id if card_row else r.ref_id if r.type.startswith(LINK_TYPE_PREFIX) else None
        if link:
            job_id = await db.scalar(
                select(ProcurementJob.id).where(
                    ProcurementJob.tenant_id == tenant_id,
                    ProcurementJob.supplier_id == supplier_id,
                    ProcurementJob.upstream_ref == link,
                    *((ProcurementJob.kind == "vcc",) if card_row else ()),
                )
            )
        inserted = await db.scalar(
            pg_insert(SupplierLedgerEntry)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                supplier_id=supplier_id,
                external_id=r.external_id,
                type=r.type,
                amount=r.amount,
                currency=(r.currency or default_currency or "USD")[:3],
                balance_after=r.balance_after,
                job_id=job_id,
                ref_id=r.ref_id,
                card_mask=r.card_mask,
                card_id=r.card_id,
                occurred_at=r.occurred_at or fallback_time or datetime.now(timezone.utc),
            )
            .on_conflict_do_nothing(index_elements=["supplier_id", "external_id"])
            .returning(SupplierLedgerEntry.id)
        )
        if inserted is not None:
            new += 1
    return new


async def link_rows(db: AsyncSession, tenant_id: uuid.UUID, supplier_id: uuid.UUID) -> int:
    """A ledger row can arrive before its job learns the upstream reference: link the late ones."""
    job = (
        select(ProcurementJob.id)
        .where(
            ProcurementJob.tenant_id == tenant_id,
            ProcurementJob.supplier_id == supplier_id,
            ProcurementJob.upstream_ref == SupplierLedgerEntry.ref_id,
        )
        .order_by(ProcurementJob.created_at)
        .limit(1)
        .scalar_subquery()
    )
    res = await db.execute(
        update(SupplierLedgerEntry)
        .where(
            SupplierLedgerEntry.tenant_id == tenant_id,
            SupplierLedgerEntry.supplier_id == supplier_id,
            SupplierLedgerEntry.job_id.is_(None),
            SupplierLedgerEntry.ref_id.is_not(None),
            SupplierLedgerEntry.type.like(LINK_TYPE_PREFIX + "%"),
            job.is_not(None),
        )
        .values(job_id=job)
    )
    card_job = (
        select(ProcurementJob.id)
        .where(
            ProcurementJob.tenant_id == tenant_id,
            ProcurementJob.supplier_id == supplier_id,
            ProcurementJob.kind == "vcc",
            ProcurementJob.upstream_ref == SupplierLedgerEntry.card_id,
        )
        .order_by(ProcurementJob.created_at)
        .limit(1)
        .scalar_subquery()
    )
    cards = await db.execute(
        update(SupplierLedgerEntry)
        .where(
            SupplierLedgerEntry.tenant_id == tenant_id,
            SupplierLedgerEntry.supplier_id == supplier_id,
            SupplierLedgerEntry.job_id.is_(None),
            SupplierLedgerEntry.card_id.is_not(None),
            card_job.is_not(None),
        )
        .values(job_id=card_job)
    )
    return int(res.rowcount or 0) + int(cards.rowcount or 0)  # type: ignore[attr-defined]


async def handle_balance_event(
    db: AsyncSession, *, tenant_id: uuid.UUID, supplier_id: uuid.UUID, payload: Mapping[str, Any],
    received_at: datetime | None,
) -> bool:  # fmt: skip
    """Engine: a stored ``balance_change`` event. ``True`` when a new ledger row was booked."""
    row = row_from_event(payload)
    if row is None:
        log.warning("balance_change event without a usable id or amount")
        return False
    cur = await db.scalar(select(SupplierAccount.currency).where(SupplierAccount.id == supplier_id))
    return (
        await record_rows(
            db,
            tenant_id=tenant_id,
            supplier_id=supplier_id,
            default_currency=cur or "USD",
            rows=[row],
            fallback_time=received_at,
        )
        > 0
    )


async def backfill(ctx: ProcurementContext, ref: SupplierRef, currency: str) -> int:
    """``GET /balance-logs`` newest first, page by page, until a page reaches rows older than the
    last booked row and the lookback window (never because one page had no new rows), or the hard
    page cap. Returns the number of new rows."""
    try:
        supplier = make_supplier(ctx, ref)
    except SupplierConfigError:
        return 0
    fetch = getattr(supplier, "balance_logs", None)
    total = 0
    try:
        if fetch is None:
            return 0
        async with tenant_session(ctx.maker, ref.tenant_id) as db:
            last_booked = await db.scalar(
                select(func.max(SupplierLedgerEntry.occurred_at)).where(
                    SupplierLedgerEntry.tenant_id == ref.tenant_id,
                    SupplierLedgerEntry.supplier_id == ref.id,
                )
            )
        # page until the rows are older than the last booked row AND the lookback window
        floor = datetime.now(timezone.utc) - BACKFILL_LOOKBACK
        if last_booked is not None:
            floor = min(floor, last_booked)
        for page in range(1, BACKFILL_MAX_PAGES + 1):
            rows: list[LedgerRow] = await fetch(page, BACKFILL_PAGE_SIZE)
            if not rows:
                break
            async with tenant_session(ctx.maker, ref.tenant_id) as db:
                new = await record_rows(
                    db,
                    tenant_id=ref.tenant_id,
                    supplier_id=ref.id,
                    default_currency=currency,
                    rows=rows,
                )
                await db.commit()
            total += new
            dated = [r.occurred_at for r in rows if r.occurred_at is not None]
            if len(rows) < BACKFILL_PAGE_SIZE or (dated and min(dated) < floor):
                break
    finally:
        await supplier.aclose()
    async with tenant_session(ctx.maker, ref.tenant_id) as db:
        linked = await link_rows(db, ref.tenant_id, ref.id)
        if total or linked:
            await audit(
                db,
                tenant_id=ref.tenant_id,
                action="supplier.ledger_backfill",
                target=f"supplier:{ref.id}",
                detail={"new": total, "linked": linked, "source": clean_detail("balance-logs")},
            )
        await db.commit()
    return total
