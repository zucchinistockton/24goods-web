"""SMS verification codes (ADR-057, spec docs/research/sms-upstream-2026-11-27.md).

* ``on_requested``: ``procure.requested`` for an ``upstream_sms`` order creates its session
  (``ready``); nothing is bought until the buyer asks.
* ``next_number``: the buyer's "Get number" / "Another number" (internal API). Holds the session
  row lock while it talks to HeroSMS (cancel the current number, buy the next one).
* ``run_sms_loop``: polls open numbers (every 3 s while one waits), seals the first code into
  ``card_stock`` and emits ``procure.fulfilled``; cancels numbers without a code after
  ``number_wait_s``; fails sessions whose attempts / window / start time ran out
  (``procure.failed`` -> delivery holds the order for a manual refund); recomputes availability.

Plaintext codes exist only between the provider answer and ``seal()``: never logged, audited,
stored in a row or put in an exception. Phone numbers never go to audit rows or logs.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.cardvault import CardContext
from app.core.db import tenant_session
from app.models import CardStock, Order, Outbox, SmsAttempt, SmsProduct, SmsSession, SupplierAccount
from app.procurement.context import ProcurementContext
from app.procurement.service import SupplierRef, make_supplier
from app.suppliers.base import Outcome, clean_detail
from app.suppliers.herosms import Activation, HeroSmsSupplier, SmsError
from app.suppliers.herosms import params as P
from app.suppliers.registry import SupplierConfigError

log = logging.getLogger("app.procurement.sms")

MODE = "upstream_sms"
TOPIC_FULFILLED = "procure.fulfilled"
TOPIC_FAILED = "procure.failed"
FAST_POLL_S = 3.0
ROUTE_BUDGET_S = 50.0  # stay under the API's 75 s wait for /internal/sms/next
LOST_AFTER_S = 1800  # a number unreadable this long after its wait is given up
IDLE_POLL_S = 20.0
REFRESH_S = 60.0
STOCK_MAX = 99
COOL = "rl:proc:sms:cool:{product}:{country}"
STREAK = "rl:proc:sms:streak:{product}:{country}"
STREAK_TTL_S = 6 * 3600


class SmsRefused(Exception):
    """A buyer request that cannot be served now; ``code`` goes to the buyer (i18n key)."""

    def __init__(self, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.code = code
        self.extra = extra


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _emit(db: AsyncSession, tenant_id: uuid.UUID, topic: str, key: str, payload: dict) -> None:
    db.add(Outbox(tenant_id=tenant_id, topic=topic, key=key[:128], payload=payload))


# --------------------------------------------------------------------------- supplier
async def _supplier_ref(db: AsyncSession, supplier_id: uuid.UUID | None) -> SupplierRef | None:
    if supplier_id is None:
        return None
    row = await db.scalar(select(SupplierAccount).where(SupplierAccount.id == supplier_id))
    if row is None or row.adapter != "herosms":
        return None
    return SupplierRef(
        row.id, row.tenant_id, row.code, row.adapter, row.env, row.health, row.enabled
    )


def _client(ctx: ProcurementContext, ref: SupplierRef) -> HeroSmsSupplier | None:
    try:
        sup = make_supplier(ctx, ref)
    except SupplierConfigError:
        return None
    return sup if isinstance(sup, HeroSmsSupplier) else None


# --------------------------------------------------------------------------- session creation
async def on_requested(ctx: ProcurementContext, db: AsyncSession, order: Any) -> None:
    """Create the order's session (idempotent). Called inside the outbox transaction."""
    exists = await db.scalar(select(SmsSession.id).where(SmsSession.order_id == order.id))
    if exists is not None:
        return
    await _create_session(db, order)


