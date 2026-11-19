"""Automatic top-ups of direct orders' funding cards: policy, cap and records (ADR-048).

The engine plans a top-up when it assigns a card that is short of the order's need; the adapter
sends it (``POST /cards/recharge``, under the row's ``idem_key``) right before the order, and the
engine settles the row from the answer. Every planned row counts against the daily cap until it
is settled ``failed`` / ``void``, so an unclear answer can only make the cap stricter. The cap
check and the insert run under one per-tenant advisory lock (two leases cannot both pass it).
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timezone
from decimal import Decimal

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import funding as fs
from app.core.audit import audit
from app.models import ProcurementFunding, ProcurementJob, Tenant
from app.suppliers.base import FundingPolicy, job_idempotency_key

COUNTED = ("planned", "sent", "done")
PREFERRED_MAX = 5


def _day_start(now: datetime) -> datetime:
    return datetime.combine(now.astimezone(timezone.utc).date(), time(0), tzinfo=timezone.utc)


async def settings_of(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, object]:
    raw = await db.scalar(select(Tenant.funding_settings).where(Tenant.id == tenant_id))
    return fs.normalize(raw)


async def spent_today(db: AsyncSession, tenant_id: uuid.UUID, now: datetime) -> Decimal:
    """Today's (UTC) counted top-ups of the TENANT, across all its suppliers: the caps are
    per tenant (what the console shows)."""
    total = await db.scalar(
        select(func.coalesce(func.sum(ProcurementFunding.amount), 0)).where(
            ProcurementFunding.tenant_id == tenant_id,
            ProcurementFunding.state.in_(COUNTED),
            ProcurementFunding.created_at >= _day_start(now),
        )
    )
    return Decimal(total or 0)


def _allowance(settings: dict[str, object], spent: Decimal) -> Decimal:
    if settings.get("auto_recharge") is not True:
        return Decimal(0)
    daily = Decimal(str(settings["daily_cap_usd"]))
    per = Decimal(str(settings["per_topup_max_usd"]))
    return max(Decimal(0), min(per, daily - spent))


async def preferred_cards(
    db: AsyncSession, tenant_id: uuid.UUID, supplier_id: uuid.UUID
) -> tuple[str, ...]:
    """The cards that paid this supplier's latest fulfilled direct orders, newest first."""
    rows = (
        await db.execute(
            select(func.coalesce(ProcurementJob.used_card_id, ProcurementJob.card_id))
            .where(
                ProcurementJob.tenant_id == tenant_id,
                ProcurementJob.supplier_id == supplier_id,
                ProcurementJob.kind == "direct",
                ProcurementJob.state == "fulfilled",
                func.coalesce(ProcurementJob.used_card_id, ProcurementJob.card_id).is_not(None),
            )
            .order_by(ProcurementJob.updated_at.desc())
            .limit(PREFERRED_MAX * 4)
        )
    ).all()
    out: list[str] = []
    for (cid,) in rows:
        if cid and cid not in out:
            out.append(cid)
    return tuple(out[:PREFERRED_MAX])


async def policy(
    db: AsyncSession, tenant_id: uuid.UUID, supplier_id: uuid.UUID, now: datetime
) -> FundingPolicy:
    st = await settings_of(db, tenant_id)
    spent = await spent_today(db, tenant_id, now) if st["auto_recharge"] else 0
    return FundingPolicy(
        auto_recharge=st["auto_recharge"] is True,
        allowance=_allowance(st, Decimal(spent)),
        bin_priority=tuple(str(b) for b in st["bin_priority"]),  # type: ignore[attr-defined]
        preferred=await preferred_cards(db, tenant_id, supplier_id),
    )


