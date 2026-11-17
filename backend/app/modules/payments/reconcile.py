"""Bounded query-based recovery; never holds row locks during external I/O."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.db import tenant_session
from app.core.errors import Unavailable
from app.core.resources import Resources
from app.models import Checkout, Message, Order, PaymentAttempt, Tenant
from app.modules.payments import usdt
from app.modules.payments.gateways import routed_resources
from app.modules.payments.settle import NotifyOutcome, apply_checkout_payment, apply_order_payment
from app.payments import bepusdt, epay
from app.payments.zpay import ZPayQueryError

log = logging.getLogger(__name__)


class UnresolvedEvidence(Unavailable):
    """Some original payment evidence could not be resolved.

    ``settled`` carries the outcomes this pass already committed durably, so a caller can still
    broadcast them and raise their staff alerts while failing the request itself closed.
    """

    def __init__(self, message: str = "", settled: NotifyOutcome | None = None) -> None:
        super().__init__(message)
        self.settled = settled


async def _carrying(
    db: AsyncSession, tid: uuid.UUID, exc: Exception, outcomes: list[NotifyOutcome]
) -> Exception:
    """Re-shape ANY failure so it carries whatever this pass already committed.

    Not only Unavailable: an IntegrityError, a Decimal error or a dropped connection raised after
    a settlement committed used to escape uncarried, and since that attempt is already 'settled'
    no later tick revisits it, so its broadcast and duplicate-payment alert were lost for good.
    An unexpected error becomes UnresolvedEvidence (503, fail closed), logged with its traceback.
    """
    if not outcomes:
        return exc
    if not isinstance(exc, Unavailable):
        log.error("reconciliation failed after a committed settlement", exc_info=exc)
    try:
        await db.rollback()  # committed work stays committed; this clears the aborted statement
        merged = await merge_outcomes(db, tid, outcomes)
    except Exception:
        # The session itself is unusable (e.g. the connection died). Nothing more can be done
        # here; keep the original error rather than masking it with this one.
        log.error("could not carry committed settlements after %s", type(exc).__name__)
        return exc
    message = (
        exc.message
        if isinstance(exc, Unavailable)
        else "Payment evidence could not be fully resolved"
    )
    return UnresolvedEvidence(message, merged)


async def emit_outcome(res: Resources, db: AsyncSession, outcome: NotifyOutcome) -> list[Order]:
    """Broadcast a committed settlement and raise its staff alerts (duplicate trade included)."""
    from app.core import notify_tg
    from app.modules.orders.service import broadcast_order

    touched = outcome.orders or ([outcome.order] if outcome.order is not None else [])
    for order in touched:
        await broadcast_order(
            res,
            db,
            order,
            [m for m in outcome.messages if m and m.conversation_id == order.conversation_id],
        )
    notify_tg.outcome_events(res, touched, outcome.dup_trade_nos or outcome.dup_trade_no)
    return touched


async def _mark_settled(
    db: AsyncSession, tid: uuid.UUID, where: Any, trade_no: str | None = None
) -> None:
    """Mark one original attempt settled, from a FRESH row.

    A fresh select matters: apply_* may roll back, which expires every instance, and touching an
    expired attribute on an async session triggers implicit IO (MissingGreenlet)."""
    att = await db.scalar(select(PaymentAttempt).where(where, PaymentAttempt.tenant_id == tid))
    if att is None:
        return
    att.status = "settled"
    if att.provider_id is not None:
        from app.modules.payments.gateways import mark_gateway_ok

        await mark_gateway_ok(db, tid, att.provider_id)
    if trade_no:
        att.data = {**att.data, "confirmed_trade_no": trade_no[:256]}
    att.checked_at = datetime.now(timezone.utc)


async def requery_attempts(
    db: AsyncSession,
    res: Resources,
    tid: uuid.UUID,
    no: str,
    sink: list[NotifyOutcome] | None = None,
    *,
    only_failed: bool = False,
    include_closed: bool = False,
) -> tuple[NotifyOutcome | None, bool]:
    """Returns (settled outcome, has durable EPay history). Failure never implies unpaid.

    ``only_failed`` is the safety net for attempts closed as failed (gateway unreachable, or
    released by staff): it re-queries just those, in case money arrived anyway.

    ``sink``, when given, receives each outcome the moment it commits, so a caller that cancels
    this coroutine (asyncio.wait_for) still holds what was already settled (#33)."""
    refs = list(
        await db.scalars(
            select(PaymentAttempt.gateway_ref)
            .where(
                PaymentAttempt.tenant_id == tid,
                PaymentAttempt.local_ref == no,
                PaymentAttempt.provider_id.is_not(None),
                # An already-settled original is decided: re-querying it on every retry only
                # replays the duplicate-payment audit trail and re-broadcasts the same order.
                # failed = never dispatched (gateway unreachable) or resolved not-paid by staff
                PaymentAttempt.status == "failed"
                if only_failed
                else PaymentAttempt.status.notin_(_skipped(include_closed)),
            )
            .order_by(PaymentAttempt.created_at)
            .limit(11)
        )
    )
    if len(refs) > 10:
        raise Unavailable("Too many attempts; staff reconciliation is required")
    outcomes: list[NotifyOutcome] = []
    failed = False
    # Any failure raised after a settlement has committed must still hand that outcome back,
    # otherwise its broadcast and duplicate-payment alert are lost for good: the attempt is
    # already 'settled', so no later tick revisits it.
    try:
        for ref in refs:
            routed, _ = await routed_resources(db, res, tid, ref)
            await db.rollback()
            try:
                q = await routed.zpay.query_order(ref)
            except ZPayQueryError:
                failed = True
                continue
            if q.found and q.paid:
                if q.out_trade_no != ref or not q.trade_no or q.money is None:
                    raise Unavailable("Payment query returned inconsistent evidence")
                trade = epay.gateway_trade_key(routed.zpay.config.instance_id, q.trade_no)
                model, col = (
                    (Checkout, Checkout.checkout_no)
                    if no.startswith("C")
                    else (Order, Order.order_no)
                )
                target_id = await db.scalar(
                    select(model.id).where(col == no, model.tenant_id == tid)
                )
                if target_id is None:
                    raise Unavailable("Payment target unavailable")
                raw = {
                    "out_trade_no": ref,
                    "trade_no": q.trade_no,
                    "money": str(q.money),
                    "provider_id": routed.zpay.config.instance_id,
                    "via": "reconciliation",
                }
                apply = apply_checkout_payment if no.startswith("C") else apply_order_payment
                where = PaymentAttempt.gateway_ref == ref
                # Mark first so the mark rides in apply's own commit. Marking only afterwards left
                # a window in which a crash or timeout kept the attempt 'ready', and the next tick
                # re-applied it, replaying the duplicate-payment audit row and staff alert (#33).
                await _mark_settled(db, tid, where, q.trade_no)
                result = await apply(db, res.settings, target_id, trade, q.money, raw, None)
                outcomes.append(result)
                if sink is not None:
                    sink.append(result)
                # apply rolls back -- and still returns -- on a no-op replay, discarding the early
                # mark; re-mark from a fresh row so that path behaves exactly as it always did.
                await _mark_settled(db, tid, where, q.trade_no)
                await db.commit()
    except Exception as exc:
        carried = await _carrying(db, tid, exc, outcomes)
        if carried is exc:
            raise
        raise carried from exc
    if failed:
        raise UnresolvedEvidence(
            "Some original payment queries are unavailable; cancellation is blocked",
            await merge_outcomes(db, tid, outcomes),
        )
    return await merge_outcomes(db, tid, outcomes), bool(refs)


# The statuses the reconciler polls; ix_attempts_open (migration 0019) matches this predicate.
OPEN_STATUSES = ("ready", "preparing", "unknown", "superseded")


def _skipped(include_closed: bool) -> tuple[str, ...]:
    """Attempt states a re-query skips: decided ones, plus ``closed`` for the background poll."""
    return ("settled", "failed") if include_closed else ("settled", "failed", "closed")


ATTEMPT_CLOSE_AFTER = timedelta(hours=72)
CLOSE_PAYABLE_STATES = ("expired", "cancelled", "refunded")

RECHECK_WINDOW = timedelta(hours=24)
RECHECK_EVERY = timedelta(minutes=10)
RECHECK_REASONS = ("gateway_unreachable", "gateway_refused", "resolved_not_paid")


async def recheck_failed(res: Resources, db: AsyncSession, tid: uuid.UUID, now: datetime) -> int:
    """Safety net: a failed attempt younger than 24 h is re-queried at most every 10 minutes.

    'Failed' is a conclusion drawn without gateway evidence (never dispatched / staff judgement).
    A positive re-query settles exactly like a notify: pending -> paid, cancelled/expired ->
    manual_review. Returns the number of settled payables."""
    rows = list(
        await db.execute(
            select(PaymentAttempt.local_ref, PaymentAttempt.id)
            .where(
                PaymentAttempt.tenant_id == tid,
                PaymentAttempt.status == "failed",
                PaymentAttempt.provider_id.is_not(None),
                PaymentAttempt.created_at > now - RECHECK_WINDOW,
                PaymentAttempt.data["reason"].as_string().in_(RECHECK_REASONS),
                or_(
                    PaymentAttempt.checked_at.is_(None),
                    PaymentAttempt.checked_at < now - RECHECK_EVERY,
                ),
            )
            .order_by(PaymentAttempt.checked_at.asc().nullsfirst())
            .limit(8)
        )
    )
    settled = 0
    for no in dict.fromkeys(r[0] for r in rows):
        outcome: NotifyOutcome | None = None
        try:
            outcome, _ = await asyncio.wait_for(
                requery_attempts(db, res, tid, no, only_failed=True), timeout=10
            )
        except Exception as exc:
            if not isinstance(exc, (Unavailable, asyncio.TimeoutError)):
                log.error("failed-attempt recheck failed for %s", no, exc_info=exc)
            await db.rollback()
            outcome = getattr(exc, "settled", None)
        if outcome is not None:
            try:
                await emit_outcome(res, db, outcome)
                settled += 1
            except Exception:
                log.warning("recheck settlement broadcast skipped for %s", no)
                await db.rollback()
        # stamp the (still failed) attempts so the next look is 10 minutes away
        for row in await db.scalars(
            select(PaymentAttempt).where(
                PaymentAttempt.tenant_id == tid,
                PaymentAttempt.local_ref == no,
                PaymentAttempt.status == "failed",
            )
        ):
            row.checked_at = datetime.now(timezone.utc)
        await db.commit()
    return settled


async def run(res: Resources) -> int:
    """At most 20 invoice groups per tenant/tick, backed off per durable attempt."""
    async with tenant_session(res.maker, None) as db:
        tids = list(await db.scalars(select(Tenant.id)))
    total = 0
    started = time.monotonic()
    if tids:
        offset = int(await res.redis.incr("sec:reconcile-tenant")) % len(tids)
        tids = tids[offset:] + tids[:offset]
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=2)
    slow_cutoff = now - timedelta(hours=1)
    for tid in tids:
        if time.monotonic() - started >= 20:
            break
        async with tenant_session(res.maker, tid) as db:
            await alert_stale_unknown(res, db, tid, now)
            total += await recheck_failed(res, db, tid, now)
            await close_abandoned(res, db, tid, now)
            await alert_dead_jobs(res, db, tid, now)
            await alert_procurement_jobs(res, db, tid, now)
            await alert_supplier_breakers(res, db, tid)
            await purge_overdue_sessions(res, db, tid)
            await alert_declining_offers(res, db, tid, now)
            refs = list(
                await db.scalars(
                    select(PaymentAttempt.local_ref)
                    .where(
                        PaymentAttempt.tenant_id == tid,
                        PaymentAttempt.status.in_(OPEN_STATUSES),
                        # back off: every 2 min for the first hour of an attempt's life, then hourly
                        or_(
                            PaymentAttempt.checked_at.is_(None),
                            and_(
                                PaymentAttempt.checked_at < cutoff,
                                PaymentAttempt.created_at >= slow_cutoff,
                            ),
                            and_(
                                PaymentAttempt.checked_at < slow_cutoff,
                                PaymentAttempt.created_at < slow_cutoff,
                            ),
                        ),
                        # an alerted-out unknown attempt is staff's now; stop polling forever
                        PaymentAttempt.data["stale_alerted"].as_string().is_(None),
                    )
                    .order_by(
                        PaymentAttempt.checked_at.asc().nullsfirst(), PaymentAttempt.created_at
                    )
                    .limit(8)
                )
            )
            for no in dict.fromkeys(refs):
                remaining = 20 - (time.monotonic() - started)
                if remaining <= 0:
                    break
                # Collects every outcome the moment it commits. wait_for CANCELS the coroutine on
                # timeout, so nothing it was about to return survives -- only what is already in
                # here. Without it a timeout lost the broadcast and duplicate-payment alert of a
                # settlement that had committed, permanently, since settled attempts are skipped.
                sink: list[NotifyOutcome] = []
                outcome: NotifyOutcome | None = None
                try:
                    outcome = await asyncio.wait_for(
                        requery_all(db, res, tid, no, sink), timeout=min(10, remaining)
                    )
                except Exception as exc:
                    # Any exception: a non-Unavailable one used to escape and abort the tick for
                    # every remaining tenant. Expected ones are routine; others get a traceback.
                    if not isinstance(exc, (Unavailable, asyncio.TimeoutError)):
                        log.error("reconciliation failed for %s", no, exc_info=exc)
                    await db.rollback()
                    if sink:
                        try:
                            outcome = await merge_outcomes(db, tid, sink)
                        except Exception:
                            log.warning("committed settlement could not be reloaded for %s", no)
                            await db.rollback()
                if outcome is not None:
                    # best effort: the bookkeeping below must still run for this ref
                    try:
                        await emit_outcome(res, db, outcome)
                        total += 1
                    except Exception:
                        log.warning("committed settlement broadcast skipped for %s", no)
                        await db.rollback()
                rows = await db.scalars(
                    select(PaymentAttempt).where(
                        PaymentAttempt.tenant_id == tid, PaymentAttempt.local_ref == no
                    )
                )
                for row in rows:
                    row.checked_at = datetime.now(timezone.utc)
                    row.attempts += 1
                await db.commit()
    return total