async def _create_session(db: AsyncSession, order: Any) -> SmsSession:
    cfg = await db.scalar(
        select(SmsProduct).where(
            SmsProduct.tenant_id == order.tenant_id, SmsProduct.product_id == order.product_id
        )
    )
    params = P.normalize(cfg.params if cfg else {})
    now = utcnow()
    paid_at = getattr(order, "paid_at", None) or now
    sess = SmsSession(
        id=uuid.uuid4(),
        tenant_id=order.tenant_id,
        order_id=order.id,
        product_id=order.product_id,
        supplier_id=cfg.supplier_id if cfg else None,
        state="ready",
        attempts_used=0,
        params=params,
        start_deadline=paid_at + timedelta(hours=params["start_hours"]),
    )
    db.add(sess)
    await db.flush()
    await audit(
        db,
        tenant_id=order.tenant_id,
        action="sms.session_created",
        target=f"order:{order.id}",
        detail={"session_id": str(sess.id), "configured": cfg is not None},
    )
    if cfg is None or cfg.supplier_id is None or order.qty != 1:
        await _fail(db, sess, "not_configured" if order.qty == 1 else "qty_not_one", now)
    return sess


async def _fail(
    db: AsyncSession, sess: SmsSession, reason: str, now: datetime, *, emit: bool = True
) -> None:
    before = sess.state
    sess.state = "failed"
    sess.fail_reason = reason[:32]
    sess.finished_at = now
    sess.version += 1
    await audit(
        db,
        tenant_id=sess.tenant_id,
        action="sms.session_failed",
        target=f"order:{sess.order_id}",
        before={"state": before},
        after={"state": "failed"},
        detail={"reason": reason, "attempts": sess.attempts_used},
        severity="warning",
    )
    if not emit:  # the order was refunded / cancelled meanwhile: nothing to hold
        return
    _emit(
        db,
        sess.tenant_id,
        TOPIC_FAILED,
        f"failed:{sess.order_id}:sms",
        {"order_id": str(sess.order_id), "reason": f"sms_{reason}"[:64]},
    )


# --------------------------------------------------------------------------- views
def view(sess: SmsSession | None, current: SmsAttempt | None, now: datetime | None = None) -> dict:
    """What the buyer may see (the API builds the same shape from its own read)."""
    now = now or utcnow()
    if sess is None:
        return {"state": "pending"}
    params = P.normalize(sess.params)
    cur = None
    swap_in = 0
    if current is not None and current.state == "waiting":
        cur = {
            "phone": current.phone,
            "bought_at": current.bought_at.isoformat(),
            "expires_at": current.expires_at.isoformat() if current.expires_at else None,
            "state": current.state,
        }
        swap_in = max(0, int(params["min_swap_s"] - (now - current.bought_at).total_seconds()))
    return {
        "state": sess.state,
        "attempts_used": sess.attempts_used,
        "max_attempts": params["max_attempts"],
        "can_swap_in_s": swap_in,
        "window_ends_at": sess.window_ends_at.isoformat() if sess.window_ends_at else None,
        "start_deadline": sess.start_deadline.isoformat(),
        "current": cur,
    }


# --------------------------------------------------------------------------- routes
async def _cooled(ctx: ProcurementContext, product_id: uuid.UUID, countries: list[int]) -> set[int]:
    if not countries:
        return set()
    keys = [COOL.format(product=product_id, country=c) for c in countries]
    try:
        vals = await ctx.redis.mget(keys)
    except Exception:  # Redis down: no cooling information, routes stay usable
        return set()
    return {c for c, v in zip(countries, vals, strict=True) if v}


async def _cool(ctx: ProcurementContext, product_id: uuid.UUID, country: int, minutes: int) -> None:
    try:
        await ctx.redis.set(COOL.format(product=product_id, country=country), "1", ex=minutes * 60)
    except Exception:
        log.warning("sms cool flag not stored (redis)")


async def _streak(
    ctx: ProcurementContext, product_id: uuid.UUID, country: int, params: dict, *, code: bool
) -> bool:
    """Count consecutive no-code numbers per route (all orders). True = the route cooled now."""
    key = STREAK.format(product=product_id, country=country)
    try:
        if code:
            await ctx.redis.delete(key)
            return False
        n = int(await ctx.redis.incr(key))
        await ctx.redis.expire(key, STREAK_TTL_S)
        if n >= params["cool_no_code_streak"]:
            await ctx.redis.delete(key)
            await _cool(ctx, product_id, country, params["cool_no_code_min"])
            return True
    except Exception:
        log.warning("sms streak not counted (redis)")
    return False


