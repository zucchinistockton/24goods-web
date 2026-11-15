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

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.cardvault import CardContext
from app.core.db import set_tenant, tenant_session
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
UPSTREAM_MODES = ("upstream_cdk",)
ACTIVE = ("queued", "submitting", "accepted_pending")
WORK = (*ACTIVE, "void_pending")  # states the poller leases (void_pending: disable upstream)
POLL_STEPS = (15, 30, 60, 120, 300, 600, 900)  # seconds; capped at 15 min (spec §6)
MAX_RETRIES = 10
MAX_RECONCILE = 40
STUCK_AFTER = timedelta(minutes=30)
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
) -> list[OfferRow]:
    """Enabled ``cdk`` offers of enabled suppliers with a closed breaker for this exact target
    (variant offers for variant orders, variant-less offers otherwise), in failover order."""
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
            SupplierOffer.kind == "cdk",
            SupplierAccount.enabled.is_(True),
            SupplierAccount.breaker_open.is_(False),
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
        cands = await candidates(ctx, db, order)
        offer = cands[0] if cands else None
        job = ProcurementJob(
            id=jid,
            tenant_id=order.tenant_id,
            order_id=order.id,
            unit_no=unit,
            offer_id=offer.id if offer else None,
            supplier_id=offer.supplier_id if offer else None,
            kind="cdk",
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
    if ev.kind.startswith("cdk.") and ref is not None:
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
            if job.state in ACTIVE:
                job.next_poll_at = utcnow()
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
    mode: str  # submit | query | verify
    state: str
    step: str
    key: str
    spec: JobSpec
    ref: SupplierRef


def _spec(job: ProcurementJob, offer: OfferRow) -> JobSpec:
    voiding = job.state == "void_pending"
    return JobSpec(
        job_id=job.id.hex,
        step="void" if voiding else job.step,
        # the vendor caches answers per key: a void never reuses the issue key
        idempotency_key=job_idempotency_key(job.id.hex, "void") if voiding else job.idempotency_key,
        offer=OfferSpec(kind="cdk", params=offer.params),
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
            )
            .join(SupplierAccount, SupplierAccount.id == SupplierOffer.supplier_id)
            .where(SupplierOffer.id == job.offer_id)
        )
    ).first()
    if r is None:
        return None
    ref = SupplierRef(r.supplier_id, job.tenant_id, r.code, r.adapter, r.env, r.health)
    usable = r.enabled and r.supplier_enabled and not r.breaker_open
    return OfferRow(r.id, r.supplier_id, r.kind, dict(r.params or {}), ref) if usable else None


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
    tried = [*list(job.tried_offer_ids or []), *([job.offer_id] if job.offer_id else [])]
    job.tried_offer_ids = tried
    cands = await candidates(ctx, db, order, exclude=tried)
    if not cands:
        await _needs_manual(db, job, f"no_source:{reason}")
        return
    nxt = cands[0]
    job.offer_id, job.supplier_id = nxt.id, nxt.supplier_id
    # a fresh key for a supplier that never saw the old one; the old key is never reused
    job.idempotency_key = job_idempotency_key(job.id.hex, f"s{len(tried) + 1}")
    job.request_hash = request_hash(nxt.kind, nxt.params)
    job.step = "submit"
    job.upstream_ref = None
    job.attempts = 0
    job.next_poll_at = utcnow()
    await _transition(
        db, job, "queued", f"failover:{reason}", actor_id=actor_id, actor_role=actor_role, ip=ip
    )


