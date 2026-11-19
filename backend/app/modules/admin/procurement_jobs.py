"""Procurement jobs for staff (review-v12 §7): list, detail with timeline, and the actions
re-query, retry, switch source, fulfil manually, void and mark failed.

Rules that keep money and secrets safe:

* retry and re-query keep the job's idempotency key; only "switch source" (never when the
  supplier may already have charged) gives the job a new key, for a supplier that never saw it;
* a manual code is sealed with the vault PUBLIC key exactly like a card import (the API holds
  no private key), enters ``card_stock`` as ``sold`` to the order, and leaves through the same
  ``procure.fulfilled`` outbox event the engine uses; the code is never returned, logged or
  audited, and a job that is already ``fulfilled`` can never be fulfilled again;
* every action locks the job row first; the engine drops its own result when the state it
  leased no longer matches ("staff acted meanwhile: their action wins");
* a refund stays in the existing after-sales flow: "mark failed" only closes the job.

Tenant rule: every query filters by the caller's tenant (reseller tenants see their own jobs).
Roles: job actions need staff; void needs an admin.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.cardvault import CardContext
from app.core.deps import Page, client_ip, get_db, get_page, get_res, require_admin, require_staff
from app.core.errors import Conflict, Forbidden, InvalidState, NotFound, Unavailable, Validation
from app.core.ratelimit import enforce
from app.core.resources import Resources
from app.models import (
    AuditLog,
    CardStock,
    Order,
    ProcurementJob,
    SupplierAccount,
    SupplierLedgerEntry,
    SupplierOffer,
    User,
)
from app.modules.admin.suppliers import _dec, _iso
from app.modules.orders.admin import _checkout_left_review, hold_reason, lock_for_release
from app.modules.orders.events import add_event, snapshot
from app.procurement import engine
from app.suppliers import direct_progress as dp
from app.suppliers.base import CARD_FIELD_SEP, canonical_card_secret, clean_detail
from app.suppliers.zovocard.webhook import mask_pan

router = APIRouter(prefix="/admin/procurement/jobs", tags=["admin"])

STATES = (
    "queued",
    "submitting",
    "accepted_pending",
    "needs_manual",
    "fulfilled",
    "failed",
    "void_pending",
    "voided",
)
OPEN = ("queued", "submitting", "accepted_pending", "needs_manual")
RATE = ("procurement:job", 120, 3600)


_LONG_DIGITS = re.compile(r"[0-9](?:[ -]?[0-9]){11,}")


def _no_card_number(v: str | None) -> str | None:
    """A free-text reason is written to audit rows: it must never carry a card number (S2).
    12+ consecutive digits (spaces / dashes between them allowed) are refused."""
    if v is not None and _LONG_DIGITS.search(v):
        raise ValueError("a reason must not contain a card number")
    return v


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=200)

    check_reason = field_validator("reason")(_no_card_number)


class FulfilIn(BaseModel):
    # a CDK code / a card; a direct job takes NO code (the supplier did the work, staff attest it)
    code: str | None = Field(default=None, min_length=4, max_length=256, repr=False)
    # a card job: the supplier's card id when staff know it; it becomes the dedup fingerprint input
    # exactly as in the engine path, so the UNIQUE catches an engine / hand overlap
    card_id: str | None = Field(default=None, pattern=r"^[0-9]{1,20}$")
    reason: str | None = Field(default=None, max_length=200)
    # a direct job (owner 2026-11-19, ADR-048): staff confirm they ran the charge check (buyer
    # paid; the supplier order and the funding card show no charge for this order) first
    charge_checked: bool = False

    check_reason = field_validator("reason")(_no_card_number)


def maybe_charged(job: ProcurementJob) -> bool:
    """The supplier may hold a code for this job: such a job is only ever resolved through a
    same-key query (or by hand), never through another supplier. Reads the persisted
    ``maybe_sent`` flag (set before the first call of a send), not an error-code guess."""
    code = job.error_code or ""
    return bool(
        job.maybe_sent
        or job.upstream_ref
        or job.card_stock_ids
        or job.state in ("accepted_pending", "fulfilled", "void_pending")
        or code.startswith("void_rejected")
    )


def allowed_actions(job: ProcurementJob) -> list[str]:
    out: list[str] = []
    st = job.state
    if st in ("submitting", "accepted_pending", "needs_manual") and job.offer_id is not None:
        out.append("requery")
    if st == "needs_manual":
        out.append("retry")
    if st in ("needs_manual", "queued") and not maybe_charged(job):
        out.append("switch")
    if st in OPEN:
        out += ["fulfil", "fail"]
    if st in ("fulfilled", "needs_manual", "failed"):
        out.append("void")
    return out


def _job_view(
    j: ProcurementJob,
    order_no: str | None,
    sup: tuple[str, str, bool] | None,
    plan: str | None = None,
) -> dict[str, Any]:
    return {
        "id": str(j.id),
        "order_id": str(j.order_id),
        "order_no": order_no,
        "unit_no": j.unit_no,
        "kind": j.kind,
        "state": j.state,
        "step": j.step,
        "supplier_id": str(j.supplier_id) if j.supplier_id else None,
        "supplier_code": sup[0] if sup else None,
        "supplier_env": sup[1] if sup else None,
        "offer_id": str(j.offer_id) if j.offer_id else None,
        "error_code": j.error_code,
        "attempts": j.attempts,
        "upstream_ref": j.upstream_ref,
        "upstream_status": j.upstream_status,
        "card_mask": j.card_mask,  # first 6 + last 4 only; the card itself is sealed
        # upstream_direct: the MASKED account e-mail and the plan only, never the buyer's session
        "account_mask": j.account_mask,
        "plan": plan,
        "preflight_expires_at": _iso(j.preflight_expires_at),
        "card_id": j.upstream_ref
        if j.kind == "vcc"
        else None,  # the supplier's card id (not secret)
        "cost": _dec(j.cost),
        "currency": j.currency,
        "next_poll_at": _iso(j.next_poll_at),
        "manual_by": str(j.manual_by) if j.manual_by else None,
        "charged_possible": maybe_charged(j),
        "maybe_sent": bool(j.maybe_sent),
        "recovering": bool(j.recover),
        "actions": allowed_actions(j),
        "created_at": _iso(j.created_at),
        "updated_at": _iso(j.updated_at),
    }


async def _view_of(db: AsyncSession, j: ProcurementJob) -> dict[str, Any]:
    await db.flush()
    await db.refresh(j)  # server-side updated_at / defaults after the UPDATE
    order_no = await db.scalar(
        select(Order.order_no).where(Order.id == j.order_id, Order.tenant_id == j.tenant_id)
    )
    sup = None
    if j.supplier_id is not None:
        r = (
            await db.execute(
                select(SupplierAccount.code, SupplierAccount.env, SupplierAccount.enabled).where(
                    SupplierAccount.id == j.supplier_id, SupplierAccount.tenant_id == j.tenant_id
                )
            )
        ).first()
        sup = (r.code, r.env, r.enabled) if r else None
    return _job_view(j, order_no, sup, await _plan_of(db, j))


async def _plan_of(db: AsyncSession, j: ProcurementJob) -> str | None:
    """``<product>/<plan>`` of a direct job's source (offer parameters carry no secret)."""
    if j.kind != "direct" or j.offer_id is None:
        return None
    params = await db.scalar(
        select(SupplierOffer.params).where(
            SupplierOffer.id == j.offer_id, SupplierOffer.tenant_id == j.tenant_id
        )
    )
    return f"{params.get('product')}/{params.get('plan')}" if params else None


