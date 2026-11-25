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
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.cardvault import CardContext
from app.core.db import set_tenant, tenant_session
from app.core.errors import Conflict
from app.models import (
    CardStock,
    HalfPriceCheck,
    Order,
    Outbox,
    ProcurementCredential,
    ProcurementFunding,
    ProcurementJob,
    Product,
    ProductVariant,
    SupplierAccount,
    SupplierOffer,
    SupplierWebhookEvent,
)
from app.procurement import ledger, topups
from app.procurement.context import ProcurementContext
from app.procurement.funding import (
    IN_FLIGHT_STATES,
    buyer_card_since,
    card_in_flight,
    funding_card_clash,
    funding_mask_clash,
    lock_card,
)
from app.procurement.notify import notify_progress_changed
from app.procurement.risk import DECLINE_CODES
from app.procurement.service import SupplierRef, make_supplier, refresh_balance, refresh_offers
from app.suppliers import direct_progress as progress
from app.suppliers.base import (
    FundingPolicy,
    JobSpec,
    Money,
    OfferSpec,
    Outcome,
    Result,
    Supplier,
    clean_detail,
    job_idempotency_key,
)
from app.suppliers.zovocard import direct as dr

log = logging.getLogger("app.procurement.engine")

TOPIC_REQUESTED = "procure.requested"
TOPIC_EVENT = "supplier.event"
TOPIC_FULFILLED = "procure.fulfilled"
TOPIC_FAILED = "procure.failed"
CONSUMED = (TOPIC_REQUESTED, TOPIC_EVENT)
_CDK_TERMINAL = frozenset({"consumed", "disabled"})  # ZovoCard CDK statuses that do not move on
UPSTREAM_MODES = ("upstream_cdk", "upstream_vcc", "upstream_direct")
# delivery mode -> offer / job kind
MODE_KIND = {"upstream_cdk": "cdk", "upstream_vcc": "vcc", "upstream_direct": "direct"}
# vendor status recorded on a job: what a fresh purchase looks like / what a void leaves
ISSUED_STATUS = {"cdk": "unused", "vcc": "active", "direct": "completed"}
VOIDED_STATUS = {"cdk": "disabled", "vcc": "deleted", "direct": "cancelled"}
TERMINAL_JOB = ("fulfilled", "failed", "voided")  # a direct job in these needs no credential
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
        "region_not_offered",  # the vendor withdrew the region after the sale (issue #27)
        "invalid_offer",
        "cdk_gpt_only",
        "kind_not_supported_m1",
        "amount_out_of_range",
        "funding_card_invalid",
        "funding_card_exhausted",
        "funding_card_low",
        "points_exhausted",  # ADR-054: the zzshu account has no point left
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
    cost_estimate: Decimal | None = None  # the offer's configured cost ...
    currency: str | None = None  # ... in this currency


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


async def _pins(
    db: AsyncSession, order: OrderRow
) -> tuple[dict[str, str] | None, tuple[str, str | None] | None]:
    """What a source must match for THIS order: the variant's ``options`` (plan, region) and the
    plan / country of the half-price check the order was created with. Never fail over to another
    plan or country."""
    from app.modules.catalog.regions import options_of

    opts = None
    if order.variant_id is not None:
        opts = options_of(
            await db.scalar(
                select(ProductVariant.options).where(
                    ProductVariant.id == order.variant_id,
                    ProductVariant.tenant_id == order.tenant_id,
                )
            )
        )
    chk = (
        await db.execute(
            select(HalfPriceCheck.plan, HalfPriceCheck.country).where(
                HalfPriceCheck.tenant_id == order.tenant_id, HalfPriceCheck.order_id == order.id
            )
        )
    ).first()
    return opts, ((chk.plan, chk.country) if chk else None)