async def _definitively_unpaid(
    db: AsyncSession, res: Resources, tid: uuid.UUID, att: PaymentAttempt, no: str
) -> bool:
    """Fresh gateway query for one attempt; True only for an unambiguous unpaid / not found."""
    try:
        if att.provider_id is not None:
            ref = att.gateway_ref  # captured: the rollback below expires the row
            routed, _ = await routed_resources(db, res, tid, ref)
            await db.rollback()
            q = await routed.zpay.query_order(ref)
            return not (q.found and q.paid)  # a paid answer is left to the normal poll
        trade_id = (att.data or {}).get("trade_id")
        if not trade_id:
            return False  # nothing to ask: stays open for staff
        client = usdt.original_client(res, att)
        await db.rollback()
        info = await client.query_info(trade_id)
        if not info.found:
            return True
        return info.status in (bepusdt.STATUS_WAITING, bepusdt.STATUS_EXPIRED)
    except (ZPayQueryError, bepusdt.BepusdtError, Unavailable, asyncio.TimeoutError):
        return False


async def close_abandoned(res: Resources, db: AsyncSession, tid: uuid.UUID, now: datetime) -> int:
    """B3: give abandoned attempts a terminal state so they stop being polled for ever.

    An open attempt older than ATTEMPT_CLOSE_AFTER whose payable is expired / cancelled /
    refunded becomes ``closed`` iff a fresh query in this tick says unpaid / not found. A failed
    or ambiguous query leaves it open. A late notify still settles it (routing is by
    ``gateway_ref``, not status)."""
    candidates = list(
        await db.scalars(
            select(PaymentAttempt)
            .where(
                PaymentAttempt.tenant_id == tid,
                PaymentAttempt.status.in_(OPEN_STATUSES),
                PaymentAttempt.created_at < now - ATTEMPT_CLOSE_AFTER,
                PaymentAttempt.data["stale_alerted"].as_string().is_(None),
            )
            .order_by(PaymentAttempt.created_at)
            .limit(40)
        )
    )
    todo: list[tuple[uuid.UUID, str]] = []
    states: dict[str, str | None] = {}
    for att in candidates:
        if len(todo) >= 4:
            break
        no = att.local_ref
        if no not in states:
            model, col = (
                (Checkout, Checkout.checkout_no) if no.startswith("C") else (Order, Order.order_no)
            )
            states[no] = await db.scalar(
                select(model.status).where(col == no, model.tenant_id == tid)
            )
        if states[no] in CLOSE_PAYABLE_STATES:
            todo.append((att.id, no))
    closed = 0
    started = time.monotonic()
    for att_id, no in todo:
        if time.monotonic() - started > 10:
            break
        att = await db.scalar(
            select(PaymentAttempt).where(
                PaymentAttempt.id == att_id, PaymentAttempt.tenant_id == tid
            )
        )
        if att is None or att.status not in OPEN_STATUSES:
            continue
        try:
            unpaid = await asyncio.wait_for(_definitively_unpaid(db, res, tid, att, no), timeout=8)
        except asyncio.TimeoutError:
            unpaid = False
        if not unpaid:
            await db.rollback()
            continue
        att = await db.scalar(
            select(PaymentAttempt)
            .where(PaymentAttempt.id == att_id, PaymentAttempt.tenant_id == tid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if att is None or att.status not in OPEN_STATUSES:
            await db.rollback()
            continue
        before = att.status
        att.status = "closed"
        att.checked_at = now
        att.data = {**(att.data or {}), "closed_at": now.isoformat()}
        await audit(
            db,
            tenant_id=tid,
            action="payment.attempt_closed",
            target="payment_attempt:" + str(att_id),
            before={"status": before},
            after={"status": "closed"},
            detail={"local_ref": no, "payable_status": states.get(no)},
        )
        await db.commit()
        closed += 1
    return closed


DEAD_JOB_ATTEMPTS = 8  # app.delivery.worker.MAX_ATTEMPTS; the delivery process has no egress
DEAD_JOB_WINDOW = timedelta(days=7)


async def alert_dead_jobs(res: Resources, db: AsyncSession, tid: uuid.UUID, now: datetime) -> int:
    """A4: a delivery job that died (8 failures, ``delivery.job_dead``) leaves its order ``paid``.
    The delivery process cannot reach Telegram, so the API process finds the dead outbox rows and
    raises the same staff alert the duplicate-payment path does (once per job, Redis-deduped)."""
    from app.core import notify_tg
    from app.models import Outbox

    rows = list(
        (
            await db.execute(
                select(Outbox.id, Outbox.topic, Outbox.payload)
                .where(
                    Outbox.tenant_id == tid,
                    Outbox.attempts >= DEAD_JOB_ATTEMPTS,
                    Outbox.last_error.is_not(None),
                    Outbox.processed_at.is_not(None),
                    Outbox.processed_at > now - DEAD_JOB_WINDOW,
                )
                .order_by(Outbox.id.desc())
                .limit(10)
            )
        ).all()
    )
    await db.rollback()
    for oid, topic, payload in rows:
        notify_tg.notify_bg(
            res,
            tid,
            "payment_review",
            [
                notify_tg.T("job.dead", topic=topic, id=oid, n=DEAD_JOB_ATTEMPTS),
                notify_tg.T(
                    "job.maybe_undelivered", order=str((payload or {}).get("order_id", ""))[:36]
                ),
            ],
            link=notify_tg.admin_link(res, "orders"),
            once=(f"dead_job:{oid}", 30 * 86400),
        )
    return len(rows)


async def alert_procurement_jobs(
    res: Resources, db: AsyncSession, tid: uuid.UUID, now: datetime
) -> int:
    """review-v12 §3/§6: a procurement job that needs staff (FATAL, no source left, unresolved)
    or is stuck past 30 min raises the staff alert once (procurement has no Telegram token)."""
    from app.core import notify_tg
    from app.models import ProcurementJob

    rows = list(
        (
            await db.execute(
                select(ProcurementJob.id, ProcurementJob.order_id, ProcurementJob.state,
                       ProcurementJob.error_code, ProcurementJob.kind)
                .where(
                    ProcurementJob.tenant_id == tid,
                    or_(
                        ProcurementJob.state == "needs_manual",
                        # a direct order refunded after the subscription completed (staff decide)
                        ProcurementJob.error_code == "refunded_after_success",
                        ProcurementJob.alerted_at.is_not(None)
                        & ProcurementJob.state.in_(("queued", "submitting", "accepted_pending")),
                    ),
                    ProcurementJob.updated_at > now - DEAD_JOB_WINDOW,
                )
                .order_by(ProcurementJob.updated_at.desc())
                .limit(10)
            )
        ).all()
    )  # fmt: skip
    await db.rollback()
    for jid, oid, state, code, kind in rows:
        if kind == "direct" and code in ("void_failed", "refunded_after_success"):
            # the subscription is on the buyer's account and cannot be undone through the API:
            # a staff decision (recover from the supplier, or accept the loss), once per job
            notify_tg.notify_bg(
                res,
                tid,
                "payment_review",
                [notify_tg.T("proc.direct_done", job=str(jid)[:8], order=str(oid)[:36])],
                link=notify_tg.admin_link(res, "orders"),
                once=(f"procurement_job:{jid}:direct_done", 30 * 86400),
            )
            continue
        if code == "void_failed":
            # a refunded / closed order whose supplier card or code may still be live: its own
            # alert (not "waits for a code"), once per job
            notify_tg.notify_bg(
                res,
                tid,
                "payment_review",
                [notify_tg.T("proc.void_failed", job=str(jid)[:8], order=str(oid)[:36])],
                link=notify_tg.admin_link(res, "orders"),
                once=(f"procurement_job:{jid}:void_failed", 30 * 86400),
            )
            continue
        notify_tg.notify_bg(
            res,
            tid,
            "payment_review",
            [
                notify_tg.T("proc.job_stuck", job=str(jid)[:8], state=state, code=code or "stuck"),
                notify_tg.T("proc.order_waits", order=str(oid)[:36]),
            ],
            link=notify_tg.admin_link(res, "orders"),
            once=(f"procurement_job:{jid}:{state}", 30 * 86400),
        )
    return len(rows)


async def alert_declining_offers(
    res: Resources, db: AsyncSession, tid: uuid.UUID, now: datetime
) -> int:
    """A direct source was switched off after repeated declines: staff re-enable it by hand."""
    from app.core import notify_tg
    from app.models import AuditLog

    rows = list(
        (
            await db.execute(
                select(AuditLog.id, AuditLog.target, AuditLog.action)
                .where(
                    AuditLog.tenant_id == tid,
                    AuditLog.action.in_(
                        ("procurement.offer_declining", "procurement.cost_over_bound")
                    ),
                    AuditLog.created_at > now - timedelta(hours=24),
                )
                .order_by(AuditLog.id.desc())
                .limit(5)
            )
        ).all()
    )
    await db.rollback()
    for aid, target, action in rows:
        key = "proc.cost_over" if action == "procurement.cost_over_bound" else "proc.offer_off"
        notify_tg.notify_bg(
            res,
            tid,
            "payment_review",
            [notify_tg.T(key, offer=str(target)[-8:])],
            link=notify_tg.admin_link(res, "orders"),
            once=(f"procurement:offer_off:{aid}", 7 * 86400),
        )
    return len(rows)


SESSIONS_OVERDUE_KEY = "proc:sessions-overdue"


async def purge_overdue_sessions(res: Resources, db: AsyncSession, tid: uuid.UUID) -> int:
    """Database backstop for the deletion of sealed buyer sessions (upstream_direct): procurement
    deletes them at a terminal state / after its maximum age, and the API role may delete the ones
    older than 6 h (migration 0030). Finding any means procurement is not doing its job: checkout of
    direct products is paused for an hour (503 ``direct_unavailable``) and staff are alerted."""
    from sqlalchemy import text as sql_text

    from app.core import notify_tg

    out = await db.execute(
        sql_text(
            "DELETE FROM procurement_credentials WHERE tenant_id = :t "
            "AND created_at < now() - interval '6 hours'"
        ),
        {"t": tid},
    )
    n = int(out.rowcount or 0)  # type: ignore[attr-defined]
    if n:
        from app.core.audit import audit

        await audit(
            db,
            tenant_id=tid,
            action="procurement.sessions_overdue",
            target="procurement_credentials",
            detail={"deleted": n},
            severity="critical",
        )
        await res.redis.set(SESSIONS_OVERDUE_KEY, "1", ex=3600)
        notify_tg.notify_bg(
            res,
            tid,
            "payment_review",
            [notify_tg.T("proc.sessions_overdue", n=str(n))],
            link=notify_tg.admin_link(res, "orders"),
            once=(f"procurement:sessions_overdue:{tid}", 3600),
        )
    await db.commit()
    return n


async def alert_supplier_breakers(res: Resources, db: AsyncSession, tid: uuid.UUID) -> int:
    """review-v12 §6: an open supplier breaker (a refusal, or a balance below the threshold)
    raises one staff alert per opening (procurement has no Telegram token)."""
    from app.core import notify_tg
    from app.models import SupplierAccount

    rows = list(
        (
            await db.execute(
                select(SupplierAccount.id, SupplierAccount.code, SupplierAccount.env,
                       SupplierAccount.breaker_reason, SupplierAccount.breaker_kind,
                       SupplierAccount.breaker_at)
                .where(
                    SupplierAccount.tenant_id == tid,
                    SupplierAccount.breaker_open.is_(True),
                    SupplierAccount.breaker_at.is_not(None),
                )
                .limit(10)
            )
        ).all()
    )  # fmt: skip
    await db.rollback()
    for sid, code, env, reason, kind, at in rows:
        notify_tg.notify_bg(
            res,
            tid,
            "payment_review",
            [
                notify_tg.T("proc.supplier_paused", code=code, env=env),
                notify_tg.T("proc.pause_reason", reason=reason or "unknown", kind=kind or "hard"),
            ],
            link=notify_tg.admin_link(res, "orders"),
            once=(f"supplier_breaker:{sid}:{int(at.timestamp())}", 30 * 86400),
        )
    return len(rows)


STALE_UNKNOWN = timedelta(hours=24)


async def alert_stale_unknown(
    res: Resources, db: AsyncSession, tid: uuid.UUID, now: datetime
) -> int:
    """One high-severity operation event + staff notification per attempt still ``unknown`` after
    24 h (instead of re-querying it forever). Flagged in ``data`` so it fires once."""
    from app.core import notify_tg
    from app.modules.operations.service import record_event

    rows = list(
        await db.scalars(
            select(PaymentAttempt)
            .where(
                PaymentAttempt.tenant_id == tid,
                PaymentAttempt.status == "unknown",
                PaymentAttempt.created_at < now - STALE_UNKNOWN,
                PaymentAttempt.data["stale_alerted"].as_string().is_(None),
            )
            .order_by(PaymentAttempt.created_at)
            .limit(20)
            .with_for_update(skip_locked=True)
        )
    )
    alerts: list[str] = []
    for att in rows:
        att.data = {**(att.data or {}), "stale_alerted": now.isoformat()}
        await record_event(
            db,
            tenant_id=tid,
            environment=res.settings.env,
            family="payment",
            action="payment.attempt_stale_unknown",
            outcome="unknown",
            reason="unresolved_24h",
            resource_id=str(att.id),
            severity="critical",
        )
        alerts.append(att.local_ref)
    if rows:
        await db.commit()
        for no in alerts:
            notify_tg.notify_bg(
                res,
                tid,
                "payment_review",
                [
                    notify_tg.T("pay.stale", no=no),
                    notify_tg.T("pay.check_gateway"),
                ],
                link=notify_tg.admin_link(res, "orders"),
                once=(f"stale_unknown:{no}", 7 * 86400),
            )
    return len(rows)


async def merge_outcomes(
    db: AsyncSession, tid: uuid.UUID, outcomes: list[NotifyOutcome]
) -> NotifyOutcome | None:
    if not outcomes:
        return None
    order_ids, message_ids = set(), set()
    # Every distinct duplicate is a separate refund owed: keeping only the last one silently
    # dropped the staff alert for the others when three or more originals were paid.
    duplicates: dict[str, None] = {}
    for result in outcomes:
        for obj in result.orders + ([result.order] if result.order is not None else []):
            order_ids.add(inspect(obj).identity[0])
        for obj in result.messages:
            if obj is not None:
                message_ids.add(inspect(obj).identity[0])
        for dup in (*result.dup_trade_nos, result.dup_trade_no):
            if dup:
                duplicates[dup] = None
    orders = list(
        await db.scalars(
            select(Order)
            .where(Order.tenant_id == tid, Order.id.in_(order_ids))
            .execution_options(populate_existing=True)
        )
    )
    messages = (
        list(
            await db.scalars(
                select(Message)
                .where(Message.tenant_id == tid, Message.id.in_(message_ids))
                .execution_options(populate_existing=True)
            )
        )
        if message_ids
        else []
    )
    dups = tuple(duplicates)
    return NotifyOutcome(
        "success",
        orders[0] if orders else None,
        messages,
        orders,
        dups[0] if dups else None,
        dups,
    )


async def requery_usdt_attempts(
    db: AsyncSession,
    res: Resources,
    tid: uuid.UUID,
    no: str,
    sink: list[NotifyOutcome] | None = None,
    *,
    include_closed: bool = False,
) -> tuple[NotifyOutcome | None, bool]:
    rows = list(
        await db.scalars(
            select(PaymentAttempt)
            .where(
                PaymentAttempt.tenant_id == tid,
                PaymentAttempt.local_ref == no,
                PaymentAttempt.provider_id.is_(None),
                # failed = refused / never created / resolved not-paid by staff; closed = abandoned
                PaymentAttempt.status.notin_(_skipped(include_closed)),
            )
            .order_by(PaymentAttempt.checked_at.asc().nullsfirst(), PaymentAttempt.created_at)
            .limit(11)
        )
    )
    if len(rows) > 10:
        raise Unavailable("Too many USDT attempts; manual reconciliation is required")
    # Capture immutable values before any network/settlement rollback expires ORM rows.
    snapshots = [
        (row.id, dict(row.data), row.channel, usdt.original_client(res, row)) for row in rows
    ]
    outcomes: list[NotifyOutcome] = []
    unresolved = False
    try:
        for ident, data, channel, client in snapshots:
            if not data.get("trade_id"):
                unresolved = True
                continue
            await db.rollback()
            try:
                info = await client.query_info(data["trade_id"])
            except bepusdt.BepusdtError:
                unresolved = True
                continue
            if not info.found:
                # A known issued trade disappearing is not evidence that no transfer occurred.
                unresolved = True
                continue
            if (
                info.trade_id != data["trade_id"]
                or info.order_id != no
                or info.fiat != bepusdt.FIAT
                or info.money is None
                or bepusdt.TRADE_TYPE_TO_CHANNEL.get(info.trade_type) != channel
            ):
                unresolved = True
                continue
            if info.status == bepusdt.STATUS_PAID:
                model, col = (
                    (Checkout, Checkout.checkout_no)
                    if no.startswith("C")
                    else (Order, Order.order_no)
                )
                target = await db.scalar(select(model).where(col == no, model.tenant_id == tid))
                target_id = target.id if target else None
                if target_id is None:
                    raise Unavailable("Payment target unavailable")
                hold = "channel_mismatch" if target.channel not in (None, channel) else None
                if info.address != data.get("address") or info.actual_amount != bepusdt.decimal_of(
                    data.get("actual_amount")
                ):
                    hold = "receiver_mismatch"
                raw = {
                    "order_id": no,
                    "trade_id": info.trade_id,
                    "amount": str(info.money),
                    "token": info.address,
                    "via": "original_attempt_requery",
                }
                apply = apply_checkout_payment if no.startswith("C") else apply_order_payment
                where = PaymentAttempt.id == ident
                await _mark_settled(db, tid, where)  # rides in apply's commit; see EPay checker
                result = await apply(
                    db,
                    res.settings,
                    target_id,
                    info.trade_id,
                    info.money,
                    raw,
                    None,
                    channel,
                    hold=hold,
                )
                outcomes.append(result)
                if sink is not None:
                    sink.append(result)
                await _mark_settled(db, tid, where)
                await db.commit()
            elif info.status not in (bepusdt.STATUS_WAITING, bepusdt.STATUS_EXPIRED):
                unresolved = True
    except Exception as exc:
        carried = await _carrying(db, tid, exc, outcomes)
        if carried is exc:
            raise
        raise carried from exc
    if unresolved:
        raise UnresolvedEvidence(
            "Original USDT evidence is unresolved; cancellation is blocked",
            await merge_outcomes(db, tid, outcomes),
        )
    return await merge_outcomes(db, tid, outcomes), bool(rows)


async def requery_all(
    db: AsyncSession,
    res: Resources,
    tid: uuid.UUID,
    no: str,
    sink: list[NotifyOutcome] | None = None,
    *,
    include_closed: bool = False,
) -> NotifyOutcome | None:
    """``include_closed``: the synchronous safety checks (buyer/staff cancel, release-hold close)
    must ask about every original ever issued, including attempts the background poll closed
    (#72 audit M1); the background poll itself keeps skipping them."""
    results = []
    failed = False
    for checker in (requery_attempts, requery_usdt_attempts):
        try:
            result, _ = await checker(db, res, tid, no, sink, include_closed=include_closed)
            if result is not None:
                results.append(result)
        except Exception as exc:
            # Any failure, not only Unavailable: an unexpected error in the EPay checker used to
            # skip the USDT checker entirely and drop what the EPay one had committed.
            if not isinstance(exc, Unavailable):
                log.error("payment re-query failed for %s", no, exc_info=exc)
            # A settlement one checker already committed survives another's unresolved evidence.
            settled = getattr(exc, "settled", None)
            if settled is not None:
                results.append(settled)
            await db.rollback()
            failed = True
    if failed:
        raise UnresolvedEvidence(
            "Some original payment evidence is unresolved", await merge_outcomes(db, tid, results)
        )
    return await merge_outcomes(db, tid, results)