def _order_routes(params: dict, tried: dict[int, int], cooled: set[int]) -> list[dict]:
    """Admin order, skipping cooled routes; routes this session already failed
    ``tries_per_route`` times go last."""
    live = [r for r in params["routes"] if r["country"] not in cooled]
    fresh = [r for r in live if tried.get(r["country"], 0) < params["tries_per_route"]]
    used = [r for r in live if r not in fresh]
    return fresh + used


# --------------------------------------------------------------------------- buyer action
async def next_number(ctx: ProcurementContext, tenant_id: uuid.UUID, order_id: uuid.UUID) -> dict:
    """Start the session or swap its number. Raises :class:`SmsRefused`."""
    changed_product: uuid.UUID | None = None
    async with tenant_session(ctx.maker, tenant_id) as db:
        order = (
            await db.execute(
                select(
                    Order.id,
                    Order.tenant_id,
                    Order.status,
                    Order.delivery_mode,
                    Order.qty,
                    Order.product_id,
                    Order.paid_at,
                ).where(Order.id == order_id, Order.tenant_id == tenant_id)
            )
        ).one_or_none()
        if order is None or order.delivery_mode != MODE:
            raise SmsRefused("sms_not_found")
        if order.status != "paid":
            raise SmsRefused("sms_not_ready")
        sess = await db.scalar(
            select(SmsSession)
            .where(SmsSession.tenant_id == tenant_id, SmsSession.order_id == order_id)
            .with_for_update()
        )
        if sess is None:  # the buyer was faster than the outbox: create it now (same rules)
            try:
                async with db.begin_nested():
                    await _create_session(db, order)
            except IntegrityError:
                pass
            await db.commit()
            sess = await db.scalar(
                select(SmsSession)
                .where(SmsSession.tenant_id == tenant_id, SmsSession.order_id == order_id)
                .with_for_update()
            )
            if sess is None:
                raise SmsRefused("sms_not_ready")
        now = utcnow()
        params = P.normalize(sess.params)
        if sess.state == "delivered":
            raise SmsRefused("sms_delivered")
        if sess.state == "failed":
            raise SmsRefused("sms_failed")
        if sess.state == "ready" and now > sess.start_deadline:
            await _fail(db, sess, "not_started", now)
            await db.commit()
            raise SmsRefused("sms_failed")
        if sess.window_ends_at is not None and now >= sess.window_ends_at:
            raise SmsRefused("sms_window_over")
        if sess.attempts_used >= params["max_attempts"]:
            raise SmsRefused("sms_attempts_exhausted")
        ref = await _supplier_ref(db, sess.supplier_id)
        sup = _client(ctx, ref) if ref is not None and ref.enabled else None
        if sup is None:
            raise SmsRefused("sms_unavailable")
        try:
            current = await db.scalar(
                select(SmsAttempt)
                .where(
                    SmsAttempt.tenant_id == tenant_id,
                    SmsAttempt.session_id == sess.id,
                    SmsAttempt.state == "waiting",
                )
                .order_by(SmsAttempt.seq.desc())
                .limit(1)
                .with_for_update()
            )
            if current is not None:
                wait = params["min_swap_s"] - (now - current.bought_at).total_seconds()
                if wait > 0:
                    raise SmsRefused("sms_too_early", seconds=int(wait) + 1)
                # a code may have just arrived: deliver it instead of throwing the number away
                try:
                    codes = await sup.client.codes(current.upstream_id)
                except SmsError:
                    # unknown: never throw away a number that may hold the buyer's code
                    raise SmsRefused("sms_busy") from None
                if codes:
                    ok = await _deliver(ctx, db, sess, current, codes[0], now)
                    del codes
                    if not ok:
                        raise SmsRefused("sms_busy")
                    await db.commit()
                    await _finish_upstream(ctx, sup, tenant_id, current.id)
                    raise SmsRefused("sms_delivered")
                try:
                    await sup.client.cancel(current.upstream_id)
                except SmsError as exc:
                    if exc.error_code == "EARLY_CANCEL_DENIED":
                        raise SmsRefused("sms_too_early", seconds=30) from None
                    if exc.outcome in (Outcome.RETRYABLE, Outcome.AMBIGUOUS):
                        raise SmsRefused("sms_busy") from None
                    # any other refusal: the number is gone upstream (expired / finished)
                await _close_attempt(ctx, db, sess, current, "no_code", now, params)
                await db.flush()
            tried = dict(
                (
                    await db.execute(
                        select(SmsAttempt.country, func.count())
                        .where(
                            SmsAttempt.tenant_id == tenant_id,
                            SmsAttempt.session_id == sess.id,
                            SmsAttempt.state.in_(("no_code", "cancelled")),
                        )
                        .group_by(SmsAttempt.country)
                    )
                ).all()
            )
            cooled = await _cooled(ctx, sess.product_id, [r["country"] for r in params["routes"]])
            routes = _order_routes(params, tried, cooled)
            bought: Activation | None = None
            route: dict | None = None
            last_error = "no_route"
            ambiguous = False
            started = time.monotonic()
            for r in routes:
                if time.monotonic() - started > ROUTE_BUDGET_S:
                    break
                try:
                    bought = await sup.client.buy(
                        service=params["service"],
                        country=r["country"],
                        max_price=Decimal(r["max_price"]),
                    )
                    route = r
                    break
                except SmsError as exc:
                    last_error = exc.reason
                    await audit(
                        db,
                        tenant_id=tenant_id,
                        action="sms.buy_refused",
                        target=f"order:{order_id}",
                        detail={"country": r["country"], "upstream": clean_detail(exc.reason)},
                        severity="warning",
                    )
                    if exc.outcome is Outcome.AMBIGUOUS:
                        # a number may exist upstream; it is never adopted (the account is shared
                        # with the owner's console) and unused numbers are refunded by HeroSMS.
                        # It still counts as an attempt, so repeats stay bounded by max_attempts.
                        ambiguous = True
                        break
                    if exc.error_code == "NO_NUMBERS":
                        await _cool(
                            ctx, sess.product_id, r["country"], params["cool_no_numbers_min"]
                        )
                        changed_product = sess.product_id
                        continue
                    if exc.error_code in ("NO_BALANCE", "BAD_KEY") or exc.outcome is Outcome.FATAL:
                        changed_product = sess.product_id
                        break
                    continue  # another refusal for this route: the next route may work
            if bought is None or route is None:
                if ambiguous:
                    sess.attempts_used += 1
                    sess.version += 1
                await db.commit()  # keep the closed old number and the audit rows
                raise SmsRefused("sms_unavailable", reason=clean_detail(last_error))
            seq = sess.attempts_used + 1
            att = SmsAttempt(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                session_id=sess.id,
                order_id=order_id,
                seq=seq,
                supplier_id=ref.id,
                upstream_id=bought.upstream_id,
                country=bought.country if bought.country is not None else route["country"],
                operator=bought.operator,
                phone=bought.phone,
                cost=bought.price if bought.price is not None else Decimal(route["max_price"]),
                currency="USD",
                state="waiting",
                bought_at=now,
                expires_at=bought.expires_at,
            )
            db.add(att)
            before = sess.state
            sess.attempts_used = seq
            if sess.state == "ready":
                sess.state = "active"
                sess.started_at = now
                sess.window_ends_at = now + timedelta(minutes=params["window_minutes"])
            sess.version += 1
            await audit(
                db,
                tenant_id=tenant_id,
                action="sms.number_bought",
                target=f"order:{order_id}",
                before={"state": before},
                after={"state": sess.state},
                detail={
                    "seq": seq,
                    "country": att.country,
                    "operator": att.operator,
                    "cost": str(att.cost),
                },
            )
            await db.commit()
            changed_product = sess.product_id
            out = view(sess, att, now)
        finally:
            await sup.aclose()
    if changed_product is not None:
        asyncio.get_running_loop().create_task(_refresh_one_safe(ctx, tenant_id, changed_product))
    return out