def _pinned(
    params: dict[str, Any],
    opts: dict[str, str] | None,
    chk: tuple[str, str | None] | None,
) -> bool:
    from app.modules.catalog.regions import source_matches_options

    if not source_matches_options(params, opts):
        return False
    return chk is None or (params.get("plan"), params.get("payment_country")) == chk


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
    if order.delivery_mode == "upstream_direct" and (
        getattr(getattr(ctx, "settings", None), "env", "") == "prod"
    ):
        stmt = stmt.where(
            SupplierAccount.env != "sandbox"
        )  # a real session never goes to a sandbox
    opts, chk = await _pins(db, order)
    stmt_rows = list((await db.execute(stmt)).all())
    if chk is None:
        # fail closed: an order with no attached half-price check never reaches a half-price
        # source (failover / round-robin included); with nothing else left the job needs staff
        from app.modules.catalog.regions import is_half_plan

        stmt_rows = [r for r in stmt_rows if not is_half_plan((r.params or {}).get("plan"))]
    rows = [
        OfferRow(
            r.id,
            r.supplier_id,
            r.kind,
            dict(r.params or {}),
            SupplierRef(r.supplier_id, order.tenant_id, r.code, r.adapter, r.env, r.health),
        )
        for r in stmt_rows
        if _pinned(dict(r.params or {}), opts, chk)  # before round-robin: it rotates matches only
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
    if order.delivery_mode == "upstream_direct" and len(rows) > 1:
        rows = await _demote_cooling(ctx, rows)
    return rows


# ADR-054: ZovoCard direct orders that fail DEFINITELY before any charge, repeatedly, put the
# supplier's direct sources behind the others for a while (never removed: with nothing else
# usable they are still tried). Fixed window counter in Redis (the procurement ACL allows incr /
# expire / get / ttl on ``rl:proc:*`` only).
PRECHARGE_LIMIT = 3  # failures ...
PRECHARGE_WINDOW_S = 3600  # ... within this window start the cool-down
COOLDOWN_S = 1800


def _cool_key(supplier_id: uuid.UUID) -> str:
    return f"rl:proc:cool:{supplier_id}"


async def _demote_cooling(ctx: HasRedis, rows: list[OfferRow]) -> list[OfferRow]:
    cooling: dict[uuid.UUID, bool] = {}
    for r in rows:
        if r.ref.adapter == "zovocard" and r.supplier_id not in cooling:
            try:
                cooling[r.supplier_id] = await ctx.redis.get(_cool_key(r.supplier_id)) is not None
            except Exception:  # Redis down: the configured order stands
                cooling[r.supplier_id] = False
    keep = [r for r in rows if not cooling.get(r.supplier_id)]
    return keep + [r for r in rows if cooling.get(r.supplier_id)]


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


async def _half_hold(db: AsyncSession, job: ProcurementJob) -> str | None:
    """A ``manual`` half-price check blocks the first send until staff approve it."""
    status = await db.scalar(
        select(HalfPriceCheck.status).where(
            HalfPriceCheck.tenant_id == job.tenant_id, HalfPriceCheck.order_id == job.order_id
        )
    )
    if status == "manual_pending":
        return "half_price_check_manual"
    if status == "manual_refused":
        return "half_price_check_refused"
    return None


async def _needs_manual(
    db: AsyncSession, job: ProcurementJob, reason: str, *, notify_order: bool = True
) -> None:
    job.error_code = clean_detail(reason)
    job.next_poll_at = None
    if job.kind == "direct" and job.upstream_ref and reason in WATCH_REASONS:
        # the supplier holds an order that has not finished: keep a SLOW poll so a late
        # ``completed`` is seen (M8). Bounded by WATCH_MAX.
        job.attempts = 0
        job.next_poll_at = utcnow() + WATCH_EVERY
    await _transition(db, job, "needs_manual", reason, severity="warning")
    if job.kind == "direct" and job.order_id is not None:
        await _keep_for_support(db, job)
    if job.kind == "direct" and job.card_id and _zzshu_card_unswept(job):
        # security review of #113: a held zzshu job (retry_exhausted, create_unconfirmed...) whose
        # dedicated card was opened and never swept still holds money: staff are told
        await audit(
            db,
            tenant_id=job.tenant_id,
            action="procurement.zzshu_card_unswept",
            target=f"procurement_job:{job.id}",
            detail={"order_id": str(job.order_id), "card_id": job.card_id,
                    "last4": job.card_last4, "reason": job.error_code},
            severity="warning",
        )  # fmt: skip
    if notify_order:
        _emit(
            db,
            job.tenant_id,
            TOPIC_FAILED,
            f"failed:{job.order_id}:{job.id}",
            {"order_id": str(job.order_id), "job_id": str(job.id), "reason": job.error_code},
        )


def _zzshu_card_unswept(job: ProcurementJob) -> bool:
    """The job's timeline shows a zzshu dedicated card opened and not (yet) swept."""
    events = (job.progress or {}).get("events")
    if not isinstance(events, list):
        return False
    codes = {e.get("code") for e in events if isinstance(e, dict) and e.get("cat") == "zzshu"}
    # ``card_unswept``: the warning was already given for this card
    return "card_opened" in codes and not codes & {"card_swept", "card_unswept"}


WATCH_REASONS = frozenset(
    {
        "pending_too_long", "unresolved", "retry_exhausted", "replay_expired", "offer_changed",
        "offer_missing", "upstream_lost",
    }
)  # fmt: skip
WATCH_EVERY = timedelta(minutes=15)
WATCH_MAX = 96  # 24 h of slow polls


def _watching(job: ProcurementJob) -> bool:
    """A held direct job whose supplier order is still being watched (slow poll)."""
    return bool(
        job.state == "needs_manual"
        and job.kind == "direct"
        and job.upstream_ref
        and job.next_poll_at is not None
    )


def _workable(job: ProcurementJob) -> bool:
    """The poller leases a job in a work state, a closed job with a pending ``recover``, or a held
    direct job being watched."""
    return job.state in WORK or job.recover or _watching(job)


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
        elif job.state == "fulfilled" and job.kind == "direct":
            await _direct_refunded(db, job, order, actor_id, actor_role, ip)
            n += 1
        elif job.state == "fulfilled" and (undelivered or job.kind == "vcc"):
            # a code bound but never handed over (N5): never leave it valid at the supplier. A
            # CARD is live money even once delivered: a refund deletes it (the supplier returns
            # whatever balance remains), unlike a delivered CDK code which after-sales handles.
            await _void_fulfilled(
                db, job, reason, actor_id, actor_role, ip, delivered=not undelivered
            )
            n += 1
    return n


UNDELIVERED_ORDER = ("paid", "manual_review")  # nothing handed over yet


async def direct_completed(db: AsyncSession, order: Order) -> bool:
    """Any ``direct`` job of the order that has (or may have) a supplier order that is not yet
    closed out: completed, or still moving (any state except failed / voided with an order id).
    A refund of such an order may be a loss, so it needs the admin's acknowledgement."""
    return bool(
        await db.scalar(
            select(func.count(ProcurementJob.id)).where(
                ProcurementJob.tenant_id == order.tenant_id,
                ProcurementJob.order_id == order.id,
                ProcurementJob.kind == "direct",
                ProcurementJob.upstream_ref.is_not(None),
                ProcurementJob.state.not_in(("failed", "voided")),
            )
        )
    )


async def _direct_refunded(
    db: AsyncSession,
    job: ProcurementJob,
    order: Order,
    actor_id: uuid.UUID | None,
    actor_role: str,
    ip: str | None,
) -> None:
    """A refund / close of an order whose direct job COMPLETED: the subscription is on the buyer's
    account and the supplier's API cannot undo it, so nothing is voided automatically. The job
    stays ``fulfilled``; a critical audit row and a staff alert (``error_code``
    ``refunded_after_success``, picked up by the reconciler) make it a visible STAFF decision (a
    refund-after-success is a loss to recover with the supplier, or a goodwill gesture)."""
    job.error_code = "refunded_after_success"
    await audit(
        db,
        tenant_id=job.tenant_id,
        action="procurement.direct_refunded_after_success",
        actor_id=actor_id,
        actor_role=actor_role,
        target=f"procurement_job:{job.id}",
        detail={
            "order_id": str(order.id),
            "upstream_ref": clean_detail(job.upstream_ref or ""),
            "supplier_id": str(job.supplier_id) if job.supplier_id else None,
            "order_status": order.status,
        },
        severity="critical",
        ip=ip,
    )


async def _void_fulfilled(
    db: AsyncSession,
    job: ProcurementJob,
    reason: str,
    actor_id: uuid.UUID | None,
    actor_role: str,
    ip: str | None,
    *,
    delivered: bool = False,
) -> None:
    """``fulfilled`` but undelivered unit of a refunded order: disable the code at the supplier
    (``void_pending``; ``voided`` at once when there is nothing to disable) and void the sealed
    ``card_stock`` row. Delivered units are never touched here (after-sales handles them)."""
    job.error_code = clean_detail(reason)
    if delivered:
        await audit(
            db,
            tenant_id=job.tenant_id,
            action="procurement.delivered_card_voided",
            actor_id=actor_id,
            actor_role=actor_role,
            target=f"procurement_job:{job.id}",
            detail={"order_id": str(job.order_id), "reason": clean_detail(reason)},
            severity="critical",
            ip=ip,
        )
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
    if order.delivery_mode == "upstream_direct" and order.qty != 1:
        # one account per order: never create a second job for a session that exists once
        _emit(
            db,
            order.tenant_id,
            TOPIC_FAILED,
            f"failed:{order.id}:qty",
            {"order_id": str(order.id), "reason": "qty_not_one"},
        )
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
    elif ev.kind.startswith("gpt_direct.") and (ev.payload or {}).get("order_id") is not None:
        oid = str((ev.payload or {})["order_id"])[:128]
        djobs = list(
            await db.scalars(
                select(ProcurementJob)
                .where(
                    ProcurementJob.tenant_id == ev.tenant_id,
                    ProcurementJob.supplier_id == ev.supplier_id,
                    ProcurementJob.kind == "direct",
                    ProcurementJob.upstream_ref == oid,
                )
                .with_for_update()
            )
        )
        dstatus = (ev.payload or {}).get("status")
        hint = progress.vendor_from_webhook(ev.payload or {})
        for job in djobs:
            _record_progress(job, hint, "webhook", now)
            if isinstance(dstatus, str) and job.state in ACTIVE:
                job.upstream_status = clean_detail(dstatus, 32)
            if job.state in ACTIVE and not _leased(job, now):
                job.next_poll_at = now  # a hint: the re-query below stays the truth
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
    order_id: uuid.UUID | None = None
    # the job may already have reached the supplier before THIS lease (``maybe_sent`` when it was
    # taken): a refusal that arrives now says nothing about that earlier send (direct jobs)
    prior_send: bool = False


def _offer_params(job: ProcurementJob, offer: OfferRow) -> dict[str, Any]:
    """The offer's parameters, plus (card mode ``auto_existing``) the card the job was assigned.
    The offer row and the pinned ``request_hash`` never carry it. A zzshu source (ADR-054) gets the
    dedicated card its adapter opened for the job (a retry reuses it)."""
    if _dedicated(offer):
        if job.card_id and job.card_id.isdigit():
            return {**offer.params, dr.PICKED_CARD_KEY: int(job.card_id)}
        return offer.params
    if _auto_card(offer) and job.card_id and job.card_id.isdigit():
        extra: dict[str, Any] = {dr.PICKED_CARD_KEY: int(job.card_id)}
        if job.fund_amount and job.fund_key:  # ADR-048: the planned top-up and its key
            extra[dr.PICKED_TOPUP_KEY] = int(job.fund_amount * 100)
            extra[dr.PICKED_TOPUP_IDEM] = job.fund_key
        return {**offer.params, **extra}
    return offer.params


def _spec(
    job: ProcurementJob,
    offer: OfferRow,
    mode: str,
    tolerance: float = 0.10,
    funding: FundingPolicy | None = None,
    upgrade_usd: Decimal | None = None,
    cancel_renewal: bool = False,
) -> JobSpec:
    voiding = mode in ("void", "dvoid")
    tol = Decimal(str(tolerance))
    bound = (
        Money(offer.cost_estimate * (1 + tol), "USD")
        if offer.kind == "direct" and offer.cost_estimate is not None and offer.currency == "USD"
        else None
    )
    return JobSpec(
        job_id=job.id.hex,
        # a pending delete is only ever READ afterwards (step ``delete_pending``, F4)
        step=("delete_pending" if job.step == "delete_pending" else "void")
        if voiding
        else job.step,
        # the vendor caches answers per key: a void never reuses the issue key
        idempotency_key=job_idempotency_key(job.id.hex, "void") if voiding else job.idempotency_key,
        offer=OfferSpec(
            kind=offer.kind,  # type: ignore[arg-type]
            params=_offer_params(job, offer),
            cost_usd=offer.cost_estimate if offer.currency == "USD" else None,
            funding=funding,
        ),
        upstream_ref=job.upstream_ref,
        account_mask=job.account_mask,
        cost_bound=bound,
        quote_tolerance=tol,
        attempt=job.card_attempt or 0,
        upgrade_max_usd=upgrade_usd,
        cancel_renewal=cancel_renewal,
    )


async def _upgrade_usd(db: AsyncSession, job: ProcurementJob) -> Decimal | None:
    """The upgrade difference (USD) the buyer of this direct order paid for (owner 2026-11-22:
    the order's ``terms.upgrade`` snapshot), else ``None`` (a full-price order)."""
    terms = await db.scalar(
        select(Order.terms).where(Order.id == job.order_id, Order.tenant_id == job.tenant_id)
    )
    up = terms.get("upgrade") if isinstance(terms, dict) else None
    if isinstance(up, dict) and up.get("credit_minor") == 0:
        # owner 2026-11-25: the buyer paid the FULL list price; the upgrade quote is only the
        # cheaper way to deliver it, and the full plan stays allowed when no upgrade is offered
        return None
    try:
        return Decimal(str(up["usd"])) if isinstance(up, dict) and up.get("usd") else None
    except (InvalidOperation, ValueError):
        return None


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
                SupplierOffer.cost_estimate,
                SupplierOffer.currency.label("offer_currency"),
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
    return OfferRow(
        r.id, r.supplier_id, r.kind, dict(r.params or {}), ref, kind, r.last_balance_at,
        r.cost_estimate, r.offer_currency,
    )  # fmt: skip


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
    only_adapter: str | None = None,
) -> None:
    """``only_adapter`` is set ONLY by the definite pre-charge exception (ADR-054,
    :func:`_precharge_failover`): the job already holds a supplier order that provably charged
    nothing, and only a source of that adapter may take it."""
    if (job.maybe_sent or job.upstream_ref) and only_adapter is None:
        # another supplier is only ever tried when the first one is known to have done nothing
        await _needs_manual(db, job, f"maybe_sent:{reason}")
        return
    tried = [*list(job.tried_offer_ids or []), *([job.offer_id] if job.offer_id else [])]
    job.tried_offer_ids = tried
    cands = await pick_candidates(ctx, db, order, tried)
    if only_adapter is not None:
        cands = [c for c in cands if c.ref.adapter == only_adapter]
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
    if only_adapter is not None:  # the failed order is over: the job is "never sent" again
        job.maybe_sent = False
        job.upstream_status = None
        job.card_id = job.card_last4 = job.used_card_id = job.used_last4 = None
        job.card_candidates = job.card_candidates_at = job.card_wait_since = None
        job.card_attempt = 0
        job.account_mask = job.preflight_expires_at = None  # the new source reports its own
        topups.forget(job)
    job.attempts = 0
    job.next_poll_at = utcnow()
    await _transition(
        db, job, "queued", f"failover:{reason}", actor_id=actor_id, actor_role=actor_role, ip=ip
    )


async def _note_precharge_failure(
    ctx: ProcurementContext, db: AsyncSession, job: ProcurementJob
) -> None:
    """Count one definite pre-charge failure of a ZovoCard direct order; at
    :data:`PRECHARGE_LIMIT` within :data:`PRECHARGE_WINDOW_S` start the cool-down of that
    supplier's direct sources and leave one audit row (the API alerts staff from it)."""
    if job.supplier_id is None:
        return
    try:
        key = f"rl:proc:pcf:{job.supplier_id}"
        n = int(await ctx.redis.incr(key))
        if n == 1:
            await ctx.redis.expire(key, PRECHARGE_WINDOW_S)
        if n < PRECHARGE_LIMIT:
            return
        cool = int(await ctx.redis.incr(_cool_key(job.supplier_id)))
        if cool == 1:  # a new cool-down (a running one is not extended or announced again)
            await ctx.redis.expire(_cool_key(job.supplier_id), COOLDOWN_S)
        else:
            return
    except Exception as exc:  # Redis down: no cool-down, the failover itself still works
        log.warning("cool-down counter failed error=%s", type(exc).__name__)
        return
    await audit(
        db,
        tenant_id=job.tenant_id,
        action="procurement.direct_cooldown",
        target=f"supplier:{job.supplier_id}",
        detail={
            "failures": n,
            "window_s": PRECHARGE_WINDOW_S,
            "cooldown_s": COOLDOWN_S,
            "order_id": str(job.order_id),
        },
        severity="critical",
    )