async def plan(
    db: AsyncSession, job: ProcurementJob, card_id: str, amount: Decimal, now: datetime
) -> bool:
    """Record a planned top-up for ``job``'s picked card when it fits the cap. ``False`` = it does
    not (nothing recorded). The caller commits with the lease."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": f"fund:{job.tenant_id}"},  # one lock per tenant: the cap is per tenant
    )
    st = await settings_of(db, job.tenant_id)
    assert job.supplier_id is not None
    left = _allowance(st, await spent_today(db, job.tenant_id, now))
    if amount <= 0 or amount > left:
        return False
    n = await db.scalar(
        select(func.count(ProcurementFunding.id)).where(
            ProcurementFunding.tenant_id == job.tenant_id, ProcurementFunding.job_id == job.id
        )
    )
    key = job_idempotency_key(job.id.hex, f"fund{int(n or 0) + 1}")
    db.add(
        ProcurementFunding(
            id=uuid.uuid4(),
            tenant_id=job.tenant_id,
            supplier_id=job.supplier_id,
            job_id=job.id,
            order_id=job.order_id,
            card_id=card_id,
            amount=amount,
            state="planned",
            idem_key=key,
        )
    )
    job.fund_amount, job.fund_key = amount, key
    await audit(
        db,
        tenant_id=job.tenant_id,
        action="procurement.topup_planned",
        target=f"procurement_job:{job.id}",
        after={"card_id": card_id, "amount": str(amount), "currency": "USD"},
        detail={"order_id": str(job.order_id), "key": key},
        severity="info",
    )
    return True


async def settle(
    db: AsyncSession, job: ProcurementJob, *, funded: Decimal | None, error: str | None
) -> None:
    """The adapter's answer for the job's planned top-up: ``funded`` -> ``done``; a definite
    refusal (``error``) -> ``failed``. Anything else leaves it ``planned`` (still counted)."""
    if not job.fund_key:
        return
    if funded is not None:
        values = {"state": "done", "amount": funded, "error": None}
    elif error:
        values = {"state": "failed", "error": error[:32]}
    else:
        return
    res = await db.execute(
        update(ProcurementFunding)
        .where(
            ProcurementFunding.tenant_id == job.tenant_id,
            ProcurementFunding.idem_key == job.fund_key,
            ProcurementFunding.state.in_(("planned", "sent")),
        )
        .values(**values, updated_at=func.now())
    )
    if res.rowcount:  # type: ignore[attr-defined]
        await audit(
            db,
            tenant_id=job.tenant_id,
            action="procurement.topup_done" if funded is not None else "procurement.topup_failed",
            target=f"procurement_job:{job.id}",
            after={"state": values["state"], "amount": str(funded) if funded else None},
            detail={"order_id": str(job.order_id), "key": job.fund_key, "error": error},
            severity="info" if funded is not None else "warning",
        )
    # done or failed: the plan is spent either way. A later attempt of the same job must never
    # resend the top-up (the vendor's replay window is not documented); after a done top-up it
    # relies on the real-time card read alone (security review M1)
    job.fund_amount = job.fund_key = None


async def mark_sent(db: AsyncSession, job: ProcurementJob) -> None:
    """The lease that carries the top-up is about to reach the adapter: from now on the row is
    never voided (its money may move), only settled."""
    if job.fund_key:
        await db.execute(
            update(ProcurementFunding)
            .where(
                ProcurementFunding.tenant_id == job.tenant_id,
                ProcurementFunding.idem_key == job.fund_key,
                ProcurementFunding.state == "planned",
            )
            .values(state="sent", updated_at=func.now())
        )


async def sent_card(db: AsyncSession, job: ProcurementJob) -> str | None:
    """The card of the job's current top-up when it reached the adapter without a clear answer
    (row ``sent``): only that top-up, under its own key, may go again."""
    if not job.fund_key:
        return None
    return await db.scalar(
        select(ProcurementFunding.card_id).where(
            ProcurementFunding.tenant_id == job.tenant_id,
            ProcurementFunding.idem_key == job.fund_key,
            ProcurementFunding.state == "sent",
        )
    )


def forget(job: ProcurementJob) -> None:
    """Stop carrying the job's top-up (its row keeps its state and stays counted)."""
    job.fund_amount = job.fund_key = None


async def void_planned(db: AsyncSession, job: ProcurementJob) -> None:
    """The job picks again (park / re-check): a plan that never reached the adapter is released;
    a ``sent`` one stays counted (its money may have moved) AND stays on the job, so only that
    top-up can ever go again (payment audit H1)."""
    if job.fund_key and await sent_card(db, job) is not None:
        return
    if job.fund_key:
        await db.execute(
            update(ProcurementFunding)
            .where(
                ProcurementFunding.tenant_id == job.tenant_id,
                ProcurementFunding.idem_key == job.fund_key,
                ProcurementFunding.state == "planned",
            )
            .values(state="void", updated_at=func.now())
        )
    job.fund_amount = job.fund_key = None