async def _lease(ctx: ProcurementContext, job_id: uuid.UUID, tenant_id: uuid.UUID) -> Lease | None:
    now = utcnow()
    async with tenant_session(ctx.maker, tenant_id) as db:
        job = await db.scalar(
            select(ProcurementJob)
            .where(ProcurementJob.id == job_id)
            .with_for_update(skip_locked=True)
        )
        if (
            job is None
            or job.state not in WORK
            or job.next_poll_at is None
            or job.next_poll_at > now
        ):
            await db.rollback()
            return None
        order = await load_order(db, job.order_id)
        assert order is not None
        offer = await _offer(db, job)
        mode = "query"
        if job.state == "queued" or (job.state == "submitting" and job.step == "submit"):
            if order.status != "paid":
                await _needs_manual(db, job, "order_not_paid", notify_order=False)
                await db.commit()
                return None
            if offer is None:  # never sent with this offer: fail over without a query
                await _failover(ctx, db, job, order, "offer_unusable")
                await db.commit()
                return None
            if job.request_hash and job.request_hash != request_hash(offer.kind, offer.params):
                await _needs_manual(db, job, "offer_changed")
                await db.commit()
                return None
            if job.state == "queued":
                job.step = "sent"  # from here on a crash means "maybe sent": reconcile
                await _transition(db, job, "submitting", "submit")
            else:
                job.step = "sent"
            mode = "submit"
        elif job.state == "void_pending":
            mode = "void"
        elif job.state == "submitting" and job.step == "verify":
            mode = "verify"
        elif job.state == "submitting" and job.step == "sent":
            job.step = "reconcile"  # leased earlier and never finished: maybe sent
        if offer is None:
            # a job that may have reached a supplier is never moved automatically: query it
            # through its own supplier even if the offer was disabled meanwhile
            offer = await _offer_any(db, job)
            if offer is None:
                await _needs_manual(db, job, "offer_missing")
                await db.commit()
                return None
        job.attempts += 1
        job.next_poll_at = now + timedelta(seconds=ctx.settings.job_lease_s)
        lease = Lease(
            job.id, job.tenant_id, mode, job.state, job.step, job.idempotency_key,
            _spec(job, offer), offer.ref,
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


async def _call(ctx: ProcurementContext, lease: Lease) -> Result:
    try:
        sup = engine_supplier(ctx, lease.ref)
        if lease.mode == "submit":
            return await sup.submit(lease.spec)
        if lease.mode == "void":
            return await sup.void(lease.spec)
        return await sup.query(lease.spec)
    except Exception as exc:  # unknown failure: if it may have been sent, reconcile
        log.error("supplier call failed job=%s error=%s", lease.job_id, type(exc).__name__)
        if lease.mode in ("submit", "void"):
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
    if len(res.secrets) != 1 or ctx.sealer is None or order.product_id is None:
        # never store or log what we got; a CDK replay returns it again once fixed
        job.step = "reconcile"
        job.error_code = "seal_unavailable" if ctx.sealer is None else "secret_count"
        job.next_poll_at = utcnow() + _backoff(job.attempts, None)
        return
    sealer = ctx.sealer
    card_id = uuid.uuid4()
    tid = str(order.tenant_id)
    code = res.secrets[0]
    values = {
        "id": card_id,
        "tenant_id": order.tenant_id,
        "product_id": order.product_id,
        "variant_id": order.variant_id,
        "blob": sealer.seal(code, CardContext(tid, str(order.product_id), str(card_id))),
        "fingerprint": sealer.fingerprint(tid, code),
        "lookup_fp": sealer.lookup_fingerprint(tid, code),
        "kid": sealer.active_kid,
        "status": "sold",
        "order_id": order.id,
    }
    del code
    try:
        async with db.begin_nested():
            # Core insert, no RETURNING: procurement has no SELECT on card_stock at all
            await db.execute(insert(CardStock.__table__).values(**values))
    except IntegrityError:
        await _needs_manual(db, job, "duplicate_code")
        return
    job.card_stock_ids = [card_id]
    job.upstream_ref = res.upstream_ref
    job.upstream_status = "unused"
    if res.cost is not None:
        job.cost, job.currency = res.cost.amount, res.cost.currency
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
        job.upstream_status = "disabled"
        await _transition(db, job, "voided", "void_ok")
    elif o is Outcome.FATAL or job.attempts > MAX_RETRIES:
        job.error_code = clean_detail(f"void_rejected:{res.error_code or 'unresolved'}")
        job.step, job.next_poll_at = "done", None
        back = "fulfilled" if job.card_stock_ids else "needs_manual"
        await _transition(db, job, back, "void_rejected", severity="warning")
    else:  # RETRYABLE / AMBIGUOUS / UNAVAILABLE: the same void key is replayed later
        job.error_code = clean_detail(res.error_code or "void_retry")
        job.next_poll_at = now + _backoff(job.attempts, res.retry_after)


async def _apply(ctx: ProcurementContext, lease: Lease, res: Result) -> None:
    now = utcnow()
    async with tenant_session(ctx.maker, lease.tenant_id) as db:
        job = await db.scalar(
            select(ProcurementJob).where(ProcurementJob.id == lease.job_id).with_for_update()
        )
        if (
            job is None
            or job.state != lease.state
            or job.step != lease.step
            or job.idempotency_key != lease.key
        ):
            await db.rollback()  # staff acted meanwhile: their action wins
            return
        order = await load_order(db, job.order_id)
        assert order is not None
        o = res.outcome
        if lease.mode == "void":
            await _apply_void(db, job, res, now)
            await db.commit()
            return
        if res.supplier_stop:
            await _open_breaker(db, job, res.error_code or "supplier_stop")
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
            if job.attempts > MAX_RETRIES:
                await _needs_manual(db, job, "retry_exhausted")
            else:
                job.next_poll_at = now + _backoff(job.attempts, res.retry_after)
        elif o is Outcome.AMBIGUOUS:
            job.error_code = clean_detail(res.error_code or "ambiguous")
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
                await _failover(ctx, db, job, order, job.error_code)
            elif job.state == "accepted_pending":
                await _needs_manual(db, job, "upstream_lost")
            else:  # reconcile found nothing: resubmit under the SAME key
                job.step = "submit"
                job.next_poll_at = now
        else:  # FATAL
            await _needs_manual(db, job, res.error_code or "fatal")
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
        await db.commit()


async def process_job(ctx: ProcurementContext, job_id: uuid.UUID, tenant_id: uuid.UUID) -> bool:
    lease = await _lease(ctx, job_id, tenant_id)
    if lease is None:
        return False
    res = await _call(ctx, lease)
    charged = res.outcome is Outcome.OK or lease.mode == "submit"
    try:
        await _apply(ctx, lease, res)
    finally:
        del res  # drop the plaintext reference as early as possible
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
                    ProcurementJob.state.in_(WORK),
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