async def _precharge_failover(
    ctx: ProcurementContext,
    db: AsyncSession,
    job: ProcurementJob,
    order: OrderRow,
    res: Result,
    lease: Lease,
) -> bool:
    """ADR-054 (owner 2026-11-25): the ONE exception to "a job that holds a supplier order is never
    moved". A ZovoCard direct order that failed DEFINITELY before any charge (``failed_precharge``
    with its fee and funding hold released / none, set by the adapter as ``precharge_clean``)
    goes to the zzshu source of the same variant. Anything that may have charged (declined,
    cancelled, a held fee, an unread status) stays in ``needs_manual``. The ZovoCard reference is
    kept in the audit row and the job timeline. Returns True when the job was moved."""
    if (
        lease.ref.adapter != "zovocard"
        or not res.precharge_clean
        or res.error_code != "order_failed_precharge"
        or not job.upstream_ref
    ):
        return False
    await _note_precharge_failure(ctx, db, job)
    tried = [*list(job.tried_offer_ids or []), *([job.offer_id] if job.offer_id else [])]
    cands = await pick_candidates(ctx, db, order, tried)
    if not any(c.ref.adapter == "zzshu" for c in cands):
        return False  # no standby source: the job goes to staff as before
    before_ref, before_offer = job.upstream_ref, job.offer_id
    await _failover(ctx, db, job, order, "precharge_failed", only_adapter="zzshu")
    if job.state != "queued" or job.offer_id == before_offer:  # (cannot happen: checked above)
        return job.state == "needs_manual"
    await audit(
        db,
        tenant_id=job.tenant_id,
        action="procurement.precharge_failover",
        target=f"procurement_job:{job.id}",
        detail={
            "order_id": str(job.order_id),
            "from_ref": clean_detail(before_ref),
            "from_offer": str(before_offer),
            "to_offer": str(job.offer_id),
            "vendor": clean_detail(res.detail or "failed_precharge"),
        },
        severity="warning",
    )
    _record_progress(
        job,
        {
            "events": [
                {
                    "tid": f"failover-{before_ref}"[:64],
                    "cat": "failover",
                    "code": "precharge_failover",
                    "msg": "ZovoCard order failed before any charge; sent to the standby source",
                }
            ]
        },
        "engine",
        utcnow(),
    )
    return True


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
        _spec(job, offer, mode, ctx.settings.direct_quote_tolerance), offer.ref, job.order_id, True,
    )  # fmt: skip
    await db.commit()
    return lease


async def _lease_watch(
    ctx: ProcurementContext, db: AsyncSession, job: ProcurementJob, now: datetime
) -> Lease | None:
    """One READ of a held direct job's supplier order (never a POST)."""
    offer = await _offer_any(db, job)
    job.attempts += 1
    if offer is None or job.attempts > WATCH_MAX:
        job.next_poll_at = None  # stop watching: staff decide
        await db.commit()
        return None
    job.next_poll_at = job.lease_until = now + timedelta(seconds=ctx.settings.job_lease_s)
    lease = Lease(
        job.id, job.tenant_id, "watch", job.state, job.step, job.idempotency_key,
        _spec(job, offer, "query", ctx.settings.direct_quote_tolerance), offer.ref, job.order_id,
        True,
    )  # fmt: skip
    await db.commit()
    return lease


async def _check_cost_bound(
    db: AsyncSession, job: ProcurementJob, res: Result, bound: Money | None
) -> None:
    """After a direct order COMPLETED, compare what it cost with the budget it was sent under: over
    it is a critical audit row (and a staff alert) even though the money is already spent. Only
    a USD cost is comparable; a non-USD plan's cost stays unbounded here (see review-v12 §18)."""
    if (
        job.kind != "direct"
        or res.cost is None
        or bound is None
        or res.cost.currency != bound.currency
        or res.cost.amount <= bound.amount
    ):
        return
    await audit(
        db,
        tenant_id=job.tenant_id,
        action="procurement.cost_over_bound",
        target=f"procurement_job:{job.id}",
        detail={
            "order_id": str(job.order_id),
            "cost": str(res.cost.amount),
            "bound": str(bound.amount),
            "currency": bound.currency,
        },
        severity="critical",
    )


async def _apply_watch(
    ctx: ProcurementContext,
    db: AsyncSession,
    job: ProcurementJob,
    order: OrderRow,
    res: Result,
    now: datetime,
    bound: Money | None = None,
) -> None:
    """A held direct job's order was read again. Completed: delivered when the order still waits
    for it (paid, or held by the procurement hold; delivery decides), otherwise the order was
    refunded meanwhile = ``refunded_after_success`` (critical audit, staff alert)."""
    o = res.outcome
    if o is Outcome.OK:
        if order.status in ("paid", "manual_review"):
            await _fulfil(ctx, db, job, order, res, allow_held=True)
            await _check_cost_bound(db, job, res, bound)
            return
        job.upstream_ref = res.upstream_ref or job.upstream_ref
        job.error_code = "refunded_after_success"
        job.next_poll_at = None
        await audit(
            db,
            tenant_id=job.tenant_id,
            action="procurement.direct_refunded_after_success",
            target=f"procurement_job:{job.id}",
            detail={
                "order_id": str(job.order_id),
                "upstream_ref": clean_detail(job.upstream_ref or ""),
                "order_status": order.status,
                "late": True,
            },
            severity="critical",
        )
        await _transition(db, job, "fulfilled", "late_completed_after_refund", severity="warning")
        await _drop_credential(db, job)
    elif o in (Outcome.ACCEPTED_PENDING, Outcome.RETRYABLE, Outcome.AMBIGUOUS):
        job.next_poll_at = now + WATCH_EVERY
    else:  # FATAL / UNAVAILABLE: the order ended another way: nothing more to wait for
        job.next_poll_at = None


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
        if _watching(job) and not job.recover:
            return await _lease_watch(ctx, db, job, now)
        if job.state not in WORK:  # closed job with a pending recovery
            return await _lease_detached(ctx, db, job, now)
        order = await load_order(db, job.order_id)
        assert order is not None
        prior_send = bool(job.maybe_sent)
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
            if problem := await _half_hold(db, job):
                # the half-price check of this order awaits (or was refused by) staff: the supplier
                # is NOT called (issue #27); approve in the console, then retry the job
                await _needs_manual(db, job, problem)
                if problem == "half_price_check_manual" and job.alerted_at is None:
                    # a paid order waits for staff: one visible warning (the Regions list also
                    # flags it overdue after ``MANUAL_SLA``)
                    job.alerted_at = now
                    await audit(
                        db,
                        tenant_id=job.tenant_id,
                        action="procurement.half_hold",
                        target=f"procurement_job:{job.id}",
                        detail={"order_id": str(job.order_id), "sla_minutes": 60},
                        severity="warning",
                    )
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
            if job.kind == "direct" and (problem := await _direct_gate(ctx, db, job, offer)):
                # no buyer session to send (expired, never sealed, unreadable), a funding card that
                # is a buyer's card, a sandbox supplier in production: nothing was sent, staff / the
                # buyer must act; checked BEFORE maybe_sent is set
                await _needs_manual(db, job, problem)
                await db.commit()
                return None
            if job.step != "ready":
                mode = "precheck"  # read-only availability check before the first send (H2)
            elif job.kind == "direct" and _auto_card(offer):
                if not await _assign_card(ctx, db, job, offer, now):
                    await db.commit()  # parked: nothing is sent, the key is unchanged
                    return None
                if job.state == "queued":
                    await _transition(db, job, "submitting", "submit")
                job.step = "sent"  # from here on a crash means "maybe sent": reconcile
                job.maybe_sent = True
                job.first_sent_at = now
            elif (
                job.kind == "direct"
                and not _dedicated(offer)
                and await _card_busy(ctx, db, job, offer)
            ):
                # another order is at the vendor on the SAME funding card: wait, never fail over
                job.error_code = "card_busy"
                job.next_poll_at = now + BALANCE_BACKOFF
                await _alert_if_stuck(db, job, now)  # a long park is still a stuck job
                await db.commit()
                return None
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
        if mode == "submit" and job.fund_key:
            await topups.mark_sent(db, job)  # committed with the lease, before any HTTP call
        policy = (
            await topups.policy(db, job.tenant_id, offer.supplier_id, now, exclude_job=job.id)
            if job.kind == "direct"
            and (
                (_auto_card(offer) and mode == "precheck")
                or (_dedicated(offer) and mode in ("precheck", "submit"))
            )
            else None
        )
        spec = _spec(
            job,
            offer,
            mode,
            ctx.settings.direct_quote_tolerance,
            policy,
            await _upgrade_usd(db, job) if job.kind == "direct" else None,
            cancel_renewal=(
                job.kind == "direct"
                and mode == "submit"
                and (await topups.settings_of(db, job.tenant_id))["cancel_renewal_before_upgrade"]
                is True
            ),
        )
        lease = Lease(
            job.id, job.tenant_id, mode, job.state, job.step, job.idempotency_key,
            replace(spec, prior_send=prior_send), offer.ref, job.order_id, prior_send,
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
                SupplierOffer.cost_estimate,
                SupplierOffer.currency.label("offer_currency"),
            )
            .join(SupplierAccount, SupplierAccount.id == SupplierOffer.supplier_id)
            .where(SupplierOffer.id == job.offer_id)
        )
    ).first()
    if r is None:
        return None
    ref = SupplierRef(r.supplier_id, job.tenant_id, r.code, r.adapter, r.env, r.health)
    return OfferRow(
        r.id, r.supplier_id, r.kind, dict(r.params or {}), ref, None, None,
        r.cost_estimate, r.offer_currency,
    )  # fmt: skip


# ------------------------------------------------------------------ buyer credential (direct)
async def _credential_problem(
    ctx: ProcurementContext, db: AsyncSession, job: ProcurementJob, offer: OfferRow
) -> str | None:
    """Why a direct job has no buyer session to send (None = there is one). Only existence is
    checked here (no decryption); the product must match the offer's closed-enum product."""
    if ctx.cred_opener is None:
        return "credential_unreadable"
    row = (
        await db.execute(
            select(ProcurementCredential.product).where(
                ProcurementCredential.tenant_id == job.tenant_id,
                ProcurementCredential.order_id == job.order_id,
            )
        )
    ).first()
    if row is None:
        return "credential_missing"
    if row.product != offer.params.get("product"):
        return "credential_mismatch"
    return None