async def _close_attempt(
    ctx: ProcurementContext,
    db: AsyncSession,
    sess: SmsSession,
    att: SmsAttempt,
    state: str,
    now: datetime,
    params: dict,
) -> None:
    att.state = state
    att.closed_at = now
    att.upstream_closed = True
    await audit(
        db,
        tenant_id=att.tenant_id,
        action="sms.number_closed",
        target=f"order:{att.order_id}",
        detail={"seq": att.seq, "country": att.country, "state": state},
    )
    if state == "no_code" and await _streak(ctx, sess.product_id, att.country, params, code=False):
        await audit(
            db,
            tenant_id=att.tenant_id,
            action="sms.route_cooled",
            target=f"product:{sess.product_id}",
            detail={"country": att.country, "reason": "no_code_streak"},
            severity="warning",
        )


async def _deliver(
    ctx: ProcurementContext,
    db: AsyncSession,
    sess: SmsSession,
    att: SmsAttempt,
    code: str,
    now: datetime,
) -> bool:
    """Seal the code (+ the number) for the order and hand it to delivery. Caller commits."""
    if ctx.sealer is None:
        log.error("sms code arrived but procurement cannot seal (no vault public key)")
        return False
    sealer = ctx.sealer
    card_id = uuid.uuid4()
    tid = str(sess.tenant_id)
    text = f"{code}----+{att.phone}"
    values = {
        "id": card_id,
        "tenant_id": sess.tenant_id,
        "product_id": sess.product_id,
        "variant_id": None,
        "blob": sealer.seal(text, CardContext(tid, str(sess.product_id), str(card_id))),
        # not derived from the code (a keyed hash over a 6-digit code is brute-forceable)
        "fingerprint": sealer.card_fingerprint(tid, f"sms:{att.supplier_id}:{att.upstream_id}"),
        "lookup_fp": None,
        "kid": sealer.active_kid,
        "status": "sold",
        "order_id": sess.order_id,
    }
    del text, code
    try:
        async with db.begin_nested():
            await db.execute(insert(CardStock.__table__).values(**values))
    except IntegrityError:
        log.error("sms code already sealed for attempt %s", att.id)
        return False
    att.state = "code"
    att.code_at = now
    att.closed_at = now
    sess.state = "delivered"
    sess.finished_at = now
    sess.version += 1
    await audit(
        db,
        tenant_id=sess.tenant_id,
        action="sms.code_received",
        target=f"order:{sess.order_id}",
        before={"state": "active"},
        after={"state": "delivered"},
        detail={
            "seq": att.seq,
            "country": att.country,
            "seconds": int((now - att.bought_at).total_seconds()),
        },
    )
    _emit(
        db,
        sess.tenant_id,
        TOPIC_FULFILLED,
        f"fulfilled:{sess.order_id}:sms",
        {"order_id": str(sess.order_id), "card_stock_ids": [str(card_id)]},
    )
    await _streak(ctx, sess.product_id, att.country, P.normalize(sess.params), code=True)
    return True