# ------------------------------------------------------------------ reads
@router.get("")
async def list_jobs(
    state: str | None = Query(default=None),
    order_id: uuid.UUID | None = Query(default=None),
    staff: User = Depends(require_staff),
    page: Page = Depends(get_page),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if state is not None and state not in STATES:
        raise Validation("Unknown state", code="bad_state")
    tid = staff.tenant_id
    where = [ProcurementJob.tenant_id == tid]
    if state:
        where.append(ProcurementJob.state == state)
    if order_id:
        where.append(ProcurementJob.order_id == order_id)
    total = await db.scalar(select(func.count(ProcurementJob.id)).where(*where)) or 0
    rows = (
        await db.execute(
            select(ProcurementJob, Order.order_no, SupplierAccount, SupplierOffer.params)
            .join(Order, Order.id == ProcurementJob.order_id)
            .outerjoin(SupplierAccount, SupplierAccount.id == ProcurementJob.supplier_id)
            .outerjoin(SupplierOffer, SupplierOffer.id == ProcurementJob.offer_id)
            .where(*where, Order.tenant_id == tid)
            .order_by(ProcurementJob.created_at.desc(), ProcurementJob.id)
            .limit(page.page_size)
            .offset(page.offset)
        )
    ).all()
    counts = dict(
        (
            await db.execute(
                select(ProcurementJob.state, func.count())
                .where(ProcurementJob.tenant_id == tid)
                .group_by(ProcurementJob.state)
            )
        ).all()
    )
    items = [
        _job_view(
            j,
            no,
            (s.code, s.env, s.enabled) if s is not None else None,
            f"{prm.get('product')}/{prm.get('plan')}" if j.kind == "direct" and prm else None,
        )
        for j, no, s, prm in rows
    ]
    out = page.wrap(items, int(total))
    out["counts"] = {k: int(counts.get(k, 0)) for k in STATES}
    return out


async def _timeline(db: AsyncSession, tid: uuid.UUID, job_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = await db.scalars(
        select(AuditLog)
        .where(AuditLog.tenant_id == tid, AuditLog.target == f"procurement_job:{job_id}")
        .order_by(AuditLog.id)
        .limit(200)
    )
    out = []
    for a in rows:
        d = a.detail or {}
        out.append(
            {
                "at": _iso(a.created_at),
                "action": a.action,
                "actor_id": str(a.actor_id) if a.actor_id else None,
                "actor_role": a.actor_role,
                "severity": a.severity,
                "before": a.before,
                "after": a.after,
                "reason": d.get("reason") or d.get("action"),
                "error_code": d.get("error_code"),
            }
        )
    return out


@router.get("/{job_id}")
async def job_detail(
    job_id: uuid.UUID,
    staff: User = Depends(require_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tid = staff.tenant_id
    job = await db.scalar(
        select(ProcurementJob).where(ProcurementJob.id == job_id, ProcurementJob.tenant_id == tid)
    )
    if job is None:
        raise NotFound("Job not found")
    view = await _view_of(db, job)
    order = (
        await db.execute(
            select(
                Order.id, Order.order_no, Order.status, Order.qty, Order.amount_minor,
                Order.currency, Order.product_id, Order.variant_id, Order.delivery_mode,
            ).where(Order.id == job.order_id, Order.tenant_id == tid)
        )
    ).first()  # fmt: skip
    ledger = (
        await db.execute(
            select(
                SupplierLedgerEntry.id,
                SupplierLedgerEntry.type,
                SupplierLedgerEntry.amount,
                SupplierLedgerEntry.currency,
                SupplierLedgerEntry.occurred_at,
            )
            .where(SupplierLedgerEntry.tenant_id == tid, SupplierLedgerEntry.job_id == job.id)
            .order_by(SupplierLedgerEntry.occurred_at)
            .limit(50)
        )
    ).all()
    view["tried_offer_ids"] = [str(x) for x in job.tried_offer_ids or []]
    view["has_code"] = bool(job.card_stock_ids)  # never the code itself
    view["order"] = (
        {
            "id": str(order.id),
            "order_no": order.order_no,
            "status": order.status,
            "qty": order.qty,
            "amount_minor": order.amount_minor,
            "currency": order.currency,
            "product_id": str(order.product_id) if order.product_id else None,
            "variant_id": str(order.variant_id) if order.variant_id else None,
            "delivery_mode": order.delivery_mode,
        }
        if order
        else None
    )
    view["ledger"] = [
        {
            "id": str(r.id),
            "type": r.type,
            "amount": _dec(r.amount),
            "currency": r.currency,
            "occurred_at": _iso(r.occurred_at),
        }
        for r in ledger
    ]
    view["timeline"] = await _timeline(db, tid, job.id)
    view.update(await _supplier_progress(db, job))
    return view


async def _supplier_progress(db: AsyncSession, job: ProcurementJob) -> dict[str, Any]:
    """Issue #30: the supplier timeline (one entry per CHANGE, already sanitised when it was
    recorded), the last vendor state and the buyer's current step. Direct jobs only."""
    if job.kind != "direct":
        return {}
    doc = job.progress or {}
    order_status = await db.scalar(
        select(Order.status).where(Order.id == job.order_id, Order.tenant_id == job.tenant_id)
    )
    p = dp.from_job(job, order_refunded=order_status == "refunded")
    return {
        "supplier_timeline": list(doc.get("events") or []),
        "vendor_state": dict(doc.get("raw") or doc.get("v") or {}),
        "buyer_progress": {
            "step": p.step,
            "percent": p.percent,
            "outcome": p.outcome,
            "reason": p.reason,
        },
    }


# ------------------------------------------------------------------ actions
async def _lock(db: AsyncSession, tid: uuid.UUID, job_id: uuid.UUID) -> ProcurementJob:
    """Lock the job together with its siblings (one ordered statement: job set -> order ->
    supplier is the lock order everywhere, L4)."""
    job = await engine.lock_job(db, tid, job_id)
    if job is None:
        raise NotFound("Job not found")
    return job


def _need(job: ProcurementJob, states: tuple[str, ...], action: str) -> None:
    if job.state not in states:
        raise InvalidState(
            f"A {job.state} job cannot be {action}", code="job_state", extra={"state": job.state}
        )


async def _resume_order(db: AsyncSession, job: ProcurementJob, actor: User, ip: str | None) -> None:
    """The order must be ``paid`` for the engine and for delivery. A hold the procurement itself
    caused (``procurement_failed``) is lifted here; any other hold stays with its own flow."""
    order = await lock_for_release(db, job.order_id, job.tenant_id)
    if order.status == "paid":
        return
    if order.status != "manual_review":
        raise InvalidState(
            f"The order is {order.status}", code="order_not_open", extra={"status": order.status}
        )
    reason, _kind = await hold_reason(db, order)
    if reason != "procurement_failed":
        raise InvalidState("The order is held for another reason", code="order_held")
    before = snapshot(order)
    order.status = "paid"
    order.version += 1
    await add_event(
        db, order, "manual_review", "paid", actor_id=actor.id, reason="procurement_resumed"
    )
    await _checkout_left_review(db, order, actor, ip)
    await audit(
        db,
        tenant_id=order.tenant_id,
        action="order.procurement_resumed",
        actor_id=actor.id,
        actor_role=actor.role,
        target=f"order:{order.id}",
        before=before,
        after=snapshot(order),
        detail={"job_id": str(job.id)},
        ip=ip,
    )


async def _note(
    db: AsyncSession, job: ProcurementJob, actor: User, ip: str | None, action: str, **detail: Any
) -> None:
    """An audit row for an action that changed bookkeeping but not the job state."""
    await audit(
        db,
        tenant_id=job.tenant_id,
        action="procurement.job_action",
        actor_id=actor.id,
        actor_role=actor.role,
        target=f"procurement_job:{job.id}",
        after={"state": job.state, "step": job.step},
        detail={"action": action, "order_id": str(job.order_id), **detail},
        ip=ip,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _refuse_in_flight(job: ProcurementJob) -> None:
    """The engine is talking to the supplier about this job (its lease is live): the answer it
    brings could be a code nobody would record, so staff wait for it (M1)."""
    if job.lease_until is not None and job.lease_until > _now():
        raise Conflict(
            "The engine is talking to the supplier about this job right now; try again in a minute",
            code="job_in_flight",
        )


def _schedule_recovery(job: ProcurementJob) -> bool:
    """A job closed by hand while the supplier may hold a code for it: the engine asks once under
    the SAME key and records the reference (it never seals or delivers anything). A closed job
    (``failed``) then has the code disabled; a hand-fulfilled one keeps it, since staff may have
    pasted exactly that code (N2): disabling stays an explicit ``void``."""
    if job.maybe_sent or job.upstream_ref:
        job.recover, job.attempts, job.next_poll_at = True, 0, _now()
        return True
    return False


@router.get("/{job_id}/charge-check")
async def charge_check(
    job_id: uuid.UUID,
    staff: User = Depends(require_staff),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    """Evidence before a hand top-up / reissue (ADR-048): did the buyer pay, what the supplier
    order says, the job's automatic top-ups and the funding card's latest spends. Read only."""
    await enforce(res.redis, RATE[0], str(staff.id), RATE[1], RATE[2])
    tid = staff.tenant_id
    row = (
        await db.execute(
            select(
                ProcurementJob.kind, Order.status, Order.paid_at, Order.amount_minor, Order.currency
            )
            .join(Order, Order.id == ProcurementJob.order_id)
            .where(ProcurementJob.id == job_id, ProcurementJob.tenant_id == tid)
        )
    ).first()
    if row is None:
        raise NotFound("Job not found")
    await db.rollback()
    payment = {
        "order_status": row.status,
        "paid": row.paid_at is not None,
        "paid_at": _iso(row.paid_at),
        "amount_minor": row.amount_minor,
        "currency": row.currency,
    }
    if row.kind != "direct":
        return {"payment": payment, "supplier": None}
    token = res.settings.procurement_token
    if len(token) < 32:
        raise Unavailable("The procurement service is not configured", code="procurement_off")
    try:
        resp = await res.procurement.post(
            "/internal/charge-check",
            json={"tenant_id": str(tid), "job_id": str(job_id)},
            headers={"X-Internal-Token": token},
        )
    except httpx.HTTPError:
        raise Unavailable(
            "The procurement service is unreachable", code="procurement_unreachable"
        ) from None
    if resp.status_code == 404:
        raise NotFound("Job not found")
    if resp.status_code != 200:
        raise Unavailable("The supplier could not be read", code="procurement_unreachable")
    return {"payment": payment, "supplier": resp.json()}


@router.post("/{job_id}/requery")
async def requery(
    job_id: uuid.UUID,
    request: Request,
    staff: User = Depends(require_staff),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    """Ask the supplier about this job again, under the SAME key (the engine does the call)."""
    await enforce(res.redis, RATE[0], str(staff.id), RATE[1], RATE[2])
    ip = client_ip(request)
    job = await _lock(db, staff.tenant_id, job_id)
    _need(job, ("submitting", "accepted_pending", "needs_manual"), "re-queried")
    if job.offer_id is None:
        raise InvalidState("This job was never sent to a supplier", code="never_sent")
    if job.state == "needs_manual":
        await _resume_order(db, job, staff, ip)
        job.step, job.attempts, job.error_code = "reconcile", 0, None
        job.next_poll_at = _now()
        await engine._transition(
            db, job, "submitting", "manual:requery", actor_id=staff.id, actor_role=staff.role, ip=ip
        )
    else:
        job.next_poll_at = _now()
        await _note(db, job, staff, ip, "requery")
    view = await _view_of(db, job)
    await db.commit()
    return view


@router.post("/{job_id}/retry")
async def retry(
    job_id: uuid.UUID,
    request: Request,
    staff: User = Depends(require_staff),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    """Run a ``needs_manual`` job again with the SAME idempotency key (never a new one). When the
    supplier may already have charged, the engine asks it first (reconcile) instead of sending."""
    await enforce(res.redis, RATE[0], str(staff.id), RATE[1], RATE[2])
    ip = client_ip(request)
    job = await _lock(db, staff.tenant_id, job_id)
    _need(job, ("needs_manual",), "retried")
    key_before = job.idempotency_key
    await _resume_order(db, job, staff, ip)
    charged = maybe_charged(job) and job.offer_id is not None  # judged BEFORE the code is cleared
    job.attempts, job.error_code, job.next_poll_at = 0, None, _now()
    job.step, to = ("reconcile", "submitting") if charged else ("submit", "queued")
    assert job.idempotency_key == key_before
    await engine._transition(
        db, job, to, "manual:retry", actor_id=staff.id, actor_role=staff.role, ip=ip
    )
    view = await _view_of(db, job)
    await db.commit()
    return view


@router.post("/{job_id}/switch")
async def switch_source(
    job_id: uuid.UUID,
    request: Request,
    staff: User = Depends(require_staff),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    """Move a job that was NOT charged to the next source (a fresh key for a supplier that never
    saw the old one). Refused when the current supplier may already have charged."""
    await enforce(res.redis, RATE[0], str(staff.id), RATE[1], RATE[2])
    ip = client_ip(request)
    job = await _lock(db, staff.tenant_id, job_id)
    _need(job, ("needs_manual", "queued"), "switched to another source")
    if maybe_charged(job):
        raise InvalidState(
            "The supplier may already have charged this job; query it or fulfil it by hand",
            code="maybe_charged",
        )
    order = await engine.load_order(db, job.order_id)
    if order is None:
        raise NotFound("Order not found")
    tried = [*(job.tried_offer_ids or []), *([job.offer_id] if job.offer_id else [])]
    shim: Any = SimpleNamespace(redis=res.redis)
    if not await engine.candidates(shim, db, order, exclude=tried):
        raise InvalidState("No other enabled source is left", code="no_other_source")
    await _resume_order(db, job, staff, ip)
    await engine._failover(
        shim, db, job, order, "manual_switch", actor_id=staff.id, actor_role=staff.role, ip=ip
    )
    job.error_code = None
    view = await _view_of(db, job)
    await db.commit()
    return view


@router.post("/{job_id}/fulfil")
async def fulfil_manually(
    job_id: uuid.UUID,
    body: FulfilIn,
    request: Request,
    staff: User = Depends(require_staff),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    """Paste the code: it is sealed (public key) into ``card_stock`` as sold to the order and the
    job becomes ``fulfilled`` through the same ``procure.fulfilled`` event as an automatic one."""
    await enforce(res.redis, RATE[0], str(staff.id), RATE[1], RATE[2])
    ip = client_ip(request)
    tid, is_admin = staff.tenant_id, staff.role == "admin"  # (a rollback expires `staff`)
    pre = await db.scalar(
        select(ProcurementJob.kind).where(
            ProcurementJob.id == job_id, ProcurementJob.tenant_id == tid
        )
    )
    if pre == "direct" and is_admin:
        # hand-fulfilling a job whose supplier order is still moving would double-subscribe:
        # read the order first (the procurement process asks the vendor; fail closed) (M4).
        # the SELECT above holds no lock, so the open transaction blocks nobody
        await _refuse_if_order_in_flight(res, tid, job_id)
    job = await _lock(db, tid, job_id)
    direct = job.kind == "direct"
    if direct:
        # the supplier does the work for a direct job: staff ATTEST that they did it by hand at the
        # supplier. No code is pasted; the buyer's delivery is the plan / masked account / order.
        if staff.role != "admin":
            raise Forbidden("Only an admin can mark a direct job done by hand")
        if body.code:
            raise Validation("A direct job takes no code", code="direct_no_code")
        if len((body.reason or "").strip()) < 12:
            raise Validation(
                "Say what you did at the supplier (at least 12 characters)", code="reason_required"
            )
        if not body.charge_checked:
            raise Validation(
                "Run the charge check first: confirm the buyer paid and that neither the supplier"
                " order nor the funding card already charged for this order",
                code="charge_check_required",
            )
        code = await _direct_text(db, job)
    else:
        code = (body.code or "").strip()
        if not (4 <= len(code) <= 256) or not code.isprintable():
            raise Validation("That is not a valid code", code="bad_code")
    mask = None
    last4 = ""
    if job.kind == "vcc":
        if staff.role != "admin":  # a card is live money: only an admin pastes one (S2)
            raise Forbidden("Only an admin can fulfil a card job by hand")
        # a card is pasted as ``<pan>----<MM/YY>----<cvv>`` and sealed exactly like an automatic one
        canonical = canonical_card_secret(code)
        if canonical is None:
            raise Validation("Paste the card as number----MM/YY----CVV", code="bad_card")
        code = canonical
        mask = mask_pan(code.split(CARD_FIELD_SEP, 1)[0])
        # the same card must not end up in two jobs (the fingerprint no longer derives from it):
        # refuse a card whose mask another live card job of this tenant already holds
        clash = await db.scalar(
            select(func.count(ProcurementJob.id)).where(
                ProcurementJob.tenant_id == staff.tenant_id,
                ProcurementJob.kind == "vcc",
                ProcurementJob.card_mask == mask,
                ProcurementJob.id != job.id,
                ProcurementJob.state != "voided",
            )
        )
        if clash:
            raise Conflict(
                "Another card job already holds this card; void it or use the supplier card id",
                code="duplicate_code",
            )
        last4 = code.split(CARD_FIELD_SEP, 1)[0][-4:]
        del canonical
    if job.state == "fulfilled" or job.card_stock_ids:
        raise Conflict("This job is already fulfilled", code="already_fulfilled")
    _need(job, OPEN, "fulfilled by hand")
    _refuse_in_flight(job)
    await _resume_order(db, job, staff, ip)
    order = await engine.load_order(db, job.order_id)
    if order is None or order.product_id is None or order.status != "paid":
        raise InvalidState("The order cannot be fulfilled", code="order_not_open")
    sealer = res.sealer
    card_id = uuid.uuid4()
    tid = str(order.tenant_id)
    values = {
        "id": card_id,
        "tenant_id": order.tenant_id,
        "product_id": order.product_id,
        "variant_id": order.variant_id,
        "blob": sealer.seal(code, CardContext(tid, str(order.product_id), str(card_id))),
        # a card's fingerprint never derives from the secret (S1); a CDK code keeps the old rule
        "fingerprint": (
            sealer.card_fingerprint(
                tid,
                body.card_id or job.upstream_ref or f"manual:{order.id}:{job.unit_no}:{last4}",
            )
            if job.kind == "vcc"
            else sealer.card_fingerprint(
                tid, f"direct:{job.supplier_id or 'manual'}:{job.upstream_ref or order.id}"
            )
            if direct
            else sealer.fingerprint(tid, code)
        ),
        "lookup_fp": None
        if job.kind in ("vcc", "direct")
        else sealer.lookup_fingerprint(tid, code),
        "kid": sealer.active_kid,
        "status": "sold",
        "order_id": order.id,
        "created_by": staff.id,
    }
    del code
    try:
        async with db.begin_nested():
            await db.execute(insert(CardStock.__table__).values(**values))
    except IntegrityError:
        raise Conflict("This code is already in stock", code="duplicate_code") from None
    finally:
        values.clear()
    job.card_stock_ids = [card_id]
    if mask is not None:
        await audit(  # who pasted a card, never the card
            db,
            tenant_id=job.tenant_id,
            action="procurement.card_pasted",
            actor_id=staff.id,
            actor_role=staff.role,
            target=f"procurement_job:{job.id}",
            detail={"order_id": str(job.order_id), "mask": mask},
            severity="warning",
            ip=ip,
        )
        if job.card_mask and job.card_mask != mask:
            # the supplier bought another card for this job than the one staff delivered: the
            # vendor's card is an orphan that must be deleted (F6); both masks are kept
            await audit(
                db,
                tenant_id=job.tenant_id,
                action="procurement.manual_card_differs",
                actor_id=staff.id,
                actor_role=staff.role,
                target=f"procurement_job:{job.id}",
                detail={
                    "vendor_mask": job.card_mask,
                    "pasted_mask": mask,
                    "order_id": str(job.order_id),
                    "vendor_card": clean_detail(job.upstream_ref or ""),
                },
                severity="critical",
                ip=ip,
            )
        job.card_mask = mask
    if direct:
        await audit(  # a hand-fulfilled direct order: who attested, why, and what the supplier held
            db,
            tenant_id=job.tenant_id,
            action="procurement.direct_manual_fulfil",
            actor_id=staff.id,
            actor_role=staff.role,
            target=f"procurement_job:{job.id}",
            detail={
                "order_id": str(job.order_id),
                "reason": clean_detail(body.reason or "", 120),
                "upstream_ref": clean_detail(job.upstream_ref or ""),
                "supplier_may_hold_order": bool(job.maybe_sent or job.upstream_ref),
            },
            severity="critical",
            ip=ip,
        )
    if mask is not None and body.card_id and not job.upstream_ref:
        # the vendor card id staff named is recorded, so a funding card can never be this card
        job.upstream_ref = body.card_id
    job.manual_by = staff.id
    job.error_code, job.step, job.next_poll_at = None, "done", None
    recovering = _schedule_recovery(job)  # the supplier's own code, if any, is disabled
    reason = clean_detail(body.reason or "manual_fulfil", 120)
    await engine._transition(
        db, job, "fulfilled", f"manual:{reason}", actor_id=staff.id, actor_role=staff.role, ip=ip
    )
    if recovering:
        await _note(db, job, staff, ip, "recover_scheduled")
    await engine.emit_if_complete(db, order)
    view = await _view_of(db, job)
    await db.commit()
    return view


async def _refuse_if_order_in_flight(res: Resources, tid: uuid.UUID, job_id: uuid.UUID) -> None:
    token = res.settings.procurement_token
    if len(token) < 32:
        raise Unavailable("The procurement service is not configured", code="procurement_off")
    try:
        resp = await res.procurement.post(
            "/internal/direct-order",
            json={"tenant_id": str(tid), "job_id": str(job_id)},
            headers={"X-Internal-Token": token},
        )
    except httpx.HTTPError:
        raise Unavailable(
            "The procurement service is unreachable", code="procurement_unreachable"
        ) from None
    if resp.status_code == 404:
        raise NotFound("Job not found")
    if resp.status_code != 200:
        raise Unavailable("The supplier order could not be read", code="procurement_unreachable")
    state = str(resp.json().get("state"))
    if state in ("pending", "unknown"):
        raise Conflict(
            "The supplier's order is still in progress (or unreadable): cancel it first with "
            "void, or wait; hand-fulfilling now could subscribe the account twice",
            code="order_in_flight",
            extra={"order_state": state},
        )


async def _direct_text(db: AsyncSession, job: ProcurementJob) -> str:
    """The buyer-facing delivery of a hand-fulfilled direct job: plan, masked account, order."""
    plan = await _plan_of(db, job)
    ref = job.upstream_ref or f"manual-{job.id.hex[:8]}"
    return CARD_FIELD_SEP.join((plan or "direct", job.account_mask or "-", ref))


@router.post("/{job_id}/void")
async def void_job(
    job_id: uuid.UUID,
    body: ReasonIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    """Void the job. With an upstream reference the engine disables the code at the supplier
    first (``void_pending`` -> ``voided``); without one it is voided at once. The buyer's refund
    stays in the after-sales flow."""
    await enforce(res.redis, RATE[0], str(admin.id), RATE[1], RATE[2])
    ip = client_ip(request)
    job = await _lock(db, admin.tenant_id, job_id)
    _need(job, ("fulfilled", "needs_manual", "failed"), "voided")
    if job.recover:
        raise InvalidState(
            "The engine is still recovering a possible code for this job", code="recover_pending"
        )
    reason = clean_detail(body.reason, 120)
    if job.upstream_ref and job.offer_id is not None:
        # a delete already accepted (202) is only ever read afterwards: its step survives; a job
        # that ended in void_failed (step done) restarts the void from the beginning
        job.step = "delete_pending" if job.step == "delete_pending" else "void"
        job.attempts, job.next_poll_at = 0, _now()
        await engine._transition(
            db, job, "void_pending", f"manual:{reason}", actor_id=admin.id,
            actor_role=admin.role, ip=ip, severity="warning",
        )  # fmt: skip
    elif job.maybe_sent and job.offer_id is not None:
        # the supplier may hold a code we never learned about: never "voided" without a call;
        # the engine asks once under the same key, records the reference and disables it
        job.error_code = "void_requested"
        job.step = "done"
        _schedule_recovery(job)
        await engine._transition(
            db, job, "failed", f"manual:{reason}", actor_id=admin.id,
            actor_role=admin.role, ip=ip, severity="warning",
        )  # fmt: skip
        await _note(db, job, admin, ip, "recover_scheduled")
    else:
        job.step, job.next_poll_at = "done", None
        await engine._transition(
            db, job, "voided", f"manual:{reason}", actor_id=admin.id,
            actor_role=admin.role, ip=ip, severity="warning",
        )  # fmt: skip
    # after the transition: the database only lets the API void a sold code of a job in a void
    # state (card_stock_guard, migration 0027)
    voided_cards = await _void_cards(db, job, admin, reason)
    if voided_cards:
        await _note(db, job, admin, ip, "cards_voided", cards=voided_cards)
    view = await _view_of(db, job)
    await db.commit()
    return view


async def _void_cards(db: AsyncSession, job: ProcurementJob, admin: User, reason: str) -> int:
    """The sealed code of a voided job can never be delivered (or delivered again): its
    ``card_stock`` row goes to ``void`` at once, before the supplier is asked to disable it."""
    if not job.card_stock_ids:
        return 0
    res = await db.execute(
        update(CardStock)
        .where(
            CardStock.tenant_id == job.tenant_id,
            CardStock.id.in_(list(job.card_stock_ids)),
            CardStock.status != "void",
        )
        .values(
            status="void",
            void_reason=clean_detail(f"procurement_void:{reason}", 64),
            voided_at=_now(),
            voided_by=admin.id,
        )
    )
    return int(res.rowcount or 0)  # type: ignore[attr-defined]


@router.post("/{job_id}/fail")
async def mark_failed(
    job_id: uuid.UUID,
    body: ReasonIn,
    request: Request,
    staff: User = Depends(require_staff),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    """Close the job as failed. The order goes (or stays) in ``manual_review`` so staff refund it
    through the existing after-sales flow; nothing is refunded here."""
    await enforce(res.redis, RATE[0], str(staff.id), RATE[1], RATE[2])
    ip = client_ip(request)
    job = await _lock(db, staff.tenant_id, job_id)
    _need(job, OPEN, "marked as failed")
    _refuse_in_flight(job)
    reason = clean_detail(body.reason, 120)
    job.error_code = "marked_failed"
    job.step, job.next_poll_at = "done", None
    recovering = _schedule_recovery(job)
    await engine._transition(
        db, job, "failed", f"manual:{reason}", actor_id=staff.id, actor_role=staff.role, ip=ip,
        severity="warning",
    )  # fmt: skip
    if recovering:
        await _note(db, job, staff, ip, "recover_scheduled")
    engine._emit(
        db,
        job.tenant_id,
        engine.TOPIC_FAILED,
        f"failed:{job.order_id}:{job.id}",
        {"order_id": str(job.order_id), "job_id": str(job.id), "reason": "marked_failed"},
    )
    view = await _view_of(db, job)
    await db.commit()
    return view