async def _direct_gate(
    ctx: ProcurementContext, db: AsyncSession, job: ProcurementJob, offer: OfferRow
) -> str | None:
    """Everything that must hold before a direct order may be sent (nothing is sent otherwise)."""
    if problem := await _credential_problem(ctx, db, job, offer):
        return problem
    if ctx.settings.env == "prod" and offer.ref.env == "sandbox":
        return "sandbox_in_prod"  # a real buyer's session never goes to the sandbox (S-L1)
    if _dedicated(offer):
        return None
    if _auto_card(offer):
        return None  # the picked card is checked against the same rule when it is assigned
    card = offer.params.get("card_id")
    if await funding_card_clash(db, job.tenant_id, offer.supplier_id, card):
        return "funding_card_invalid"  # a card bought for a buyer is never a funding card (H1)
    return None


def _dedicated(offer: OfferRow) -> bool:
    """A zzshu source (ADR-054): its adapter opens a NEW card per order, so there is no funding
    card to pick, check or serialise."""
    return offer.ref.adapter == "zzshu"


def _auto_card(offer: OfferRow) -> bool:
    """A direct source whose funding card is picked per order (card mode ``auto_existing``)."""
    return offer.kind == "direct" and offer.params.get("card_mode", "fixed") != "fixed"


def _candidates_of(job: ProcurementJob) -> list[tuple[str, str | None]]:
    """The usable cards of the last pre-check: ``"id:last4:cents,id"`` -> ``[(id, last4)]``."""
    return [(cid, last4) for cid, last4, _c in _candidate_rows(job)]


def _candidate_rows(job: ProcurementJob) -> list[tuple[str, str | None, int]]:
    """``(id, last4 | None, top-up cents)`` of the last pre-check (ADR-048: 0 = pays as is)."""
    out: list[tuple[str, str | None, int]] = []
    for part in (job.card_candidates or "").split(","):
        cid, _, rest = part.partition(":")
        last4, _, cents = rest.partition(":")
        if cid.isdigit() and len(cid) <= 13:
            c = int(cents) if cents.isdigit() and len(cents) <= 7 else 0
            out.append((cid, last4 if len(last4) == 4 and last4.isdigit() else None, c))
    return out[: dr.MAX_CARD_TRIES]


def _encode_candidates(res: Result) -> str:
    tops = {c: int(a * 100) for c, a in res.card_topups}
    return ",".join(
        f"{c}:{l4 or ''}" + (f":{tops[c]}" if tops.get(c) else "") for c, l4 in res.card_candidates
    )[:120]


CARD_FRESH_S = 120  # a pre-check's candidates are never spent on after this many seconds


async def _park_for_card(
    ctx: ProcurementContext, db: AsyncSession, job: ProcurementJob, reason: str, now: datetime
) -> None:
    """No card can pay this order now (review-v12 §21.3): the job WAITS, back at the start of the
    send path (``queued`` / ``submit``). A park caused by OUR OWN checks sent nothing, so the
    idempotency key and client request id stay; a park after a vendor refusal re-keys first
    (:func:`_rekey`). No vendor money moves while it waits. The total wait is bound
    (``direct_card_wait_max_s``); then ``needs_manual``. One staff warning audit row per job, at the
    first park. The buyer sees only "waiting for the supplier" (never ``reason``)."""
    before = {"state": job.state, "step": job.step}
    job.card_id = job.card_last4 = job.card_candidates = job.card_candidates_at = None
    await topups.void_planned(db, job)  # a planned top-up that was not sent is released
    first = job.card_wait_since is None
    if first:
        job.card_wait_since = now
    limit = timedelta(seconds=ctx.settings.direct_card_wait_max_s)
    deadline = job.card_wait_since + limit
    job.step = "submit"
    if now >= deadline:
        _unsent(job)
        await _needs_manual(db, job, "card_wait_exhausted")
        return
    if job.state != "queued":  # e.g. submitting -> queued: the move is a transition row
        await _transition(db, job, "queued", f"waiting_for_card:{reason}", severity="warning")
    job.error_code = "waiting_for_card"
    job.next_poll_at = min(now + _backoff(job.attempts, None), deadline)
    if first:
        await audit(
            db,
            tenant_id=job.tenant_id,
            action="procurement.waiting_for_card",
            target=f"procurement_job:{job.id}",
            before=before,
            after={"state": "queued", "step": "submit"},
            detail={
                "order_id": str(job.order_id),
                "reason": clean_detail(reason),
                "max_wait_s": int(limit.total_seconds()),
            },
            severity="warning",
        )


async def _rekey(db: AsyncSession, job: ProcurementJob) -> bool:
    """A NEW attempt key and client request id (both derive from ``card_attempt``) for a job whose
    last send was refused by the vendor BEFORE an order existed and VERIFIED so by a lookup. The
    vendor replays the first answer per key and refuses a changed body under one key, so a parked
    job could never succeed otherwise. ``False`` = the attempts are used up (caller holds it)."""
    if job.card_attempt >= CARD_MAX_REKEYS:
        return False
    old = job.idempotency_key
    job.card_attempt += 1
    job.idempotency_key = job_idempotency_key(job.id.hex, f"c{job.card_attempt}")
    await audit(
        db,
        tenant_id=job.tenant_id,
        action="procurement.card_rekeyed",
        target=f"procurement_job:{job.id}",
        before={"key": old},
        after={"key": job.idempotency_key},
        detail={"order_id": str(job.order_id), "attempt": job.card_attempt},
        severity="warning",
    )
    return True


CARD_MAX_REKEYS = 10
# the adapter's definite top-up refusals: nothing moved, the planned row is settled ``failed``
TOPUP_REFUSED = frozenset({dr.FUNDS_INSUFFICIENT, "topup_below_min", "topup_refused"})


async def _assign_card(
    ctx: ProcurementContext, db: AsyncSession, job: ProcurementJob, offer: OfferRow, now: datetime
) -> bool:
    """Pick the card of an ``auto_existing`` job from the pre-check's usable cards: the first that
    is not a buyer's card and has no order in flight (the same rule as a fixed card). The
    candidates must have been checked within ``CARD_FRESH_S``: an older list is dropped and the
    job re-checks (nothing is parked, nothing sent). The advisory locks of the candidates are
    taken in id order (no lock cycle) and released by the lease commit. ``False`` = parked or
    re-checking."""
    cands = _candidates_of(job)
    at = job.card_candidates_at
    if cands and (at is None or (now - at).total_seconds() > CARD_FRESH_S):
        job.card_candidates = job.card_candidates_at = None
        job.step, job.next_poll_at = "submit", now + timedelta(seconds=1)  # re-check (read-only)
        job.attempts = 0  # a successful pre-check is not a failure to count against the retries
        return False
    for cid, _l in sorted(cands):
        await lock_card(db, offer.supplier_id, cid)
    for _cid, last4 in cands:
        # a buyer card recorded AFTER the pre-check that shares a candidate's last four: the
        # full-mask check must run again before anything is spent (re-audit M2)
        if await buyer_card_since(db, job.tenant_id, last4, at):
            job.card_candidates = job.card_candidates_at = None
            job.step, job.next_poll_at = "submit", now + timedelta(seconds=1)
            job.attempts = 0
            return False
    capped = False
    # a top-up that reached the adapter with an unclear answer keeps its card and its key: the
    # job may only resend THAT top-up (the vendor replays one key once), never plan a second one
    # (payment audit H1); a plan that never reached the adapter is released
    sent_card = await topups.sent_card(db, job)
    if sent_card is None:
        await topups.void_planned(db, job)
    for cid, last4, cents in _candidate_rows(job):
        if sent_card is not None and cid != sent_card:
            continue
        if sent_card is not None:
            if not cents:  # the card now holds the money: never resend the unclear top-up
                topups.forget(job)
            # else: the same key and amount go again (replayed or executed once by the vendor)
            cents = 0
        # a card bought for a buyer is never a funding card (H1); the mask check ran on the
        # pre-check's answer (masks never leave that call) and again for the same card here by id
        if await funding_card_clash(db, job.tenant_id, offer.supplier_id, cid):
            continue
        n = await card_in_flight(db, job.tenant_id, offer.supplier_id, cid, job.id)
        if n >= ctx.settings.direct_card_inflight_max:
            continue
        if cents and not await topups.plan(db, job, cid, Decimal(cents) / 100, now):
            capped = True  # the daily top-up cap: try a card that needs less (or none)
            continue
        job.card_id, job.card_last4 = cid, last4
        job.card_candidates = job.card_candidates_at = None
        job.card_wait_since = None  # the wait is over
        job.attempts = 0
        return True
    if capped:
        # the money exists but today's cap forbids moving it: staff decide (owner: no waiting)
        _unsent(job)
        await _needs_manual(db, job, dr.FUNDING_CAP_REACHED)
        return False
    job.attempts += 1  # the park backoff grows
    await _park_for_card(ctx, db, job, dr.CARD_POOL_BUSY if cands else dr.NO_CARD_PICK, now)
    return False


async def _card_busy(
    ctx: ProcurementContext, db: AsyncSession, job: ProcurementJob, offer: OfferRow
) -> bool:
    """The funding card already pays ``direct_card_inflight_max`` orders at the vendor (M2). The
    check runs under an advisory lock that the lease commit (``maybe_sent``) releases, so two
    concurrent leases cannot both pass."""
    card = offer.params.get("card_id")
    await lock_card(db, offer.supplier_id, card)
    n = await card_in_flight(db, job.tenant_id, offer.supplier_id, card, job.id)
    return n >= ctx.settings.direct_card_inflight_max


async def _open_credential(ctx: ProcurementContext, lease: Lease) -> tuple[str | None, str | None]:
    """The buyer's session, opened IN MEMORY for one supplier call: ``(text, None)`` or
    ``(None, problem)``. The private key lives only in this process; nothing here logs, caches or
    stores the plaintext, and no exception message ever carries it."""
    if ctx.cred_opener is None or lease.order_id is None:
        return None, "credential_unreadable"
    async with tenant_session(ctx.maker, lease.tenant_id) as db:
        row = (
            await db.execute(
                select(ProcurementCredential.blob, ProcurementCredential.product).where(
                    ProcurementCredential.tenant_id == lease.tenant_id,
                    ProcurementCredential.order_id == lease.order_id,
                )
            )
        ).first()
        await db.rollback()
    if row is None:
        return None, "credential_missing"
    if row.product != lease.spec.offer.params.get("product"):
        return None, "credential_mismatch"
    try:
        return (
            ctx.cred_opener.open(
                bytes(row.blob), tenant_id=str(lease.tenant_id), order_id=str(lease.order_id)
            ),
            None,
        )
    except Exception:  # VaultError or anything else: never a message that could carry data
        return None, "credential_unreadable"