async def _finish_upstream(
    ctx: ProcurementContext, sup: HeroSmsSupplier, tenant_id: uuid.UUID, attempt_id: uuid.UUID
) -> None:
    """Release a number that delivered its code (best effort; HeroSMS also ends it on expiry)."""
    async with tenant_session(ctx.maker, tenant_id) as db:
        att = await db.scalar(
            select(SmsAttempt).where(SmsAttempt.tenant_id == tenant_id, SmsAttempt.id == attempt_id)
        )
        if att is None or att.upstream_closed:
            return
        try:
            await sup.client.finish(att.upstream_id)
        except SmsError as exc:
            log.info("sms finish refused: %s", clean_detail(exc.reason))
        att.upstream_closed = True
        await db.commit()


# --------------------------------------------------------------------------- loop
@dataclass
class _Waiting:
    id: uuid.UUID
    tenant_id: uuid.UUID
    supplier_id: uuid.UUID


async def poll_once(ctx: ProcurementContext) -> int:
    """One pass over waiting numbers. Returns how many still wait."""
    async with tenant_session(ctx.maker, None) as db:
        rows = (
            await db.execute(
                select(SmsAttempt.id, SmsAttempt.tenant_id, SmsAttempt.supplier_id).where(
                    SmsAttempt.state == "waiting"
                )
            )
        ).all()
    waiting = [_Waiting(*r) for r in rows]
    if not waiting:
        return 0
    by_supplier: dict[uuid.UUID, list[_Waiting]] = {}
    for w in waiting:
        by_supplier.setdefault(w.supplier_id, []).append(w)
    still = 0
    for supplier_id, items in by_supplier.items():
        async with tenant_session(ctx.maker, items[0].tenant_id) as db:
            ref = await _supplier_ref(db, supplier_id)
        sup = _client(ctx, ref) if ref is not None else None
        if sup is None:
            still += len(items)
            continue
        try:
            try:
                listed, complete = await sup.client.open_activations()
                open_now = {a.upstream_id: a for a in listed}
            except SmsError as exc:
                log.warning("sms open list failed: %s", clean_detail(exc.reason))
                still += len(items)
                continue
            for w in items:
                try:
                    if await _check_attempt(ctx, sup, w, open_now, complete):
                        still += 1
                except Exception as exc:  # one number must not stop the others
                    log.error("sms attempt check failed id=%s error=%s", w.id, type(exc).__name__)
                    still += 1
            open_now.clear()
        finally:
            await sup.aclose()
    return still


