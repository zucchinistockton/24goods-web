"""Procurement engine (review-v12 §3, §4, §6; ADR-038): ``upstream_cdk`` jobs end to end.

Flow: delivery emits ``procure.requested`` -> one job per order unit is created and COMMITTED
(idempotency key ``pj:<job hex>:<step>`` included) before any supplier call -> the poller leases
due jobs, calls the supplier outside any transaction, and applies the :class:`Outcome`:

* OK -> seal the code (public key only) into ``card_stock`` as ``sold`` for the order, job
  ``fulfilled``; when every unit is fulfilled emit ``procure.fulfilled`` (delivery delivers).
* ACCEPTED_PENDING -> ``accepted_pending``, polled at 15 s, 30 s, 1, 2, 5, 10, 15 min (capped).
* RETRYABLE -> same key, backoff (a write answered RETRYABLE was not executed).
* AMBIGUOUS -> ``query()`` next (same key), never a new key.
* UNAVAILABLE -> ``query()`` first; only when that also finds nothing does the job fail over to
  the next offer (``queued`` with a fresh key for the new supplier) or, with no offer left, go to
  ``needs_manual``.
* FATAL -> ``needs_manual`` + ``procure.failed`` (delivery moves the order to manual_review and
  posts the buyer message; the API reconciler alerts staff).

Payment-audit rules (M2, 2026-11-15): nothing is bought or bound for an order that is not ``paid``
(``failed`` + same-key recovery when a code may exist); availability is checked read-only before
the FIRST send; ``maybe_sent`` (set in the lease commit) decides what may fail over; replays pin the
request hash and a maximum age; a balance reading must be fresh and the balance breaker holds
queued jobs instead of failing them. Lock order: the order's job set, then order, then supplier.

Every job state change writes an audit row. Plaintext codes exist only between the supplier
answer and ``seal()``: never logged, never in an exception, never in a job row.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.cardvault import CardContext
from app.core.db import set_tenant, tenant_session
from app.core.errors import Conflict
from app.models import (
    CardStock,
    Order,
    Outbox,
    ProcurementJob,
    Product,
    ProductVariant,
    SupplierAccount,
    SupplierOffer,
    SupplierWebhookEvent,
)
from app.procurement import ledger
from app.procurement.context import ProcurementContext
from app.procurement.service import SupplierRef, make_supplier, refresh_balance
from app.suppliers.base import (
    CARD_FIELD_SEP,
    JobSpec,
    OfferSpec,
    Outcome,
    Result,
    Supplier,
    clean_detail,
    job_idempotency_key,
)

log = logging.getLogger("app.procurement.engine")

TOPIC_REQUESTED = "procure.requested"
TOPIC_EVENT = "supplier.event"
TOPIC_FULFILLED = "procure.fulfilled"
TOPIC_FAILED = "procure.failed"
CONSUMED = (TOPIC_REQUESTED, TOPIC_EVENT)
_CDK_TERMINAL = frozenset({"consumed", "disabled"})  # ZovoCard CDK statuses that do not move on
UPSTREAM_MODES = ("upstream_cdk", "upstream_vcc")
MODE_KIND = {"upstream_cdk": "cdk", "upstream_vcc": "vcc"}  # delivery mode -> offer / job kind
# vendor status recorded on a job: what a fresh purchase looks like / what a void leaves
ISSUED_STATUS = {"cdk": "unused", "vcc": "active"}
VOIDED_STATUS = {"cdk": "disabled", "vcc": "deleted"}
ACTIVE = ("queued", "submitting", "accepted_pending")
WORK = (*ACTIVE, "void_pending")  # states the poller leases (void_pending: disable upstream)
POLL_STEPS = (15, 30, 60, 120, 300, 600, 900)  # seconds; capped at 15 min (spec §6)
MAX_RETRIES = 10
MAX_RECONCILE = 40
STUCK_AFTER = timedelta(minutes=30)
# availability() reasons that mean "this plan cannot be bought" (anything else = lookup failed)
PLAN_REFUSALS = frozenset(
    {
        "not_purchasable",
        "invalid_offer",
        "cdk_gpt_only",
        "kind_not_supported_m1",
        "amount_out_of_range",
    }
)
OUTBOX_MAX_ATTEMPTS = 8


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class OrderRow:
    id: uuid.UUID
    tenant_id: uuid.UUID
    status: str
    delivery_mode: str
    qty: int
    product_id: uuid.UUID | None
    variant_id: uuid.UUID | None


@dataclass(frozen=True)
class OfferRow:
    id: uuid.UUID
    supplier_id: uuid.UUID
    kind: str
    params: dict[str, Any]
    ref: SupplierRef
    breaker_kind: str | None = None  # 'hard' | 'balance' while the supplier's breaker is open
    balance_at: datetime | None = None  # last good balance reading of the supplier


def request_hash(kind: str, params: dict[str, Any]) -> str:
    """Pins the request a job sends: a resubmit/replay under the same key must not change it."""
    raw = json.dumps({"kind": kind, "params": params}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


async def load_order(db: AsyncSession, order_id: uuid.UUID) -> OrderRow | None:
    row = (
        await db.execute(
            select(
                Order.id,
                Order.tenant_id,
                Order.status,
                Order.delivery_mode,
                Order.qty,
                Order.product_id,
                Order.variant_id,
            ).where(Order.id == order_id)
        )
    ).first()
    return OrderRow(*row) if row else None


async def _routing(db: AsyncSession, order: OrderRow) -> str:
    if order.variant_id is not None:
        r = await db.scalar(
            select(ProductVariant.procurement_routing).where(ProductVariant.id == order.variant_id)
        )
    else:
        r = await db.scalar(
            select(Product.procurement_routing).where(Product.id == order.product_id)
        )
    return r or "priority"


class HasRedis(Protocol):
    redis: Any


async def candidates(
    ctx: HasRedis,
    db: AsyncSession,
    order: OrderRow,
    exclude: list[uuid.UUID] | tuple[uuid.UUID, ...] = (),
    *,
    balance_open: bool = False,
) -> list[OfferRow]:
    """Enabled offers of the order mode's kind (``cdk`` / ``vcc``) of enabled suppliers with a
    closed breaker for this exact target (variant offers for variant orders, variant-less offers
    otherwise), in failover order.
    ``balance_open`` also lists suppliers held by the self-closing balance breaker (never a hard
    one): their queued jobs wait for the balance instead of failing."""
    variant_match = (
        SupplierOffer.variant_id == order.variant_id
        if order.variant_id is not None
        else SupplierOffer.variant_id.is_(None)
    )
    stmt = (
        select(
            SupplierOffer.id,
            SupplierOffer.supplier_id,
            SupplierOffer.kind,
            SupplierOffer.params,
            SupplierAccount.code,
            SupplierAccount.adapter,
            SupplierAccount.env,
            SupplierAccount.health,
        )
        .join(SupplierAccount, SupplierAccount.id == SupplierOffer.supplier_id)
        .where(
            SupplierOffer.tenant_id == order.tenant_id,
            SupplierOffer.product_id == order.product_id,
            variant_match,
            SupplierOffer.enabled.is_(True),
            SupplierOffer.kind == MODE_KIND.get(order.delivery_mode, "cdk"),
            SupplierAccount.enabled.is_(True),
            SupplierAccount.breaker_open.is_(False)
            if not balance_open
            else (
                SupplierAccount.breaker_open.is_(False)
                | (SupplierAccount.breaker_kind == "balance")
            ),
        )
        .order_by(SupplierOffer.priority, SupplierAccount.priority, SupplierOffer.created_at)
    )
    if exclude:
        stmt = stmt.where(SupplierOffer.id.not_in(list(exclude)))
    rows = [
        OfferRow(
            r.id,
            r.supplier_id,
            r.kind,
            dict(r.params or {}),
            SupplierRef(r.supplier_id, order.tenant_id, r.code, r.adapter, r.env, r.health),
        )
        for r in (await db.execute(stmt)).all()
    ]
    if len(rows) > 1 and await _routing(db, order) == "round_robin":
        target = f"{order.product_id}:{order.variant_id or '-'}"
        try:
            n = int(await ctx.redis.incr(f"rl:proc:rr:{target}"))
            await ctx.redis.expire(f"rl:proc:rr:{target}", 30 * 86400)
        except Exception:  # Redis down: priority order is a safe fallback
            n = 0
        k = n % len(rows)
        rows = rows[k:] + rows[:k]
    return rows


async def pick_candidates(
    ctx: HasRedis,
    db: AsyncSession,
    order: OrderRow,
    exclude: list[uuid.UUID] | tuple[uuid.UUID, ...] = (),
) -> list[OfferRow]:
    """Healthy sources first; only when none is left, sources paused by a low balance (their
    jobs back off until the balance recovers)."""
    return await candidates(ctx, db, order, exclude) or await candidates(
        ctx, db, order, exclude, balance_open=True
    )


async def _transition(
    db: AsyncSession,
    job: ProcurementJob,
    to: str,
    reason: str,
    *,
    severity: str = "info",
    actor_id: uuid.UUID | None = None,
    actor_role: str = "system",
    ip: str | None = None,
) -> None:
    before = {"state": job.state, "step": job.step}
    job.state = to
    await audit(
        db,
        tenant_id=job.tenant_id,
        action="procurement.job_state",
        actor_id=actor_id,
        actor_role=actor_role,
        target=f"procurement_job:{job.id}",
        before=before,
        after={"state": to, "step": job.step},
        detail={
            "reason": clean_detail(reason),
            "order_id": str(job.order_id),
            "unit": job.unit_no,
            "supplier_id": str(job.supplier_id) if job.supplier_id else None,
            "error_code": job.error_code,
        },
        severity=severity,
        ip=ip,
    )


def _emit(db: AsyncSession, tenant_id: uuid.UUID, topic: str, key: str, payload: dict) -> None:
    db.add(Outbox(tenant_id=tenant_id, topic=topic, key=key[:128], payload=payload))


async def _needs_manual(
    db: AsyncSession, job: ProcurementJob, reason: str, *, notify_order: bool = True
) -> None:
    job.error_code = clean_detail(reason)
    job.next_poll_at = None
    await _transition(db, job, "needs_manual", reason, severity="warning")
    if notify_order:
        _emit(
            db,
            job.tenant_id,
            TOPIC_FAILED,
            f"failed:{job.order_id}:{job.id}",
            {"order_id": str(job.order_id), "job_id": str(job.id), "reason": job.error_code},
        )


def _workable(job: ProcurementJob) -> bool:
    """The poller leases a job in a work state, or a closed job with a pending ``recover``."""
    return job.state in WORK or job.recover


async def lock_job_set(
    db: AsyncSession, tenant_id: uuid.UUID, order_id: uuid.UUID, *, nowait: bool = False
) -> list[ProcurementJob]:
    """Lock EVERY job of an order in one ordered statement (L4). Lock order everywhere is
    job set -> order -> supplier; the engine cannot lock orders (no UPDATE grant), the API
    paths that already hold the order (refund, close) take the job set with ``nowait`` and
    answer 409 ``procurement_busy`` instead of waiting, so no wait cycle can form."""
    stmt = (
        select(ProcurementJob)
        .where(ProcurementJob.tenant_id == tenant_id, ProcurementJob.order_id == order_id)
        .order_by(ProcurementJob.unit_no, ProcurementJob.id)
        .with_for_update(nowait=nowait)
    )
    try:
        async with db.begin_nested():
            return list(await db.scalars(stmt))
    except DBAPIError as exc:
        orig = exc.orig
        code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
        if nowait and code == "55P03":  # lock_not_available
            raise Conflict(
                "Procurement is working on this order right now; try again in a moment",
                code="procurement_busy",
            ) from None
        raise


async def lock_job(
    db: AsyncSession, tenant_id: uuid.UUID, job_id: uuid.UUID
) -> ProcurementJob | None:
    """Lock the job (and, in the same ordered statement, its siblings)."""
    order_id = await db.scalar(
        select(ProcurementJob.order_id).where(
            ProcurementJob.id == job_id, ProcurementJob.tenant_id == tenant_id
        )
    )
    if order_id is None:
        return None
    jobs = await lock_job_set(db, tenant_id, order_id)
    return next((j for j in jobs if j.id == job_id), None)


async def close_job(
    db: AsyncSession,
    job: ProcurementJob,
    reason: str,
    *,
    actor_id: uuid.UUID | None = None,
    actor_role: str = "system",
    ip: str | None = None,
) -> None:
    """Close a job whose order will not be delivered (refund, close, no longer ``paid``).

    Never sent -> ``failed``. A job that may have reached the supplier is never dropped: it is
    ``failed`` with ``recover`` set, and the engine then asks once under the SAME key to learn the
    reference and disables the code (``voided``), or ends in ``needs_manual`` flagged as maybe
    charged. Touches only columns ``app_api`` may update."""
    reached = bool(job.maybe_sent or job.upstream_ref)
    job.error_code = clean_detail(reason)
    job.step, job.attempts = "done", 0
    job.recover = reached
    job.next_poll_at = utcnow() if reached else None
    await _transition(
        db,
        job,
        "failed",
        reason,
        severity="warning" if reached else "info",
        actor_id=actor_id,
        actor_role=actor_role,
        ip=ip,
    )


CLOSABLE = ("queued", "submitting", "accepted_pending", "needs_manual")


async def close_order_jobs(
    db: AsyncSession,
    order: Order,
    *,
    actor_id: uuid.UUID | None,
    actor_role: str,
    ip: str | None,
    reason: str,
) -> int:
    """Refund / cancel of an upstream order: close its open jobs (caller holds the order lock)."""
    if order.delivery_mode not in UPSTREAM_MODES:
        return 0
    jobs = await lock_job_set(db, order.tenant_id, order.id, nowait=True)
    undelivered = order.status in UNDELIVERED_ORDER
    n = 0
    for job in jobs:
        if job.state in CLOSABLE:
            await close_job(db, job, reason, actor_id=actor_id, actor_role=actor_role, ip=ip)
            n += 1
        elif job.state == "fulfilled" and undelivered:
            # a code bound but never handed over (N5): never leave it valid at the supplier
            await _void_fulfilled(db, job, reason, actor_id, actor_role, ip)
            n += 1
    return n


UNDELIVERED_ORDER = ("paid", "manual_review")  # nothing handed over yet


async def _void_fulfilled(
    db: AsyncSession,
    job: ProcurementJob,
    reason: str,
    actor_id: uuid.UUID | None,
    actor_role: str,
    ip: str | None,
) -> None:
    """``fulfilled`` but undelivered unit of a refunded order: disable the code at the supplier
    (``void_pending``; ``voided`` at once when there is nothing to disable) and void the sealed
    ``card_stock`` row. Delivered units are never touched here (after-sales handles them)."""
    job.error_code = clean_detail(reason)
    if job.upstream_ref and job.offer_id is not None:
        job.step, job.attempts, job.next_poll_at = "void", 0, utcnow()
        to = "void_pending"
    else:
        job.step, job.next_poll_at = "done", None
        to = "voided"
    await _transition(
        db, job, to, reason, severity="warning", actor_id=actor_id, actor_role=actor_role, ip=ip
    )
    # after the transition: the database only lets the API void a sold code of a job in a void
    # state (card_stock_guard, migration 0027)
    if job.card_stock_ids:
        voided = await db.execute(
            update(CardStock)
            .where(
                CardStock.tenant_id == job.tenant_id,
                CardStock.id.in_(list(job.card_stock_ids)),
                CardStock.status != "void",
                CardStock.order_id == job.order_id,  # only this order's own codes
            )
            .values(
                status="void",
                void_reason=clean_detail(f"procurement_void:{reason}", 64),
                voided_at=utcnow(),
                voided_by=actor_id,
            )
        )
        await audit(
            db,
            tenant_id=job.tenant_id,
            action="procurement.cards_voided",
            actor_id=actor_id,
            actor_role=actor_role,
            target=f"procurement_job:{job.id}",
            detail={"cards": int(voided.rowcount or 0), "order_id": str(job.order_id)},  # type: ignore[attr-defined]
            severity="warning",
            ip=ip,
        )


# ------------------------------------------------------------------ job creation
async def handle_requested(ctx: ProcurementContext, db: AsyncSession, payload: dict) -> None:
    """One job per order unit, committed by the caller BEFORE any supplier call."""
    order = await load_order(db, uuid.UUID(str(payload["order_id"])))
    if order is None or order.status != "paid" or order.delivery_mode not in UPSTREAM_MODES:
        log.info("procure.requested ignored (order missing, not paid or not upstream)")
        return
    if order.product_id is None:
        return
    taken = set(
        await db.scalars(
            select(ProcurementJob.unit_no).where(
                ProcurementJob.order_id == order.id, ProcurementJob.state != "voided"
            )
        )
    )
    now = utcnow()
    failed = False
    for unit in range(1, order.qty + 1):
        if unit in taken:
            continue
        jid = uuid.uuid4()
        cands = await pick_candidates(ctx, db, order)
        offer = cands[0] if cands else None
        job = ProcurementJob(
            id=jid,
            tenant_id=order.tenant_id,
            order_id=order.id,
            unit_no=unit,
            offer_id=offer.id if offer else None,
            supplier_id=offer.supplier_id if offer else None,
            kind=MODE_KIND[order.delivery_mode],
            state="queued",
            step="submit",
            idempotency_key=job_idempotency_key(jid.hex, "submit"),
            request_hash=request_hash(offer.kind, offer.params) if offer else None,
            attempts=0,
            next_poll_at=now,
            card_stock_ids=[],
            tried_offer_ids=[],
        )
        try:
            async with db.begin_nested():
                db.add(job)
                await db.flush()
        except IntegrityError:  # a concurrent request created this unit
            continue
        await audit(
            db,
            tenant_id=order.tenant_id,
            action="procurement.job_created",
            target=f"procurement_job:{jid}",
            after={"state": "queued", "unit": unit, "offer_id": str(offer.id) if offer else None},
            detail={"order_id": str(order.id)},
        )
        if offer is None:
            await _needs_manual(db, job, "no_source", notify_order=False)
            failed = True
    if failed:
        _emit(
            db,
            order.tenant_id,
            TOPIC_FAILED,
            f"failed:{order.id}:no_source",
            {"order_id": str(order.id), "reason": "no_source"},
        )


# ------------------------------------------------------------------ supplier events
CARD_EVENTS = ("card_operation", "card_transaction", "card_fee", "card_3ds_otp")


def _leased(job: ProcurementJob, now: datetime) -> bool:
    return job.lease_until is not None and job.lease_until > now


async def _card_event(db: AsyncSession, ev: SupplierWebhookEvent, now: datetime) -> None:
    """A card webhook is a hint: it records the vendor's last operation status on the job that
    owns the card (``operation:status``, e.g. ``recharge:success``) and makes an active job due for
    its re-query, which stays the truth. Transactions, fees and 3DS codes change nothing here (3DS
    codes never reach the database at all: the API hands them to the buyer's live channel)."""
    payload = ev.payload or {}
    if ev.kind != "card_operation":
        return
    jobs = list(
        await db.scalars(
            select(ProcurementJob)
            .where(
                ProcurementJob.tenant_id == ev.tenant_id,
                ProcurementJob.supplier_id == ev.supplier_id,
                ProcurementJob.kind == "vcc",
                ProcurementJob.upstream_ref == str(payload["card_id"])[:128],
            )
            .with_for_update()
        )
    )
    op, status = payload.get("operation"), payload.get("status")
    for job in jobs:
        if isinstance(op, str) and isinstance(status, str) and job.upstream_status != "deleted":
            job.upstream_status = clean_detail(f"{op}:{status}", 32)
        if job.state in ACTIVE and not _leased(job, now):
            job.next_poll_at = now


async def handle_event(ctx: ProcurementContext, db: AsyncSession, payload: dict) -> None:
    """A webhook is a hint (spec §6): it updates the last known upstream status and makes a
    matching active job due for an immediate re-query; it never fulfils by itself."""
    ev = await db.scalar(
        select(SupplierWebhookEvent)
        .where(SupplierWebhookEvent.id == uuid.UUID(str(payload["event_id"])))
        .with_for_update()
    )
    if ev is None or ev.processed_at is not None:
        return
    ref = (ev.payload or {}).get("cdk_id")
    now = utcnow()
    if ev.kind in CARD_EVENTS and (ev.payload or {}).get("card_id") is not None:
        await _card_event(db, ev, now)
    elif ev.kind.startswith("cdk.") and ref is not None:
        jobs = list(
            await db.scalars(
                select(ProcurementJob)
                .where(
                    ProcurementJob.supplier_id == ev.supplier_id,
                    ProcurementJob.upstream_ref == str(ref),
                )
                .with_for_update()
            )
        )
        status = (ev.payload or {}).get("cdk_status") or (ev.payload or {}).get("to_status")
        for job in jobs:
            # webhooks arrive out of order: a late non-terminal status never replaces a
            # terminal one (the re-query below stays the truth either way)
            if isinstance(status, str) and job.upstream_status not in _CDK_TERMINAL:
                job.upstream_status = clean_detail(status, 32)
            if job.state in ACTIVE and not _leased(job, now):
                job.next_poll_at = now  # (L6: never pulled forward under a live lease)
    elif ev.kind == "balance_change":
        # cost ledger: the same unique insert path as the balance-logs backfill (ledger.py);
        # card_fee events are never booked (their money arrives as a balance_change)
        await ledger.handle_balance_event(
            db,
            tenant_id=ev.tenant_id,
            supplier_id=ev.supplier_id,
            payload=ev.payload or {},
            received_at=ev.received_at,
        )
    ev.processed_at = utcnow()


# ------------------------------------------------------------------ outbox consumer
async def process_outbox_one(ctx: ProcurementContext) -> bool:
    async with tenant_session(ctx.maker, None) as db:
        row = await db.scalar(
            select(Outbox)
            .where(
                Outbox.processed_at.is_(None),
                Outbox.topic.in_(CONSUMED),
                Outbox.available_at <= func.now(),
            )
            .order_by(Outbox.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            await db.rollback()
            return False
        outbox_id, tenant_id, topic, payload = row.id, row.tenant_id, row.topic, dict(row.payload)
        await set_tenant(db, tenant_id)
        failed: str | None = None
        try:
            async with db.begin_nested():
                if topic == TOPIC_REQUESTED:
                    await handle_requested(ctx, db, payload)
                else:
                    await handle_event(ctx, db, payload)
        except Exception as exc:  # retried with backoff; never kills the loop
            failed = type(exc).__name__
            log.error("procurement outbox job failed id=%s error=%s", outbox_id, failed)
        row = await db.scalar(
            select(Outbox).where(Outbox.id == outbox_id).execution_options(populate_existing=True)
        )
        assert row is not None
        now = utcnow()
        if failed is None:
            row.processed_at = now
        else:
            row.attempts += 1
            row.last_error = failed[:200]
            row.available_at = now + timedelta(seconds=min(300, 2**row.attempts))
            if row.attempts >= OUTBOX_MAX_ATTEMPTS:
                row.processed_at = now
                await audit(
                    db,
                    tenant_id=tenant_id,
                    action="procurement.outbox_dead",
                    target=f"outbox:{outbox_id}",
                    detail={"error": failed, "topic": topic},
                    severity="critical",
                )
        await db.commit()
    return True


# ------------------------------------------------------------------ jobs
@dataclass
class Lease:
    job_id: uuid.UUID
    tenant_id: uuid.UUID
    mode: str  # submit | query | verify | void | recover | dvoid
    state: str
    step: str
    key: str
    spec: JobSpec
    ref: SupplierRef


def _spec(job: ProcurementJob, offer: OfferRow, mode: str) -> JobSpec:
    voiding = mode in ("void", "dvoid")
    return JobSpec(
        job_id=job.id.hex,
        step="void" if voiding else job.step,
        # the vendor caches answers per key: a void never reuses the issue key
        idempotency_key=job_idempotency_key(job.id.hex, "void") if voiding else job.idempotency_key,
        offer=OfferSpec(kind=offer.kind, params=offer.params),  # type: ignore[arg-type]
        upstream_ref=job.upstream_ref,
    )


async def _offer(db: AsyncSession, job: ProcurementJob) -> OfferRow | None:
    if job.offer_id is None:
        return None
    r = (
        await db.execute(
            select(
                SupplierOffer.id,
                SupplierOffer.supplier_id,
                SupplierOffer.kind,
                SupplierOffer.params,
                SupplierOffer.enabled,
                SupplierAccount.code,
                SupplierAccount.adapter,
                SupplierAccount.env,
                SupplierAccount.health,
                SupplierAccount.enabled.label("supplier_enabled"),
                SupplierAccount.breaker_open,
                SupplierAccount.breaker_kind,
                SupplierAccount.last_balance_at,
            )
            .join(SupplierAccount, SupplierAccount.id == SupplierOffer.supplier_id)
            .where(SupplierOffer.id == job.offer_id)
        )
    ).first()
    if r is None:
        return None
    ref = SupplierRef(r.supplier_id, job.tenant_id, r.code, r.adapter, r.env, r.health)
    if not (r.enabled and r.supplier_enabled):
        return None
    kind = (r.breaker_kind or "hard") if r.breaker_open else None
    return OfferRow(r.id, r.supplier_id, r.kind, dict(r.params or {}), ref, kind, r.last_balance_at)


async def _failover(
    ctx: HasRedis,
    db: AsyncSession,
    job: ProcurementJob,
    order: OrderRow,
    reason: str,
    *,
    actor_id: uuid.UUID | None = None,
    actor_role: str = "system",
    ip: str | None = None,
) -> None:
    if job.maybe_sent or job.upstream_ref:
        # another supplier is only ever tried when the first one is known to have done nothing
        await _needs_manual(db, job, f"maybe_sent:{reason}")
        return
    tried = [*list(job.tried_offer_ids or []), *([job.offer_id] if job.offer_id else [])]
    job.tried_offer_ids = tried
    cands = await pick_candidates(ctx, db, order, tried)
    if not cands:
        await _needs_manual(db, job, f"no_source:{reason}")
        return
    nxt = cands[0]
    job.offer_id, job.supplier_id = nxt.id, nxt.supplier_id
    # a fresh key for a supplier that never saw the old one; the old key is never reused
    job.idempotency_key = job_idempotency_key(job.id.hex, f"s{len(tried) + 1}")
    job.request_hash = request_hash(nxt.kind, nxt.params)
    job.step = "submit"
    job.first_sent_at = None
    job.upstream_ref = None
    job.attempts = 0
    job.next_poll_at = utcnow()
    await _transition(
        db, job, "queued", f"failover:{reason}", actor_id=actor_id, actor_role=actor_role, ip=ip
    )


BALANCE_BACKOFF = timedelta(seconds=60)


def _balance_gate(ctx: ProcurementContext, offer: OfferRow, now: datetime) -> str | None:
    """Why no NEW purchase may start on this supplier right now (M3, fail closed)."""
    if offer.breaker_kind == "balance":
        return "balance_breaker"
    stale = timedelta(seconds=ctx.settings.balance_stale_s)
    if offer.balance_at is None or now - offer.balance_at > stale:
        return "balance_stale"
    return None


def _unsent(job: ProcurementJob) -> None:
    """The supplier answered "nothing happened" for the current key (a refused or not-executed
    write, a verified miss): the job is back to never-sent, so another source may be tried."""
    if job.upstream_ref is None:
        job.maybe_sent, job.first_sent_at = False, None


def _replay_problem(
    ctx: ProcurementContext, job: ProcurementJob, offer: OfferRow, now: datetime
) -> str | None:
    """A replay under the job's key is only safe for the SAME request and while the vendor still
    remembers the key (M5): the pinned ``request_hash`` is enforced in every mode, and a replay
    older than ``replay_max_age_s`` is refused."""
    if not job.request_hash:
        return "request_hash_missing"  # nothing pins what was sent: not replayed blindly (M5)
    if job.request_hash != request_hash(offer.kind, offer.params):
        return "offer_changed"
    if job.first_sent_at is not None and now - job.first_sent_at > timedelta(
        seconds=ctx.settings.replay_max_age_s
    ):
        return "replay_expired"
    return None


async def _stop_recovery(db: AsyncSession, job: ProcurementJob, reason: str) -> None:
    """A recovery that cannot finish: the job goes to ``needs_manual`` (flagged as maybe charged
    through ``maybe_sent`` / ``upstream_ref``); a hand-fulfilled job only gets a critical audit."""
    job.recover = False
    job.next_poll_at = None
    if job.state == "failed":
        await _needs_manual(db, job, reason, notify_order=False)
    else:
        await audit(
            db,
            tenant_id=job.tenant_id,
            action="procurement.recover_failed",
            target=f"procurement_job:{job.id}",
            detail={"reason": clean_detail(reason), "order_id": str(job.order_id)},
            severity="critical",
        )


RECOVER_BLOCKED_MAX = 20  # ~1.5 h of waiting for the kill switch to be lifted


async def _supplier_blocked(db: AsyncSession, job: ProcurementJob) -> bool:
    """The job's supplier is disabled or its ``hard`` breaker is open: no call that may buy."""
    r = (
        await db.execute(
            select(
                SupplierAccount.enabled, SupplierAccount.breaker_open, SupplierAccount.breaker_kind
            )
            .join(SupplierOffer, SupplierOffer.supplier_id == SupplierAccount.id)
            .where(SupplierOffer.id == job.offer_id)
        )
    ).first()
    if r is None:
        return True
    return (not r.enabled) or bool(r.breaker_open and (r.breaker_kind or "hard") == "hard")


async def _audit_manual_fulfil(db: AsyncSession, job: ProcurementJob) -> None:
    await audit(
        db,
        tenant_id=job.tenant_id,
        action="procurement.recovered_after_manual_fulfil",
        target=f"procurement_job:{job.id}",
        detail={"order_id": str(job.order_id), "upstream_ref_recorded": bool(job.upstream_ref)},
        severity="critical",
    )


async def _lease_detached(
    ctx: ProcurementContext, db: AsyncSession, job: ProcurementJob, now: datetime
) -> Lease | None:
    """A closed job with ``recover``: learn the reference by one same-key query (never sealing
    anything), then disable the code under the void key."""
    offer = await _offer_any(db, job)
    if offer is None:
        await _stop_recovery(db, job, "offer_missing")
        await db.commit()
        return None
    mode = "dvoid" if job.upstream_ref else "recover"
    if mode == "dvoid" and job.state == "fulfilled":
        # a hand-fulfilled job: staff may have pasted exactly this code, so it is never disabled
        # behind their back (N2); disabling stays an explicit staff ``void``
        job.recover, job.next_poll_at = False, None
        await _audit_manual_fulfil(db, job)
        await db.commit()
        return None
    if mode == "recover" and (problem := _replay_problem(ctx, job, offer, now)):
        await _stop_recovery(db, job, problem)  # no replay of a changed / too old request
        await db.commit()
        return None
    if mode == "recover" and await _supplier_blocked(db, job):
        # the replay can buy (one issue fee at most): it honours the kill switches (N1). The job
        # waits for staff to re-enable the supplier / reset its breaker, then ends in
        # needs_manual (maybe charged: maybe_sent stays set).
        job.attempts += 1
        if job.attempts > RECOVER_BLOCKED_MAX:
            await _stop_recovery(db, job, "recover_blocked")
        else:
            job.error_code = "recover_blocked"
            job.next_poll_at = now + _backoff(min(job.attempts, 6), None)
        await db.commit()
        return None
    job.attempts += 1
    job.next_poll_at = job.lease_until = now + timedelta(seconds=ctx.settings.job_lease_s)
    lease = Lease(
        job.id, job.tenant_id, mode, job.state, job.step, job.idempotency_key,
        _spec(job, offer, mode), offer.ref,
    )  # fmt: skip
    await db.commit()
    return lease


async def _lease(ctx: ProcurementContext, job_id: uuid.UUID, tenant_id: uuid.UUID) -> Lease | None:
    now = utcnow()
    async with tenant_session(ctx.maker, tenant_id) as db:
        job = await db.scalar(
            select(ProcurementJob)
            .where(ProcurementJob.id == job_id)
            .with_for_update(skip_locked=True)
        )
        if job is None or not _workable(job) or job.next_poll_at is None or job.next_poll_at > now:
            await db.rollback()
            return None
        if job.state not in WORK:  # closed job with a pending recovery
            return await _lease_detached(ctx, db, job, now)
        order = await load_order(db, job.order_id)
        assert order is not None
        if job.state == "void_pending":
            mode = "void"
        elif job.state == "queued" or (
            job.state == "submitting" and job.step in ("submit", "ready")
        ):
            mode = "submit"
        elif job.state == "submitting" and job.step == "verify":
            mode = "verify"
        else:
            mode = "query"
        if mode != "void" and order.status != "paid":
            # no purchase and no code binding for an order that is not (or no longer) paid: a
            # held order waits for staff, a closed one ends the job (a possible code is voided)
            if order.status == "manual_review":
                await _needs_manual(db, job, "order_not_paid", notify_order=False)
            else:
                await close_job(db, job, "order_not_paid")
            await db.commit()
            return None
        offer = await _offer(db, job)
        if offer is not None and offer.breaker_kind == "hard":
            offer = None  # a refusal-stopped supplier is only asked about work it may hold
        if mode == "submit" and not job.maybe_sent:
            if offer is None:  # never sent with this offer: fail over without a query
                await _failover(ctx, db, job, order, "offer_unusable")
                await db.commit()
                return None
            if not job.request_hash:
                job.request_hash = request_hash(offer.kind, offer.params)  # never sent: pin now
            elif job.request_hash != request_hash(offer.kind, offer.params):
                await _needs_manual(db, job, "offer_changed")
                await db.commit()
                return None
            if gate := _balance_gate(ctx, offer, now):
                # a low (breaker) or unknown (stale reading) balance: wait, never fail over or
                # give up; the job resumes by itself when the balance is known and sufficient
                if gate == "balance_breaker":
                    # nothing was sent, so another usable source is safe to try right away (N8)
                    tried = [*list(job.tried_offer_ids or []), job.offer_id]
                    if await candidates(ctx, db, order, tried):
                        await _failover(ctx, db, job, order, gate)
                        await db.commit()
                        return None
                job.error_code = gate
                job.next_poll_at = now + BALANCE_BACKOFF
                await _alert_if_stuck(db, job, now)  # a long park is still a stuck job
                await db.commit()
                return None
            if job.step != "ready":
                mode = "precheck"  # read-only availability check before the first send (H2)
            else:
                if job.state == "queued":
                    await _transition(db, job, "submitting", "submit")
                job.step = "sent"  # from here on a crash means "maybe sent": reconcile
                job.maybe_sent = True
                job.first_sent_at = now
        else:
            if offer is None:
                # a job that may have reached a supplier is never moved automatically: ask it
                # through its own supplier even if the offer was disabled meanwhile
                offer = await _offer_any(db, job)
                if offer is None:
                    await _needs_manual(db, job, "offer_missing")
                    await db.commit()
                    return None
            if mode != "void" and (problem := _replay_problem(ctx, job, offer, now)):
                await _needs_manual(db, job, problem)  # maybe charged: never replayed
                await db.commit()
                return None
            if mode == "submit":
                job.step = "sent"  # same-key resubmit after a reconcile found nothing
            elif mode == "query" and job.state == "submitting":
                job.step = "reconcile"  # leased earlier and never finished: maybe sent
        job.attempts += 1
        job.next_poll_at = job.lease_until = now + timedelta(seconds=ctx.settings.job_lease_s)
        lease = Lease(
            job.id, job.tenant_id, mode, job.state, job.step, job.idempotency_key,
            _spec(job, offer, mode), offer.ref,
        )  # fmt: skip
        await db.commit()
    return lease


async def _offer_any(db: AsyncSession, job: ProcurementJob) -> OfferRow | None:
    r = (
        await db.execute(
            select(
                SupplierOffer.id,
                SupplierOffer.supplier_id,
                SupplierOffer.kind,
                SupplierOffer.params,
                SupplierAccount.code,
                SupplierAccount.adapter,
                SupplierAccount.env,
                SupplierAccount.health,
            )
            .join(SupplierAccount, SupplierAccount.id == SupplierOffer.supplier_id)
            .where(SupplierOffer.id == job.offer_id)
        )
    ).first()
    if r is None:
        return None
    ref = SupplierRef(r.supplier_id, job.tenant_id, r.code, r.adapter, r.env, r.health)
    return OfferRow(r.id, r.supplier_id, r.kind, dict(r.params or {}), ref)


def engine_supplier(ctx: ProcurementContext, ref: SupplierRef) -> Supplier:
    key = (ref.adapter, ref.code, ref.env)
    sup = ctx.suppliers.get(key)
    if sup is None:
        sup = make_supplier(ctx, ref)
        ctx.suppliers[key] = sup
    return sup


async def _availability_result(sup: Supplier, offer: OfferSpec) -> Result:
    """Read-only availability against a FRESH catalog (adapters without a cache just ask)."""
    fn = getattr(sup, "availability_fresh", None) or sup.availability
    av = await fn(offer)
    if av.available is True:
        return Result(Outcome.OK)
    reason = clean_detail(av.reason or "unavailable")
    # only the catalog's own "no" is definite; a failed lookup says nothing about the plan
    definite = reason in PLAN_REFUSALS
    return Result(Outcome.UNAVAILABLE if definite else Outcome.RETRYABLE, error_code=reason)


async def _call(ctx: ProcurementContext, lease: Lease) -> Result:
    try:
        sup = engine_supplier(ctx, lease.ref)
        if lease.mode == "precheck":
            return await _availability_result(sup, lease.spec.offer)
        if lease.mode == "submit":
            # an ambiguous write stays ambiguous whatever the catalog says afterwards: a plan
            # can sell out or be paused BECAUSE this write ran, so "not offered now" proves
            # nothing about it (re-audit F1). Only the fresh pre-check keeps a paused plan away.
            return await sup.submit(lease.spec)
        if lease.mode in ("void", "dvoid"):
            return await sup.void(lease.spec)
        return await sup.query(lease.spec)  # query / verify / recover: same-key replay
    except Exception as exc:  # unknown failure: if it may have been sent, reconcile
        log.error("supplier call failed job=%s error=%s", lease.job_id, type(exc).__name__)
        if lease.mode in ("submit", "void", "dvoid"):
            return Result(Outcome.AMBIGUOUS, error_code="internal_error")
        return Result(Outcome.RETRYABLE, error_code="internal_error")


def _backoff(attempts: int, retry_after: timedelta | None) -> timedelta:
    base = timedelta(seconds=min(900, 5 * 2 ** min(attempts, 8)))
    return max(base, retry_after or timedelta(0))


async def _open_breaker(db: AsyncSession, job: ProcurementJob, reason: str) -> None:
    sup = await db.scalar(
        select(SupplierAccount).where(SupplierAccount.id == job.supplier_id).with_for_update()
    )
    if sup is None:
        return
    if sup.breaker_open and sup.breaker_kind == "hard":
        return
    # a refusal outranks a low-balance breaker: from here on only staff can close it
    sup.breaker_open, sup.breaker_kind = True, "hard"
    sup.breaker_reason, sup.breaker_at = clean_detail(reason), utcnow()
    await audit(
        db,
        tenant_id=job.tenant_id,
        action="supplier.breaker_opened",
        target=f"supplier:{sup.id}",
        after={"breaker_open": True, "reason": sup.breaker_reason},
        severity="critical",
    )


async def emit_if_complete(db: AsyncSession, order: OrderRow) -> bool:
    """When every unit of the order has a fulfilled job, emit ``procure.fulfilled`` (delivery
    then delivers; it never re-delivers a card, so a repeated event is harmless)."""
    siblings = list(
        await db.scalars(
            select(ProcurementJob)
            .where(ProcurementJob.order_id == order.id, ProcurementJob.state != "voided")
            .order_by(ProcurementJob.unit_no)
            .with_for_update()
        )
    )
    if len(siblings) == order.qty and all(s.state == "fulfilled" for s in siblings):
        ids = [str(c) for s in siblings for c in s.card_stock_ids]
        _emit(
            db,
            order.tenant_id,
            TOPIC_FULFILLED,
            f"fulfilled:{order.id}",
            {"order_id": str(order.id), "card_stock_ids": ids},
        )
        return True
    return False


async def _fulfil(
    ctx: ProcurementContext, db: AsyncSession, job: ProcurementJob, order: OrderRow, res: Result
) -> None:
    # the reference and the cost are recorded before anything can refuse the code (L1): a code
    # the supplier issued must stay findable so it can be disabled
    job.upstream_ref = res.upstream_ref or job.upstream_ref
    if res.cost is not None:
        job.cost, job.currency = res.cost.amount, res.cost.currency
    if order.status != "paid":
        # the order was refunded / held while the supplier call ran: never bind the code to it
        # (the answer is dropped here, never stored); the code is disabled at the supplier
        job.error_code = "order_not_paid"
        if job.upstream_ref:
            job.step, job.attempts, job.next_poll_at = "void", 0, utcnow()
            await _transition(db, job, "void_pending", "order_not_paid", severity="warning")
        else:
            await _needs_manual(db, job, "order_not_paid_no_ref", notify_order=False)
        return
    if len(res.secrets) != 1 or ctx.sealer is None or order.product_id is None:
        # never store or log what we got; a CDK replay returns it again once fixed
        job.error_code = "seal_unavailable" if ctx.sealer is None else "secret_count"
        if job.attempts > MAX_RETRIES:
            await _needs_manual(db, job, job.error_code)
        else:
            job.step = "reconcile"
            job.next_poll_at = utcnow() + _backoff(job.attempts, None)
        return
    sealer = ctx.sealer
    card_id = uuid.uuid4()
    tid = str(order.tenant_id)
    code = res.secrets[0]
    # a card is ONE sealed secret ``<pan>----<MM/YY>----<cvv>`` (first field = PAN: staff find
    # a card by its number without decrypting it); a CDK code is the whole secret
    first = code.split(CARD_FIELD_SEP, 1)[0] if job.kind == "vcc" else code
    values = {
        "id": card_id,
        "tenant_id": order.tenant_id,
        "product_id": order.product_id,
        "variant_id": order.variant_id,
        "blob": sealer.seal(code, CardContext(tid, str(order.product_id), str(card_id))),
        "fingerprint": sealer.fingerprint(tid, code),
        "lookup_fp": sealer.lookup_fingerprint(tid, first),
        "kid": sealer.active_kid,
        "status": "sold",
        "order_id": order.id,
    }
    del code, first
    try:
        async with db.begin_nested():
            # Core insert, no RETURNING: procurement has no SELECT on card_stock at all
            await db.execute(insert(CardStock.__table__).values(**values))
    except IntegrityError:
        await _needs_manual(db, job, "duplicate_code")
        return
    job.card_stock_ids = [card_id]
    job.upstream_status = ISSUED_STATUS.get(job.kind, "unused")
    job.error_code = None
    job.step = "done"
    job.next_poll_at = None
    await _transition(db, job, "fulfilled", "supplier_ok")
    await emit_if_complete(db, order)


async def _apply_void(db: AsyncSession, job: ProcurementJob, res: Result, now: datetime) -> None:
    """``void_pending`` -> ``voided`` once the supplier disabled the code; a refusal ("already
    redeemed", unknown code) puts the job back where it was and tells staff why."""
    o = res.outcome
    if o is Outcome.OK:
        job.step, job.next_poll_at, job.error_code = "done", None, None
        job.upstream_status = VOIDED_STATUS.get(job.kind, "disabled")
        await _transition(db, job, "voided", "void_ok")
    elif o is Outcome.FATAL or job.attempts > MAX_RETRIES:
        job.error_code = clean_detail(f"void_rejected:{res.error_code or 'unresolved'}")
        job.step, job.next_poll_at = "done", None
        back = "fulfilled" if job.card_stock_ids else "needs_manual"
        await _transition(db, job, back, "void_rejected", severity="warning")
    else:  # RETRYABLE / AMBIGUOUS / UNAVAILABLE: the same void key is replayed later
        job.error_code = clean_detail(res.error_code or "void_retry")
        job.next_poll_at = now + _backoff(job.attempts, res.retry_after)


async def _set_check(db: AsyncSession, job: ProcurementJob, ok: bool) -> None:
    """The offer's last availability verdict (the storefront hides an unchecked source)."""
    if job.offer_id is not None:
        await db.execute(
            update(SupplierOffer)
            .where(SupplierOffer.tenant_id == job.tenant_id, SupplierOffer.id == job.offer_id)
            .values(last_check_ok=ok, last_check_at=utcnow())
        )


async def _apply_precheck(
    ctx: ProcurementContext,
    db: AsyncSession,
    job: ProcurementJob,
    order: OrderRow,
    res: Result,
    now: datetime,
) -> None:
    """Nothing was sent. Available -> ready to submit; the catalog says no -> next source (or
    ``needs_manual`` without one) and the offer is marked unchecked; a failed lookup is retried."""
    o = res.outcome
    if o is Outcome.OK:
        # attempts is NOT reset: a submit that keeps answering RETRYABLE must reach MAX_RETRIES (N7)
        job.step, job.error_code, job.next_poll_at = "ready", None, now
        await _set_check(db, job, True)
    elif o is Outcome.UNAVAILABLE:
        await _set_check(db, job, False)
        await _failover(ctx, db, job, order, "plan_unavailable")
    else:
        job.error_code = clean_detail(res.error_code or "precheck_failed")
        if job.attempts > MAX_RETRIES:
            await _needs_manual(db, job, "precheck_failed")
        else:
            job.next_poll_at = now + _backoff(job.attempts, res.retry_after)


async def _apply_detached(
    db: AsyncSession, job: ProcurementJob, lease: Lease, res: Result, now: datetime
) -> None:
    """Recovery of a closed job: ``recover`` learns the reference (the answer's code is dropped,
    never sealed), ``dvoid`` disables the code. Never changes who the code is delivered to."""
    o = res.outcome
    if lease.mode == "recover":
        if o is Outcome.OK:
            job.upstream_ref = res.upstream_ref or job.upstream_ref
            if res.cost is not None:
                job.cost, job.currency = res.cost.amount, res.cost.currency
            job.upstream_status = ISSUED_STATUS.get(job.kind, "unused")
            if not job.upstream_ref:
                await _stop_recovery(db, job, "recover_no_ref")
                return
            if job.state == "fulfilled":  # hand-fulfilled: record the reference, never disable
                job.recover, job.next_poll_at = False, None
                await _audit_manual_fulfil(db, job)
                return
            job.attempts, job.next_poll_at = 0, now  # next: disable it
            await audit(
                db,
                tenant_id=job.tenant_id,
                action="procurement.job_recovered",
                target=f"procurement_job:{job.id}",
                detail={"order_id": str(job.order_id), "state": job.state},
                severity="warning",
            )
        elif res.error_code == "IDEMPOTENCY_CONFLICT":
            # the key HAS been used (the first request may have run): never "nothing found" (N6)
            await _stop_recovery(db, job, "IDEMPOTENCY_CONFLICT")
        elif o in (Outcome.UNAVAILABLE, Outcome.FATAL):  # the supplier holds nothing for the key
            job.recover, job.next_poll_at = False, None
            _unsent(job)
            await audit(
                db,
                tenant_id=job.tenant_id,
                action="procurement.recover_nothing_found",
                target=f"procurement_job:{job.id}",
                detail={"order_id": str(job.order_id), "error_code": clean_detail(res.error_code)},
            )
        elif job.attempts > MAX_RECONCILE:
            await _stop_recovery(db, job, "unresolved")
        else:
            job.error_code = clean_detail(res.error_code or "recover_retry")
            job.next_poll_at = now + _backoff(min(job.attempts, 3), res.retry_after)
        return
    # dvoid
    if o is Outcome.OK:
        job.recover, job.next_poll_at = False, None
        job.upstream_status = VOIDED_STATUS.get(job.kind, "disabled")
        if job.state == "failed":
            await _transition(db, job, "voided", "recovered_void", severity="warning")
    elif o is Outcome.FATAL or job.attempts > MAX_RETRIES:
        await _stop_recovery(db, job, f"void_rejected:{res.error_code or 'unresolved'}")
    else:
        job.error_code = clean_detail(res.error_code or "void_retry")
        job.next_poll_at = now + _backoff(job.attempts, res.retry_after)


async def _alert_if_stuck(db: AsyncSession, job: ProcurementJob, now: datetime) -> None:
    if (
        job.state in ACTIVE
        and job.alerted_at is None
        and job.created_at is not None
        and now - job.created_at > STUCK_AFTER
    ):
        job.alerted_at = now
        await audit(
            db,
            tenant_id=job.tenant_id,
            action="procurement.job_stuck",
            target=f"procurement_job:{job.id}",
            detail={"state": job.state, "order_id": str(job.order_id)},
            severity="warning",
        )


def _record_reference(job: ProcurementJob, res: Result, mode: str) -> None:
    """A multi-step purchase (a card) knows its supplier reference before it is finished: record it
    on EVERY answer that carries one (ambiguous, pending, refused after the open), so the job is
    never "nothing happened" once the supplier holds something for it and a void can find it."""
    if mode in ("submit", "query", "verify", "recover") and res.upstream_ref:
        job.upstream_ref = job.upstream_ref or res.upstream_ref
    if res.card_mask and not job.card_mask:
        job.card_mask = res.card_mask[:24]


async def _apply(ctx: ProcurementContext, lease: Lease, res: Result) -> None:
    now = utcnow()
    async with tenant_session(ctx.maker, lease.tenant_id) as db:
        job = await lock_job(db, lease.tenant_id, lease.job_id)
        if (
            job is None
            or job.state != lease.state
            or job.step != lease.step
            or job.idempotency_key != lease.key
        ):
            await db.rollback()  # staff acted meanwhile: their action wins
            return
        job.lease_until = None
        order = await load_order(db, job.order_id)
        assert order is not None
        o = res.outcome
        if lease.mode == "void":
            await _apply_void(db, job, res, now)
            await db.commit()
            return
        if res.supplier_stop:
            await _open_breaker(db, job, res.error_code or "supplier_stop")
        _record_reference(job, res, lease.mode)
        if lease.mode in ("recover", "dvoid"):
            await _apply_detached(db, job, lease, res, now)
            await db.commit()
            return
        if lease.mode == "precheck":
            await _apply_precheck(ctx, db, job, order, res, now)
            await db.commit()
            return
        if o is Outcome.OK:
            await _fulfil(ctx, db, job, order, res)
        elif o is Outcome.ACCEPTED_PENDING:
            if job.state != "accepted_pending":
                job.step, job.attempts = "pending", 0
                job.upstream_ref = res.upstream_ref or job.upstream_ref
                await _transition(db, job, "accepted_pending", "accepted")
            step = POLL_STEPS[min(job.attempts, len(POLL_STEPS) - 1)]
            job.next_poll_at = now + timedelta(seconds=step)
        elif o is Outcome.RETRYABLE:
            job.error_code = clean_detail(res.error_code or "retryable")
            if lease.mode == "submit":
                job.step = "submit"  # a RETRYABLE write was not executed
                _unsent(job)
            if job.attempts > MAX_RETRIES:
                await _needs_manual(db, job, "retry_exhausted")
            else:
                job.next_poll_at = now + _backoff(job.attempts, res.retry_after)
        elif o is Outcome.AMBIGUOUS:
            job.error_code = clean_detail(res.error_code or "ambiguous")
            if res.error_code == "request_failed":
                await _set_check(db, job, False)  # per-offer stop: the supplier stays open
            if job.state == "submitting":
                job.step = "reconcile"
            if job.attempts > MAX_RECONCILE:
                await _needs_manual(db, job, "unresolved")
            else:
                job.next_poll_at = now + _backoff(min(job.attempts, 3), res.retry_after)
        elif o is Outcome.UNAVAILABLE:
            job.error_code = clean_detail(res.error_code or "unavailable")
            if lease.mode == "submit":
                job.step = "verify"  # failover only after query() also finds nothing
                job.next_poll_at = now
            elif lease.mode == "verify":
                _unsent(job)  # submit and the same-key query both found nothing
                await _failover(ctx, db, job, order, job.error_code)
            elif job.state == "accepted_pending":
                await _needs_manual(db, job, "upstream_lost")
            else:  # reconcile found nothing: resubmit under the SAME key
                job.step = "submit"
                job.next_poll_at = now
        else:  # FATAL: a deterministic refusal, nothing was executed for this key
            if res.error_code != "IDEMPOTENCY_CONFLICT":
                _unsent(job)  # (a conflicting key HAS been used: the first request may have run)
            await _needs_manual(db, job, res.error_code or "fatal")
        await _alert_if_stuck(db, job, now)
        await db.commit()


async def process_job(ctx: ProcurementContext, job_id: uuid.UUID, tenant_id: uuid.UUID) -> bool:
    lease = await _lease(ctx, job_id, tenant_id)
    if lease is None:
        return False
    res = await _call(ctx, lease)
    charged = res.outcome is Outcome.OK or lease.mode in ("submit", "recover")
    try:
        await _apply(ctx, lease, res)
    finally:
        del res  # drop the plaintext reference as early as possible
    if lease.mode == "precheck":
        await process_job(ctx, job_id, tenant_id)  # a passed check sends in the same pass
    if charged:  # review-v12 §6: the balance (and the breaker) is refreshed after every job
        try:
            await refresh_balance(ctx, lease.ref)
        except Exception as exc:
            log.error("post-job balance refresh failed error=%s", type(exc).__name__)
    return True


async def due_jobs(ctx: ProcurementContext, limit: int = 20) -> list[tuple[uuid.UUID, uuid.UUID]]:
    async with tenant_session(ctx.maker, None) as db:
        rows = (
            await db.execute(
                select(ProcurementJob.id, ProcurementJob.tenant_id)
                .where(
                    ProcurementJob.state.in_(WORK) | ProcurementJob.recover.is_(True),
                    ProcurementJob.next_poll_at <= func.now(),
                )
                .order_by(ProcurementJob.next_poll_at)
                .limit(limit)
            )
        ).all()
    return [(r.id, r.tenant_id) for r in rows]


async def run_once(ctx: ProcurementContext) -> int:
    """Drain the outbox, then work every due job once. Returns the number of actions."""
    n = 0
    while n < 100 and await process_outbox_one(ctx):
        n += 1
    for jid, tid in await due_jobs(ctx):
        if await process_job(ctx, jid, tid):
            n += 1
    return n


async def drain(ctx: ProcurementContext, rounds: int = 20) -> int:
    """Tests / CLI: repeat ``run_once`` until nothing is due now."""
    total = 0
    for _ in range(rounds):
        n = await run_once(ctx)
        total += n
        if n == 0:
            break
    return total


async def run_engine(ctx: ProcurementContext, stop: asyncio.Event) -> None:
    log.info("procurement engine started")
    while not stop.is_set():
        try:
            await run_once(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("procurement engine pass failed error=%s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=ctx.settings.jobs_poll_s)
        except asyncio.TimeoutError:
            pass