async def _keep_for_support(db: AsyncSession, job: ProcurementJob) -> None:
    """A direct order goes to staff: keep its sealed session for support when the buyer agreed
    (review-v12 §26). In a savepoint, so it can never break the job's own transition."""
    from app.procurement import support_session

    try:
        async with db.begin_nested():
            await support_session.keep_for_order(db, job.tenant_id, job.order_id)
    except Exception as exc:  # the sweeper copies it on its next pass anyway
        log.warning("support copy failed job=%s error=%s", job.id, type(exc).__name__)


async def _drop_credential(db: AsyncSession, job: ProcurementJob) -> None:
    """Delete the sealed session of a direct job's order (a terminal state needs none)."""
    if job.kind == "direct":
        await db.execute(
            delete(ProcurementCredential).where(
                ProcurementCredential.tenant_id == job.tenant_id,
                ProcurementCredential.order_id == job.order_id,
            )
        )


LIVE_ORDER = ("pending_payment", "paid", "manual_review")  # a credential may be needed for these


async def sweep_credentials(ctx: ProcurementContext, *, force: bool = False) -> int:
    """Delete sealed buyer sessions that are no longer needed (AGENTS.md §1.1 level of care):

    * older than ``cred_max_age_s`` in any case;
    * every direct job of the order is terminal (fulfilled / failed / voided): staff actions run as
      ``app_api`` (which cannot delete) and rely on this;
    * the order is no longer ``pending_payment`` / ``paid`` / ``manual_review`` (refunded, closed,
      expired, delivered).

    Returns the number of rows deleted. Throttled by ``cred_sweep_interval_s``."""
    now = time.monotonic()
    interval = ctx.settings.cred_sweep_interval_s
    if not force and ctx.cred_swept_at is not None and now - ctx.cred_swept_at < interval:
        return 0
    ctx.cred_swept_at = now
    total = 0
    try:  # half-price check sessions (migration 0032): wipe any that outlived their read
        from app.procurement.halfcheck import wipe_stale

        total += await wipe_stale(ctx)
    except Exception as exc:
        log.error("half-check sweep failed error=%s", type(exc).__name__)
    try:  # redemption attempt log: rows older than the retention (issue #31)
        from app.procurement.redeem_admin import sweep_attempts

        await sweep_attempts(ctx)
    except Exception as exc:
        log.error("redeem log sweep failed error=%s", type(exc).__name__)
    from app.procurement import support_session

    async with tenant_session(ctx.maker, None) as db:
        holders = list(await db.scalars(select(ProcurementCredential.tenant_id).distinct()))
        await db.rollback()
    for tid in holders:  # consented sessions of failing orders are kept for support FIRST
        try:
            async with tenant_session(ctx.maker, tid) as db:
                total += await support_session.keep_failing(db, tid)
                await db.commit()
        except Exception as exc:
            log.error("support copy sweep failed error=%s", type(exc).__name__)
    async with tenant_session(ctx.maker, None) as db:
        res = await db.execute(
            text(
                "DELETE FROM procurement_credentials c WHERE "
                "c.created_at < now() - make_interval(secs => :age) "
                "OR (EXISTS (SELECT 1 FROM procurement_jobs j WHERE j.order_id = c.order_id "
                "AND j.kind = 'direct') AND NOT EXISTS (SELECT 1 FROM procurement_jobs j "
                "WHERE j.order_id = c.order_id AND j.kind = 'direct' "
                "AND j.state NOT IN ('fulfilled', 'failed', 'voided')))"
            ),
            {"age": float(ctx.settings.cred_max_age_s)},
        )
        total += int(res.rowcount or 0)  # type: ignore[attr-defined]
        tenants = list(await db.scalars(select(ProcurementCredential.tenant_id).distinct()))
        await db.commit()
    for tid in tenants:  # orders are RLS-protected: the order-status rule runs tenant-pinned
        async with tenant_session(ctx.maker, tid) as db:
            res = await db.execute(
                text(
                    "DELETE FROM procurement_credentials c USING orders o "
                    "WHERE o.id = c.order_id AND o.tenant_id = c.tenant_id "
                    "AND o.status <> ALL(:live)"
                ),
                {"live": list(LIVE_ORDER)},
            )
            total += int(res.rowcount or 0)  # type: ignore[attr-defined]
            await db.commit()
    try:  # support copies: past the window, or the order went through after all
        async with tenant_session(ctx.maker, None) as db:
            kept = list(await db.scalars(text("SELECT DISTINCT tenant_id FROM support_sessions")))
            await db.rollback()
        total += await support_session.sweep(ctx, kept)
    except Exception as exc:
        log.error("support session sweep failed error=%s", type(exc).__name__)
    return total


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
        return Result(
            Outcome.OK,
            funding_mask=av.funding_mask,
            card_candidates=av.card_candidates,
            candidate_masks=av.candidate_masks,
            card_topups=av.card_topups,
            card_opened=av.card_opened,
        )
    reason = clean_detail(av.reason or "unavailable")
    if reason in dr.WAIT_REASONS:  # no card right now: a wait, never a refusal of the plan
        return Result(Outcome.RETRYABLE, error_code=reason)
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
            if lease.spec.offer.kind == "direct" and not lease.spec.upstream_ref:
                # the buyer's session is opened here, in memory, for this one call; a job that
                # already knows its order never needs it (it only reads)
                cred, problem = await _open_credential(ctx, lease)
                if cred is None:
                    return Result(Outcome.FATAL, error_code=problem or "credential_unreadable")
                from app.modules.catalog.regions import is_half_plan

                if is_half_plan(lease.spec.offer.params.get("plan")) and lease.order_id:
                    # the order's session must be the account the half-price check was made for:
                    # compared HERE (the session is sealed for the API), before any vendor call
                    from app.procurement.halfcheck import verify_account

                    async with tenant_session(ctx.maker, lease.tenant_id) as vdb:
                        refused = await verify_account(
                            ctx,
                            vdb,
                            lease.tenant_id,
                            lease.order_id,
                            str(lease.spec.offer.params.get("product") or "gpt"),
                            cred,
                        )
                        await vdb.rollback()
                    if refused:
                        del cred
                        return Result(Outcome.FATAL, error_code=refused)
                spec = replace(lease.spec, credential=cred)
                del cred
                try:
                    return await sup.submit(spec)
                finally:
                    del spec
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
    ctx: ProcurementContext,
    db: AsyncSession,
    job: ProcurementJob,
    order: OrderRow,
    res: Result,
    *,
    allow_held: bool = False,
) -> None:
    # the reference and the cost are recorded before anything can refuse the code (L1): a code
    # the supplier issued must stay findable so it can be disabled
    job.upstream_ref = res.upstream_ref or job.upstream_ref
    if res.cost is not None:
        job.cost, job.currency = res.cost.amount, res.cost.currency
    if order.status != "paid" and not (allow_held and order.status == "manual_review"):
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
    # a card is ONE sealed secret ``<pan>----<MM/YY>----<cvv>``. Its dedup fingerprint is NOT
    # derived from the secret (cardvault.card_fingerprint: the card id) and it has no lookup_fp:
    # a keyed hash over a PAN/expiry/CVV is brute-forceable by anyone holding fp_key (S1)
    vcc = job.kind == "vcc"
    direct = job.kind == "direct"  # a short non-secret delivery text, keyed by the vendor order
    if vcc:
        fingerprint = sealer.card_fingerprint(tid, job.upstream_ref or f"job:{job.id}")
    elif direct:
        fingerprint = sealer.card_fingerprint(
            tid, f"direct:{job.supplier_id}:{job.upstream_ref or job.id}"
        )
    else:
        fingerprint = sealer.fingerprint(tid, code)
    lookup_fp = None if (vcc or direct) else sealer.lookup_fingerprint(tid, code)
    values = {
        "id": card_id,
        "tenant_id": order.tenant_id,
        "product_id": order.product_id,
        "variant_id": order.variant_id,
        "blob": sealer.seal(code, CardContext(tid, str(order.product_id), str(card_id))),
        "fingerprint": fingerprint,
        "lookup_fp": lookup_fp,
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
    job.upstream_status = ISSUED_STATUS.get(job.kind, "unused")
    job.error_code = None
    job.step = "done"
    job.next_poll_at = None
    await _transition(db, job, "fulfilled", "supplier_ok")
    await _drop_credential(db, job)  # the supplier is done: the buyer's session is not needed
    await emit_if_complete(db, order)


VOID_PENDING_MAX = 100  # ~24 h of cheap GET polls (capped at 15 min) for a pending delete


async def _void_failed(db: AsyncSession, job: ProcurementJob, reason: str) -> None:
    """A void the supplier refused or that never resolved: the sealed card stays voided locally
    but the supplier may still hold a live code/card, so staff must look (never back to
    ``fulfilled``). A critical audit row names the reason (no secret)."""
    await audit(
        db,
        tenant_id=job.tenant_id,
        action="procurement.void_failed",
        target=f"procurement_job:{job.id}",
        detail={"reason": clean_detail(reason), "order_id": str(job.order_id)},
        severity="critical",
    )
    job.step = "done"
    await _needs_manual(db, job, "void_failed", notify_order=False)


async def _apply_void(db: AsyncSession, job: ProcurementJob, res: Result, now: datetime) -> None:
    """``void_pending`` -> ``voided`` once the supplier disabled/deleted the code; a refusal
    ("already redeemed", unknown code) or an exhausted retry budget ends in ``needs_manual`` with
    ``void_failed``. A pending delete (202) keeps polling for ~24 h without re-sending."""
    o = res.outcome
    if o is Outcome.OK:
        job.step, job.next_poll_at, job.error_code = "done", None, None
        job.upstream_status = VOIDED_STATUS.get(job.kind, "disabled")
        await _transition(db, job, "voided", "void_ok")
    elif o is Outcome.ACCEPTED_PENDING:
        job.error_code = clean_detail(res.error_code or "void_pending")
        if job.attempts > VOID_PENDING_MAX:
            await _void_failed(db, job, f"void_unresolved:{res.error_code or ''}")
            return
        if res.error_code == "delete_pending":
            job.step = "delete_pending"  # from now on only the card is read
        job.next_poll_at = now + _backoff(job.attempts, res.retry_after)
    elif o is Outcome.FATAL or job.attempts > MAX_RETRIES:
        await _void_failed(db, job, f"void_rejected:{res.error_code or 'unresolved'}")
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
    *,
    auto_card: bool = False,
) -> None:
    """Nothing was sent. Available -> ready to submit; the catalog says no -> next source (or
    ``needs_manual`` without one) and the offer is marked unchecked; a failed lookup is retried."""
    o = res.outcome
    if res.card_opened is not None and job.kind == "direct":
        # ADR-049: the check opened a funding card from the wallet: it counts against the cap
        await topups.record_open(db, job, *res.card_opened)
    if o is Outcome.OK and auto_card and not res.card_candidates:
        await _park_for_card(ctx, db, job, dr.NO_CARD_PICK, now)
    elif o is Outcome.OK:
        # attempts is NOT reset: a submit that keeps answering RETRYABLE must reach MAX_RETRIES (N7)
        job.step, job.error_code, job.next_poll_at = "ready", None, now
        if auto_card:
            job.card_candidates = _encode_candidates(res)
            job.card_candidates_at = now
        await _set_check(db, job, True)
    elif o is Outcome.UNAVAILABLE:
        await _set_check(db, job, False)
        # the catalog's own reason is what staff must see (``region_not_offered``: the vendor
        # withdrew the region after the sale)
        await _failover(
            ctx,
            db,
            job,
            order,
            "region_not_offered" if res.error_code == "region_not_offered" else "plan_unavailable",
        )
    elif res.error_code in dr.WAIT_REASONS and auto_card:
        await _park_for_card(ctx, db, job, res.error_code or dr.NO_CARD_PICK, now)
    elif res.error_code in dr.MANUAL_NOW and job.kind == "direct":
        # ADR-048 (owner 2026-11-19): no card / no money / no price for a PAID order: another
        # source if one is usable, else staff at once (never the 30-minute card wait)
        await _set_check(db, job, False)
        await topups.void_planned(db, job)
        await _failover(ctx, db, job, order, res.error_code or dr.FUNDS_INSUFFICIENT)
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
    elif o is Outcome.ACCEPTED_PENDING:
        job.error_code = clean_detail(res.error_code or "void_pending")
        if job.attempts > VOID_PENDING_MAX:
            await _dvoid_failed(db, job, f"void_unresolved:{res.error_code or ''}")
            return
        if res.error_code == "delete_pending":
            job.step = "delete_pending"
        job.next_poll_at = now + _backoff(job.attempts, res.retry_after)
    elif o is Outcome.FATAL or job.attempts > MAX_RETRIES:
        await _dvoid_failed(db, job, f"void_rejected:{res.error_code or 'unresolved'}")
    else:
        job.error_code = clean_detail(res.error_code or "void_retry")
        job.next_poll_at = now + _backoff(job.attempts, res.retry_after)