async def _check_attempt(
    ctx: ProcurementContext,
    sup: HeroSmsSupplier,
    w: _Waiting,
    open_now: dict[str, Activation],
    complete: bool = True,
) -> bool:
    """True = the number still waits. ``complete`` = the open list was read to its end (absence
    from a truncated list proves nothing)."""
    finish: uuid.UUID | None = None
    async with tenant_session(ctx.maker, w.tenant_id) as db:
        att = await db.scalar(
            select(SmsAttempt)
            .where(SmsAttempt.tenant_id == w.tenant_id, SmsAttempt.id == w.id)
            .with_for_update(skip_locked=True)
        )
        if att is None or att.state != "waiting":
            return False
        sess = await db.scalar(
            select(SmsSession)
            .where(SmsSession.tenant_id == w.tenant_id, SmsSession.id == att.session_id)
            .with_for_update(skip_locked=True)
        )
        if sess is None:  # a buyer action holds it: next pass
            return True
        now = utcnow()
        params = P.normalize(sess.params)
        age = (now - att.bought_at).total_seconds()
        status = await db.scalar(
            select(Order.status).where(Order.tenant_id == w.tenant_id, Order.id == att.order_id)
        )
        if status != "paid":
            # refunded / cancelled meanwhile: release the number, never deliver into it
            try:
                await sup.client.cancel(att.upstream_id)
            except SmsError as exc:
                if exc.outcome in (Outcome.RETRYABLE, Outcome.AMBIGUOUS):
                    return True
            att.state, att.closed_at, att.upstream_closed = "cancelled", now, True
            await _fail(db, sess, "order_closed", now, emit=False)
            await db.commit()
            return False
        a = open_now.get(att.upstream_id)
        codes = a.codes if a is not None else ()
        if a is None or (a.otp_total and not codes):
            # left the open list (finished / cancelled / expired upstream), or the list counted a
            # code it did not include: ask for the codes of this one number
            try:
                codes = await sup.client.codes(att.upstream_id)
            except SmsError:
                # unknown: keep waiting (a blip must not lose a paid code); give up only long
                # after the number's wait
                if age < params["number_wait_s"] + LOST_AFTER_S:
                    return True
                codes = ()
        if codes and sess.state == "active":
            if await _deliver(ctx, db, sess, att, codes[0], now):
                finish = att.id
                codes = ()
                await db.commit()
            else:
                codes = ()
                return True
        elif a is None and complete:
            await _close_attempt(ctx, db, sess, att, "no_code", now, params)
            await db.commit()
            return False
        elif age >= params["number_wait_s"]:
            try:
                await sup.client.cancel(att.upstream_id)
            except SmsError as exc:
                if exc.error_code == "EARLY_CANCEL_DENIED" or exc.outcome in (
                    Outcome.RETRYABLE,
                    Outcome.AMBIGUOUS,
                ):
                    return True
            await _close_attempt(ctx, db, sess, att, "no_code", now, params)
            await db.commit()
            return False
        else:
            return True
    if finish is not None:
        await _finish_upstream(ctx, sup, w.tenant_id, finish)
    return False