async def _dvoid_failed(db: AsyncSession, job: ProcurementJob, reason: str) -> None:
    job.recover, job.next_poll_at = False, None
    if job.state == "failed":
        await _void_failed(db, job, reason)
    else:  # a hand-fulfilled job: only the critical audit row
        await _stop_recovery(db, job, reason)


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


def _record_progress(job: ProcurementJob, vendor: Any, source: str, now: datetime) -> None:
    """Staff timeline + buyer step bookkeeping is best effort: a corrupt ``progress`` document
    must never abort the real state transition around it (the old document is kept)."""
    try:
        job.progress, _ = progress.record(job.progress, vendor, source, now)
    except Exception as exc:
        log.error("progress record failed error=%s", type(exc).__name__)


def _record_reference(job: ProcurementJob, res: Result, mode: str) -> str | None:
    """A multi-step purchase (a card) knows its supplier reference before it is finished: record it
    on EVERY answer that carries one (ambiguous, pending, refused after the open), so the job is
    never "nothing happened" once the supplier holds something for it and a void can find it."""
    other: str | None = None
    if mode in ("submit", "query", "verify", "recover") and res.upstream_ref:
        if job.upstream_ref and job.upstream_ref != res.upstream_ref:
            other = res.upstream_ref  # a SECOND card for one job: never silently adopted
        job.upstream_ref = job.upstream_ref or res.upstream_ref
        if res.cost is not None and job.cost is None:
            job.cost, job.currency = res.cost.amount, res.cost.currency
    if res.card_mask and not job.card_mask:
        job.card_mask = res.card_mask[:24]
    if res.detail:
        job.upstream_status = clean_detail(res.detail, 32)  # direct: vendor status:fee status
    if res.vendor and job.kind == "direct":  # issue #30: what CHANGED goes to the staff timeline
        _record_progress(job, res.vendor, "poll", utcnow())
    if res.account_mask and not job.account_mask:
        job.account_mask = res.account_mask[:64]  # masked e-mail only, never the session
    if res.preflight_expires_at is not None:
        job.preflight_expires_at = res.preflight_expires_at
    return other


def _sent_card(job: ProcurementJob, params: dict[str, Any]) -> str | None:
    """The funding card WE sent the order with: the engine's pick, else the offer's fixed card."""
    if job.card_id:
        return job.card_id
    fixed = params.get("card_id")
    return str(fixed) if isinstance(fixed, int) and not isinstance(fixed, bool) else None


async def _reported_card_problem(
    db: AsyncSession, job: ProcurementJob, res: Result, lease: Lease
) -> str | None:
    """The vendor says which card paid the order. We never trust it over what we sent: it is
    recorded SEPARATELY (``used_card_id`` / ``used_last4``; ``card_id`` stays the sent card) and
    checked on every vendor read. A different card is a discrepancy -> ``needs_manual``
    ``card_mismatch``; with ``vendor_switch`` on (never for a production supplier) it is allowed
    unless it is a card bought for a buyer (id, and number when the answer carries one) ->
    ``switched_to_buyer_card``. A reported id that is not a card id of at most 20 digits is a
    discrepancy too (never truncated)."""
    if job.kind != "direct" or lease.mode not in ("submit", "query", "verify", "watch"):
        return None
    if not res.card_id:
        return None
    reported = res.card_id
    if not reported.isdigit() or len(reported) > 20:
        return "card_mismatch" if lease.mode != "watch" else None
    job.used_card_id = reported
    if lease.mode == "watch":  # a held job: only the in-flight identity is kept up to date
        job.used_last4 = res.card_last4
        return None
    params = lease.spec.offer.params
    sent = _sent_card(job, params)
    if sent is None or reported == sent:
        job.used_last4 = res.card_last4 or (job.card_last4 if reported == sent else None)
        return None
    job.used_last4 = res.card_last4
    if not (params.get("vendor_switch") is True and lease.ref.env != "production"):
        return "card_mismatch"
    if await funding_card_clash(db, job.tenant_id, job.supplier_id, reported) or (
        await funding_mask_clash(db, job.tenant_id, res.funding_mask)
    ):
        return "switched_to_buyer_card"
    return None


async def _card_discrepancy(
    db: AsyncSession, job: ProcurementJob, reason: str, reported: str | None
) -> None:
    """Staff warning (card ids are not secrets; never a number) and the job is held, flagged maybe
    charged: an order ran on a card we did not choose or may not use."""
    await audit(
        db,
        tenant_id=job.tenant_id,
        action="procurement.card_discrepancy",
        target=f"procurement_job:{job.id}",
        detail={
            "order_id": str(job.order_id),
            "reason": reason,
            "sent": job.card_id,
            "reported": clean_detail(reported, 24),
        },
        severity="warning",
    )
    job.maybe_sent = True
    await _needs_manual(db, job, reason)


async def _card_mismatch(db: AsyncSession, job: ProcurementJob, other: str) -> None:
    """The supplier answered with a card id other than the one recorded: two cards may exist for
    one job. Nothing is sealed; both ids (not secrets) go to a critical audit row and the job is
    parked for staff, flagged maybe charged (it keeps ``upstream_ref`` and ``maybe_sent``)."""
    await audit(
        db,
        tenant_id=job.tenant_id,
        action="procurement.card_mismatch",
        target=f"procurement_job:{job.id}",
        detail={
            "recorded": clean_detail(job.upstream_ref),
            "answered": clean_detail(other),
            "order_id": str(job.order_id),
        },
        severity="critical",
    )
    job.maybe_sent = True
    await _needs_manual(db, job, "card_mismatch")


DECLINES = DECLINE_CODES  # one list: the console risk monitor counts the same outcomes


async def _direct_fatal(db: AsyncSession, job: ProcurementJob, res: Result, limit: int = 3) -> None:
    """Direct-only consequences of a FATAL answer: a COMPLETED order that is not provably ours is
    a critical audit row (M3); repeated declines on one source switch the source off (M2)."""
    code = res.error_code or ""
    if code == "order_mismatch":
        await audit(
            db,
            tenant_id=job.tenant_id,
            action="procurement.order_mismatch",
            target=f"procurement_job:{job.id}",
            detail={
                "order_id": str(job.order_id),
                "upstream_ref": clean_detail(job.upstream_ref or ""),
                "detail": clean_detail(res.detail or ""),
            },
            severity="critical",
        )
    elif code in DECLINES and job.offer_id is not None:
        recent = list(
            await db.scalars(
                select(ProcurementJob.error_code)
                .where(
                    ProcurementJob.tenant_id == job.tenant_id,
                    ProcurementJob.offer_id == job.offer_id,
                    ProcurementJob.kind == "direct",
                )
                .order_by(ProcurementJob.updated_at.desc())
                .limit(limit)
            )
        )
        if len(recent) >= limit and all(c in DECLINES for c in recent):
            # DURABLE: the source is disabled (a precheck / refresh cannot switch it back on and
            # routing stops); re-enabling is a staff action in the console
            await db.execute(
                update(SupplierOffer)
                .where(SupplierOffer.tenant_id == job.tenant_id, SupplierOffer.id == job.offer_id)
                .values(enabled=False, last_check_ok=False, last_check_at=utcnow())
            )
            await audit(
                db,
                tenant_id=job.tenant_id,
                action="procurement.offer_declining",
                target=f"supplier_offer:{job.offer_id}",
                detail={"consecutive": limit, "last": clean_detail(code)},
                severity="critical",
            )


async def _vet_candidates(db: AsyncSession, job: ProcurementJob, res: Result) -> Result:
    """Auto mode: every candidate the vendor's pick offered runs the buyer-card checks (id AND
    masked number, H1) BEFORE it is stored; a clashing card is dropped and never spent on. The
    masks live only in this call: they are not stored, logged or audited."""
    masks = dict(res.candidate_masks)
    keep: list[tuple[str, str | None]] = []
    for cid, last4 in res.card_candidates:
        if await funding_card_clash(db, job.tenant_id, job.supplier_id, cid):
            continue
        if await funding_mask_clash(db, job.tenant_id, masks.get(cid)):
            continue
        keep.append((cid, last4))
    kept = {cid for cid, _l in keep}
    return Result(
        Outcome.OK,
        funding_mask=res.funding_mask,
        card_candidates=tuple(keep),
        card_topups=tuple((c, a) for c, a in res.card_topups if c in kept),
        card_opened=res.card_opened,
    )  # (masks dropped)


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
        other = _record_reference(job, res, lease.mode)
        if other is not None:
            await _card_mismatch(db, job, other)
            await db.commit()
            return
        if lease.ref.adapter == "zzshu" and job.kind == "direct":
            await _zzshu_card(db, job, res, lease.mode, now)
        if problem := await _reported_card_problem(db, job, res, lease):
            await _card_discrepancy(db, job, problem, res.card_id)
            await db.commit()
            return
        if lease.mode == "watch":
            await _apply_watch(ctx, db, job, order, res, now, lease.spec.cost_bound)
            await db.commit()
            return
        if lease.mode in ("recover", "dvoid"):
            await _apply_detached(db, job, lease, res, now)
            await db.commit()
            return
        if lease.mode == "precheck":
            if (
                job.kind == "direct"
                and res.outcome is Outcome.OK
                and await funding_mask_clash(db, job.tenant_id, res.funding_mask)
            ):  # the funding card is a card bought for a buyer (number match): never use it
                res = Result(Outcome.UNAVAILABLE, error_code="funding_card_invalid")
            if job.kind == "direct" and res.outcome is Outcome.OK and res.card_candidates:
                res = await _vet_candidates(db, job, res)
            await _apply_precheck(
                ctx, db, job, order, res, now,
                auto_card=lease.spec.offer.params.get("card_mode", "fixed") != "fixed",
            )  # fmt: skip
            await db.commit()
            return
        if lease.mode == "submit" and job.kind == "direct" and job.fund_key:
            # ADR-048: record what the top-up did before anything else reads the job
            await topups.settle(
                db,
                job,
                funded=res.funded,
                error=res.error_code
                if res.funded is None and res.error_code in TOPUP_REFUSED
                else None,
            )
        if (
            lease.mode == "submit"
            and job.kind == "direct"
            and res.outcome is Outcome.FATAL
            and res.error_code in dr.MANUAL_NOW
        ):
            # refused BEFORE the order POST (no money for it, no price): nothing was sent, staff
            # at once (owner 2026-11-19); a failed top-up moved nothing either
            if not lease.prior_send:
                _unsent(job)
            await _needs_manual(db, job, res.error_code or dr.FUNDS_INSUFFICIENT)
            await db.commit()
            return
        if (
            lease.mode == "submit"
            and job.kind == "direct"
            and lease.spec.offer.params.get("card_mode", "fixed") != "fixed"
            and not lease.prior_send
            and job.upstream_ref is None
            and res.error_code in (dr.CARD_REFUSAL_CODES | dr.CARD_GUESSED_CODES)
            and o in (Outcome.UNAVAILABLE, Outcome.AMBIGUOUS, Outcome.FATAL, Outcome.RETRYABLE)
        ):
            # the vendor answered a "no card" code. Only a DOCUMENTED refusal whose lookup by
            # client request id VERIFIED that no order exists may park the job and re-key it; a
            # guessed code, or a refusal we could not verify, is held for staff (maybe sent: the
            # order may exist). Never a blind second key (review-v12 §21.3)
            documented = res.error_code in dr.CARD_REFUSAL_CODES
            if documented and res.no_order_verified and await _rekey(db, job):
                _unsent(job)
                await _park_for_card(ctx, db, job, res.error_code or dr.NO_CARD_PICK, now)
            else:
                await _needs_manual(
                    db,
                    job,
                    "card_refusal_unverified" if documented else "card_code_unconfirmed",
                )
            await db.commit()
            return
        if o is Outcome.OK:
            await _fulfil(ctx, db, job, order, res)
            await _check_cost_bound(db, job, res, lease.spec.cost_bound)
        elif o is Outcome.ACCEPTED_PENDING:
            if job.state != "accepted_pending":
                job.step, job.attempts = "pending", 0
                job.upstream_ref = res.upstream_ref or job.upstream_ref
                await _transition(db, job, "accepted_pending", "accepted")
            if (
                job.kind == "direct"
                and job.first_sent_at is not None
                and now - job.first_sent_at > timedelta(seconds=ctx.settings.direct_pending_max_s)
            ):
                # a subscription takes minutes: an order that never reaches a terminal state is a
                # staff matter (requery, fulfil by hand, fail); it is still flagged maybe charged
                await _needs_manual(db, job, "pending_too_long")
                await db.commit()
                return
            step = POLL_STEPS[min(job.attempts, len(POLL_STEPS) - 1)]
            job.next_poll_at = now + timedelta(seconds=step)
        elif o is Outcome.RETRYABLE:
            job.error_code = clean_detail(res.error_code or "retryable")
            if lease.mode == "submit":
                job.step = "submit"  # a RETRYABLE write was not executed
                if not (job.kind == "direct" and lease.prior_send):
                    _unsent(job)  # (a direct refusal before the order says nothing about an
                    # EARLIER send of the same job)
            if lease.ref.adapter == "zzshu" and res.error_code == "card_opened":
                # the dedicated card is open and now recorded: send at once, not a failed attempt
                job.attempts = max(0, job.attempts - 1)
                job.next_poll_at = now
            elif job.attempts > MAX_RETRIES:
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
        elif (
            o is Outcome.UNAVAILABLE
            and job.kind == "direct"
            and (lease.prior_send or job.upstream_ref)
            and lease.mode in ("submit", "verify")
        ):
            # a direct job that may already have an order at the vendor: a "no" now says nothing
            # about that earlier send. Never failed over, never "unsent": it stays in reconcile
            # (reads, then a same-key resubmit the vendor de-duplicates) and ends in needs_manual,
            # flagged maybe charged (H2)
            job.error_code = clean_detail(res.error_code or "unavailable")
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
        elif (
            o is Outcome.FATAL
            and job.kind == "direct"
            and lease.mode in ("submit", "query", "verify")
            and await _precharge_failover(ctx, db, job, order, res, lease)
        ):
            pass  # ADR-054: moved to the standby source, nothing was charged
        else:  # FATAL: a deterministic refusal, nothing was executed for this key
            if res.error_code != "IDEMPOTENCY_CONFLICT" and not (
                job.kind == "direct" and lease.prior_send
            ):
                _unsent(job)  # (a conflicting key HAS been used: the first request may have run)
            await _needs_manual(db, job, res.error_code or "fatal")
            if job.kind == "direct":
                await _direct_fatal(db, job, res, ctx.settings.direct_decline_limit)
        await _alert_if_stuck(db, job, now)
        await db.commit()


async def _zzshu_card(
    db: AsyncSession, job: ProcurementJob, res: Result, mode: str, now: datetime
) -> None:
    """ADR-054: what staff and support see of a zzshu order's dedicated card: its id and last four
    (never a number), what was loaded and what went back to the wallet, in the job record, the
    audit log and the job timeline. A card that is left with money after the order is over gets a
    warning row (the API alerts staff)."""
    if res.card_id and res.card_id.isdigit() and len(res.card_id) <= 13 and not job.card_id:
        job.card_id = res.card_id
    if res.card_last4 and res.card_last4.isdigit() and len(res.card_last4) == 4:
        job.card_last4 = job.card_last4 or res.card_last4
    if res.card_opened is not None and mode == "submit":
        await topups.record_open(db, job, *res.card_opened)  # counts against the daily cap
    cid = clean_detail(res.card_id or job.card_id or "", 20)
    events: list[dict[str, str]] = []
    if res.card_loaded is not None:
        events.append(
            {"tid": f"zzcard-open-{cid}", "cat": "zzshu", "code": "card_opened",
             "msg": f"Dedicated card {job.card_last4 or '-'} opened and loaded "
             f"{res.card_loaded:.2f} USD"}
        )  # fmt: skip
    if res.card_swept:
        events.append(
            {"tid": f"zzcard-swept-{cid}", "cat": "zzshu", "code": "card_swept",
             "msg": f"Dedicated card {job.card_last4 or '-'} returned "
             f"{(res.card_leftover or Decimal(0)):.2f} USD to the wallet and was deleted"}
        )  # fmt: skip
    if res.card_loaded is not None or res.card_swept:
        await audit(
            db,
            tenant_id=job.tenant_id,
            action="procurement.zzshu_card",
            target=f"procurement_job:{job.id}",
            detail={
                "order_id": str(job.order_id),
                "card_id": cid,
                "last4": job.card_last4,
                "loaded": str(res.card_loaded) if res.card_loaded is not None else None,
                "leftover": str(res.card_leftover) if res.card_leftover is not None else None,
                "swept": res.card_swept,
                "currency": "USD",
            },
            severity="info",
        )
    if events:
        _record_progress(job, {"events": events}, "engine", now)
    over = res.outcome in (Outcome.OK, Outcome.FATAL, Outcome.UNAVAILABLE)
    if over and res.card_id and not res.card_swept:
        _record_progress(
            job,
            {"events": [{"tid": f"zzcard-unswept-{cid}", "cat": "zzshu", "code": "card_unswept",
                         "msg": f"Dedicated card {job.card_last4 or '-'} still holds money"}]},
            "engine",
            now,
        )  # fmt: skip
        await audit(
            db,
            tenant_id=job.tenant_id,
            action="procurement.zzshu_card_unswept",
            target=f"procurement_job:{job.id}",
            detail={"order_id": str(job.order_id), "card_id": cid, "last4": job.card_last4},
            severity="warning",
        )


async def _publish_progress(
    ctx: ProcurementContext, tenant_id: uuid.UUID, job_id: uuid.UUID
) -> None:
    """Tell the API when the buyer's STEP changed since the last SUCCESSFUL notification (the step
    last notified lives in ``progress.pub``; it is only set after Redis accepted the message, so a
    failed publish is retried on the next pass). The message carries ids only; the API derives the
    buyer and the progress itself. Nothing is written, and no row lock taken, on a pass where
    nothing changed. Never raises."""
    try:
        async with tenant_session(ctx.maker, tenant_id) as db:
            job = await db.scalar(
                select(ProcurementJob).where(
                    ProcurementJob.id == job_id, ProcurementJob.tenant_id == tenant_id
                )
            )
            if job is None or job.kind != "direct":
                return
            order_status = await db.scalar(
                select(Order.status).where(Order.id == job.order_id, Order.tenant_id == tenant_id)
            )
            offer = await _offer_any(db, job)
            half = bool(offer and dr.is_half_plan((offer.params or {}).get("plan")))
            p = progress.from_job(job, half=half, order_refunded=order_status == "refunded")
            doc = progress.stamp(job.progress, p.step, utcnow())
            order_id, old = job.order_id, dict(job.progress or {})
            if doc == old and doc.get("pub") == p.step:
                return  # nothing changed: no write, no lock
            await db.rollback()
            if doc.get("reached") != old.get("reached"):
                await _write_progress(db, job_id, tenant_id, reached_step=p.step)
        if doc.get("pub") != p.step and await notify_progress_changed(
            ctx.redis, tenant_id, order_id
        ):
            async with tenant_session(ctx.maker, tenant_id) as db:
                await _write_progress(db, job_id, tenant_id, pub_step=p.step)
    except Exception as exc:
        log.error("progress publish failed error=%s", type(exc).__name__)