async def expire_sessions(ctx: ProcurementContext) -> int:
    """Fail sessions that can no longer get a code (no number waits and attempts / window / start
    time ran out). Returns the number failed."""
    now = utcnow()
    async with tenant_session(ctx.maker, None) as db:
        rows = (
            await db.execute(
                select(SmsSession.id, SmsSession.tenant_id).where(
                    SmsSession.state.in_(("ready", "active"))
                )
            )
        ).all()
    n = 0
    for sid, tid in rows:
        async with tenant_session(ctx.maker, tid) as db:
            sess = await db.scalar(
                select(SmsSession)
                .where(SmsSession.tenant_id == tid, SmsSession.id == sid)
                .with_for_update(skip_locked=True)
            )
            if sess is None or sess.state not in ("ready", "active"):
                continue
            params = P.normalize(sess.params)
            reason = None
            status = await db.scalar(
                select(Order.status).where(Order.tenant_id == tid, Order.id == sess.order_id)
            )
            if status != "paid" and sess.state == "ready":
                # refunded / cancelled before any number: close quietly (nothing to hold)
                await _fail(db, sess, "order_closed", now, emit=False)
                await db.commit()
                n += 1
                continue
            if sess.state == "ready":
                if now > sess.start_deadline:
                    reason = "not_started"
            else:
                waiting = await db.scalar(
                    select(func.count())
                    .select_from(SmsAttempt)
                    .where(
                        SmsAttempt.tenant_id == tid,
                        SmsAttempt.session_id == sid,
                        SmsAttempt.state == "waiting",
                    )
                )
                if not waiting:
                    if sess.attempts_used >= params["max_attempts"]:
                        reason = "attempts_exhausted"
                    elif sess.window_ends_at is not None and now >= sess.window_ends_at:
                        reason = "window_over"
            if reason is None:
                continue
            await _fail(db, sess, reason, now)
            await db.commit()
            n += 1
    return n


# --------------------------------------------------------------------------- availability
async def refresh_products(ctx: ProcurementContext) -> int:
    async with tenant_session(ctx.maker, None) as db:
        rows = (
            await db.execute(
                select(SmsProduct.tenant_id, SmsProduct.product_id).where(
                    SmsProduct.enabled.is_(True)
                )
            )
        ).all()
    for tid, pid in rows:
        await _refresh_one_safe(ctx, tid, pid)
    await _mark_disabled(ctx)
    return len(rows)


async def _mark_disabled(ctx: ProcurementContext) -> None:
    async with tenant_session(ctx.maker, None) as db:
        rows = (
            await db.execute(
                select(SmsProduct.tenant_id, SmsProduct.product_id).where(
                    SmsProduct.enabled.is_(False), SmsProduct.last_check_ok.is_not(False)
                )
            )
        ).all()
    for tid, pid in rows:
        async with tenant_session(ctx.maker, tid) as db:
            await _store_check(db, tid, pid, False, "disabled", 0)
            await db.commit()


async def _refresh_one_safe(ctx: ProcurementContext, tid: uuid.UUID, pid: uuid.UUID) -> None:
    try:
        await refresh_one(ctx, tid, pid)
    except Exception as exc:
        log.error("sms availability refresh failed product=%s error=%s", pid, type(exc).__name__)


async def _store_check(
    db: AsyncSession, tid: uuid.UUID, pid: uuid.UUID, ok: bool, reason: str | None, stock: int
) -> None:
    await db.execute(
        update(SmsProduct)
        .where(SmsProduct.tenant_id == tid, SmsProduct.product_id == pid)
        .values(
            last_check_ok=ok,
            last_check_at=utcnow(),
            last_check_reason=clean_detail(reason) if reason else None,
            stock_hint=max(0, min(STOCK_MAX, stock)),
        )
    )