async def _write_progress(
    db: AsyncSession,
    job_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    reached_step: str | None = None,
    pub_step: str | None = None,
) -> None:
    """Merge one bookkeeping key into the (freshly locked) progress document and commit."""
    job = await db.scalar(
        select(ProcurementJob)
        .where(ProcurementJob.id == job_id, ProcurementJob.tenant_id == tenant_id)
        .with_for_update()
    )
    if job is None:
        await db.rollback()
        return
    doc = dict(job.progress or {})
    if reached_step is not None:
        doc = progress.stamp(doc, reached_step, utcnow())
    if pub_step is not None:
        doc["pub"] = pub_step
    job.progress = doc
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
    if lease.mode != "void":
        await _publish_progress(ctx, lease.tenant_id, lease.job_id)
    if lease.mode == "precheck":
        await process_job(ctx, job_id, tenant_id)  # a passed check sends in the same pass
    if charged and lease.spec.offer.kind == "direct" and lease.mode not in ("void", "precheck"):
        try:
            await sweep_leftovers(ctx, lease.tenant_id, lease.ref, lease.spec.offer)
        except Exception as exc:  # best effort: money stays on the card, nothing is lost
            log.error("leftover sweep failed error=%s", type(exc).__name__)
    if charged:  # review-v12 §6: the balance (and the breaker) is refreshed after every job
        try:
            bal = await refresh_balance(ctx, lease.ref)
            if bal is not None and lease.spec.offer.kind == "direct":
                # the storefront's sold-out state follows the money right away, not in 5 minutes
                await refresh_offers(ctx, lease.ref)
        except Exception as exc:
            log.error("post-job balance refresh failed error=%s", type(exc).__name__)
    return True


async def sweep_leftovers(
    ctx: ProcurementContext, tenant_id: uuid.UUID, ref: SupplierRef, offer: OfferSpec
) -> int:
    """ADR-049 (owner 2026-11-21): after a direct order, move the money of our cards whose uses
    for that tier are spent back to the supplier wallet (``sweep_leftover``, under the master
    ``auto_recharge`` switch). Never a card bought for a buyer (id and masked number checks, as
    for funding candidates) and never one with an order in flight. Returns the cards swept."""
    async with tenant_session(ctx.maker, tenant_id) as db:
        on = topups.sweep_on(await topups.settings_of(db, tenant_id))
        await db.rollback()
    if not on:
        return 0
    sup = engine_supplier(ctx, ref)
    find = getattr(sup, "leftover_cards", None)
    sweep = getattr(sup, "sweep_card", None)
    if find is None or sweep is None:
        return 0
    cards = await find(offer)
    if not cards:
        return 0
    keep: list[tuple[str, Decimal]] = []
    async with tenant_session(ctx.maker, tenant_id) as db:
        for cid, mask, amount in cards:
            if await funding_card_clash(db, tenant_id, ref.id, cid):
                continue
            if mask and await funding_mask_clash(db, tenant_id, mask):
                continue
            busy = await db.scalar(
                select(func.count(ProcurementJob.id)).where(
                    ProcurementJob.tenant_id == tenant_id,
                    ProcurementJob.state.in_(IN_FLIGHT_STATES),
                    func.coalesce(ProcurementJob.used_card_id, ProcurementJob.card_id) == cid,
                )
            )
            if not busy:
                keep.append((cid, amount))
        await db.rollback()
    swept = 0
    hour = utcnow().strftime("%Y%m%d%H")
    for cid, amount in keep:
        key = f"sweep:{cid}:{int(amount * 100)}:{hour}"
        if not await sweep(cid, amount, key):
            continue
        swept += 1
        async with tenant_session(ctx.maker, tenant_id) as db:
            await topups.record_sweep(db, tenant_id, ref.id, cid, amount, key)
            await db.commit()
    return swept


async def sweep_all(ctx: ProcurementContext) -> int:
    """Scheduled leftover sweep (owner 2026-11-21): for every enabled supplier with an enabled
    direct offer of a standard tier, :func:`sweep_leftovers` (same rules as after an order)."""
    from app.procurement.service import enabled_suppliers

    swept = 0
    for ref in await enabled_suppliers(ctx):
        try:
            async with tenant_session(ctx.maker, ref.tenant_id) as db:
                # a plain column, read inside the session (an ORM row expires on rollback)
                params = await db.scalar(
                    select(SupplierOffer.params)
                    .where(
                        SupplierOffer.tenant_id == ref.tenant_id,
                        SupplierOffer.supplier_id == ref.id,
                        SupplierOffer.kind == "direct",
                        SupplierOffer.enabled.is_(True),
                        SupplierOffer.params["plan"].astext.not_like("pro_20x%"),
                    )
                    .order_by(SupplierOffer.priority)
                    .limit(1)
                )
                await db.rollback()
            if params is None:
                continue
            offer = OfferSpec(kind="direct", params=dict(params))
            swept += await sweep_leftovers(ctx, ref.tenant_id, ref, offer)
        except Exception as exc:  # one supplier must not stop the others
            log.error("scheduled sweep failed supplier=%s error=%s", ref.id, type(exc).__name__)
    return swept


async def charge_check(
    ctx: ProcurementContext, tenant_id: uuid.UUID, job_id: uuid.UUID
) -> dict[str, Any]:
    """READ-ONLY evidence for staff before a hand top-up / reissue (owner 2026-11-19, ADR-048):
    the supplier order's state (as :func:`direct_order_state`), the job's automatic top-ups, and
    the latest spends on the funding card it used. No card number; the vendor's times are shown
    as written (their zone is not documented) next to our UTC times."""
    out: dict[str, Any] = dict(await direct_order_state(ctx, tenant_id, job_id))
    async with tenant_session(ctx.maker, tenant_id) as db:
        job = await db.scalar(
            select(ProcurementJob).where(
                ProcurementJob.id == job_id, ProcurementJob.tenant_id == tenant_id
            )
        )
        if job is None:
            raise LookupError("job")
        offer = await _offer_any(db, job) if job.offer_id else None
        card = job.used_card_id or job.card_id
        if card is None and offer is not None and job.kind == "direct":
            fixed = offer.params.get("card_id")
            card = str(fixed) if isinstance(fixed, int) and not isinstance(fixed, bool) else None
        rows = (
            await db.execute(
                select(ProcurementFunding)
                .where(
                    ProcurementFunding.tenant_id == tenant_id, ProcurementFunding.job_id == job_id
                )
                .order_by(ProcurementFunding.created_at)
            )
        ).scalars()
        out["topups"] = [
            {"state": r.state, "card_id": r.card_id, "amount": str(r.amount), "error": r.error}
            for r in rows
        ]
        out["job_created_at"] = job.created_at.isoformat() if job.created_at else None
        out["first_sent_at"] = job.first_sent_at.isoformat() if job.first_sent_at else None
        await db.rollback()
    out["card_id"] = card
    out["charges"] = None
    if card and card.isdigit() and offer is not None:
        sup = engine_supplier(ctx, offer.ref)
        reader = getattr(sup, "card_charges", None)
        if reader is not None:
            try:
                out["charges"] = await reader(int(card))
            except Exception as exc:  # unreadable: staff see "unknown", never "not charged"
                log.error("card charges read failed error=%s", type(exc).__name__)
    return out


async def direct_order_state(
    ctx: ProcurementContext, tenant_id: uuid.UUID, job_id: uuid.UUID
) -> dict[str, str | None]:
    """READ-ONLY state of a direct job's supplier order for the console (hand fulfil, M4): the
    recorded order, else the vendor's order list searched for our request id. ``state`` is one of
    ``not_sent`` | ``completed`` | ``pending`` | ``failed`` | ``not_found`` | ``unknown``."""
    async with tenant_session(ctx.maker, tenant_id) as db:
        job = await db.scalar(
            select(ProcurementJob).where(
                ProcurementJob.id == job_id, ProcurementJob.tenant_id == tenant_id
            )
        )
        if job is None:
            raise LookupError("job")
        if job.kind != "direct" or job.offer_id is None or not (job.maybe_sent or job.upstream_ref):
            return {"state": "not_sent", "upstream_ref": job.upstream_ref}
        offer = await _offer_any(db, job)
        spec = _spec(job, offer, "query", ctx.settings.direct_quote_tolerance) if offer else None
        # the rollback expires every attribute of ``job`` (issue #54 F3): keep the primitive
        known_ref = job.upstream_ref
        await db.rollback()
    if offer is None or spec is None:
        return {"state": "unknown", "upstream_ref": known_ref}
    try:
        res = await engine_supplier(ctx, offer.ref).query(spec)
    except Exception as exc:
        log.error("direct order read failed error=%s", type(exc).__name__)
        return {"state": "unknown", "upstream_ref": known_ref}
    state = {
        Outcome.OK: "completed",
        Outcome.ACCEPTED_PENDING: "pending",
        Outcome.FATAL: "failed",
        Outcome.UNAVAILABLE: "not_found",
    }.get(res.outcome, "unknown")
    if res.outcome is Outcome.FATAL and res.error_code in ("order_mismatch", "plan_mismatch"):
        state = "unknown"
    return {"state": state, "upstream_ref": res.upstream_ref or known_ref}


async def due_jobs(ctx: ProcurementContext, limit: int = 20) -> list[tuple[uuid.UUID, uuid.UUID]]:
    async with tenant_session(ctx.maker, None) as db:
        rows = (
            await db.execute(
                select(ProcurementJob.id, ProcurementJob.tenant_id)
                .where(
                    ProcurementJob.state.in_(WORK)
                    | ProcurementJob.recover.is_(True)
                    | (
                        (ProcurementJob.state == "needs_manual")
                        & (ProcurementJob.kind == "direct")
                        & ProcurementJob.upstream_ref.is_not(None)
                    ),
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
    try:  # first: a failing job pass can never skip the deletion of a buyer's session (S-M1)
        await sweep_credentials(ctx)
    except Exception as exc:
        log.error("session sweep failed error=%s", type(exc).__name__)
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