async def refresh_one(
    ctx: ProcurementContext, tid: uuid.UUID, pid: uuid.UUID
) -> tuple[bool, str | None]:
    """Spec §5: enabled, supplier usable, a live route, daily cap left, balance covers every open
    session plus one at the highest live cap."""
    async with tenant_session(ctx.maker, tid) as db:
        cfg = await db.scalar(
            select(SmsProduct).where(SmsProduct.tenant_id == tid, SmsProduct.product_id == pid)
        )
        if cfg is None:
            return False, "not_configured"
        params = P.normalize(cfg.params)
        sup_row = (
            await db.scalar(select(SupplierAccount).where(SupplierAccount.id == cfg.supplier_id))
            if cfg.supplier_id
            else None
        )
        day0 = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        sold_today = int(
            await db.scalar(
                select(func.count())
                .select_from(SmsSession)
                .where(
                    SmsSession.tenant_id == tid,
                    SmsSession.product_id == pid,
                    SmsSession.created_at >= day0,
                )
            )
            or 0
        )
        open_n = int(
            await db.scalar(
                select(func.count())
                .select_from(SmsSession)
                .where(
                    SmsSession.tenant_id == tid,
                    SmsSession.product_id == pid,
                    SmsSession.state.in_(("ready", "active")),
                )
            )
            or 0
        )
    reason: str | None = None
    stock = 0
    if not cfg.enabled:
        reason = "disabled"
    elif sup_row is None or sup_row.adapter != "herosms":
        reason = "no_supplier"
    elif not sup_row.enabled or sup_row.breaker_open:
        reason = "supplier_off"
    elif sold_today >= params["daily_cap"]:
        reason = "daily_cap"
    if reason is None:
        countries = [r["country"] for r in params["routes"]]
        cooled = await _cooled(ctx, pid, countries)
        ref = SupplierRef(
            sup_row.id,
            sup_row.tenant_id,
            sup_row.code,
            sup_row.adapter,
            sup_row.env,
            sup_row.health,
        )
        sup = _client(ctx, ref)
        if sup is None:
            reason = "not_configured"
        else:
            try:
                balance = await sup.client.balance()
                listed = await sup.client.offers(params["service"], countries)
            except SmsError as exc:
                reason = clean_detail(exc.reason)
                balance, listed = Decimal(0), {}
            finally:
                await sup.aclose()
            if reason is None:
                live = set()
                for r in params["routes"]:
                    c = r["country"]
                    price, count = listed.get(c, (None, 0))
                    if (
                        c in cooled
                        or count <= 0
                        or price is None
                        or price > Decimal(r["max_price"])
                    ):
                        continue
                    live.add(c)
                if not live:
                    reason = "no_route"
                else:
                    cap = P.max_cap(params, live)
                    covers = (
                        int((balance / cap).to_integral_value(rounding=ROUND_FLOOR)) if cap else 0
                    )
                    if covers < open_n + 1:
                        reason = "low_balance"
                    else:
                        stock = min(params["daily_cap"] - sold_today, covers - open_n)
    ok = reason is None and stock > 0
    async with tenant_session(ctx.maker, tid) as db:
        await _store_check(db, tid, pid, ok, reason, stock if ok else 0)
        await db.commit()
    return ok, reason


# --------------------------------------------------------------------------- runner
async def run_sms_loop(ctx: ProcurementContext, stop: asyncio.Event) -> None:
    last_refresh = float("-inf")
    last_expire = float("-inf")
    while not stop.is_set():
        still = 0
        try:
            still = await poll_once(ctx)
        except Exception as exc:
            log.error("sms poll failed: %s", type(exc).__name__)
        mono = time.monotonic()
        if mono - last_expire >= 15:
            last_expire = mono
            try:
                await expire_sessions(ctx)
            except Exception as exc:
                log.error("sms expiry pass failed: %s", type(exc).__name__)
        if mono - last_refresh >= REFRESH_S:
            last_refresh = mono
            try:
                await refresh_products(ctx)
            except Exception as exc:
                log.error("sms availability pass failed: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), FAST_POLL_S if still else IDLE_POLL_S)
        except asyncio.TimeoutError:
            continue
