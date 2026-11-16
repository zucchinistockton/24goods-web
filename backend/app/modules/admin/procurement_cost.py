"""Procurement cost report (review-v12 §7): the supplier ledger by day / supplier / type,
margin per order, rows that match no job, and a CSV export. Admins only; every query filters by
the caller's tenant. Amounts are ``Decimal`` strings next to their currency; amounts in different
currencies are never added together (a margin is only computed when revenue and cost share one).
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import Date, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.deps import Page, client_ip, get_db, get_page, get_res, require_admin
from app.core.errors import Validation
from app.core.money import to_decimal
from app.core.ratelimit import enforce
from app.core.resources import Resources
from app.models import Order, ProcurementJob, SupplierAccount, SupplierLedgerEntry, User
from app.modules.admin.service import CSV_MAX_ROWS, _csv_cell
from app.modules.admin.suppliers import _dec, _iso

router = APIRouter(prefix="/admin/procurement/cost", tags=["admin"])

MAX_RANGE_DAYS = 366
DEFAULT_RANGE_DAYS = 30
L = SupplierLedgerEntry
J = ProcurementJob


def _range(date_from: date | None, date_to: date | None) -> tuple[datetime, datetime, date, date]:
    """[start, end) in UTC for inclusive dates; defaults to the last 30 days."""
    today = datetime.now(timezone.utc).date()
    end_d = date_to or today
    start_d = date_from or end_d - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    if start_d > end_d:
        raise Validation("date_from must not be after date_to", code="bad_range")
    if (end_d - start_d).days >= MAX_RANGE_DAYS:
        raise Validation("The range is too long", code="bad_range")
    start = datetime.combine(start_d, time.min, tzinfo=timezone.utc)
    end = datetime.combine(end_d + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return start, end, start_d, end_d


def _money(v: Decimal | None) -> str:
    return _dec(v or Decimal(0)) or "0"


@router.get("/summary")
async def summary(
    group: Literal["day", "supplier", "type"] = "day",
    date_from: date | None = None,
    date_to: date | None = None,
    supplier_id: uuid.UUID | None = None,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Ledger totals per group and currency: debit = money spent (negative rows), credit =
    money in (top-ups, refunds), net = credit - debit."""
    start, end, start_d, end_d = _range(date_from, date_to)
    where = [L.tenant_id == admin.tenant_id, L.occurred_at >= start, L.occurred_at < end]
    if supplier_id is not None:
        where.append(L.supplier_id == supplier_id)
    debit = func.coalesce(func.sum(case((L.amount < 0, -L.amount), else_=0)), 0)
    credit = func.coalesce(func.sum(case((L.amount > 0, L.amount), else_=0)), 0)
    if group == "day":
        key = cast(func.timezone("UTC", L.occurred_at), Date)
        stmt = select(key.label("k"), L.currency, func.count(), debit, credit)
        stmt = stmt.where(*where).group_by(key, L.currency).order_by(key.desc(), L.currency)
    elif group == "type":
        stmt = select(L.type.label("k"), L.currency, func.count(), debit, credit)
        stmt = stmt.where(*where).group_by(L.type, L.currency).order_by(L.type, L.currency)
    else:
        stmt = (
            select(L.supplier_id.label("k"), L.currency, func.count(), debit, credit)
            .where(*where)
            .group_by(L.supplier_id, L.currency)
            .order_by(L.supplier_id, L.currency)
        )
    rows = (await db.execute(stmt.limit(1000))).all()
    labels: dict[uuid.UUID, tuple[str, str]] = {}
    if group == "supplier":
        for sid, code, env in (
            await db.execute(
                select(SupplierAccount.id, SupplierAccount.code, SupplierAccount.env).where(
                    SupplierAccount.tenant_id == admin.tenant_id
                )
            )
        ).all():
            labels[sid] = (code, env)
    items: list[dict[str, Any]] = []
    for k, cur, n, d, c in rows:
        item: dict[str, Any] = {
            "key": k.isoformat() if isinstance(k, date) else str(k),
            "currency": cur,
            "count": int(n),
            "debit": _money(d),
            "credit": _money(c),
            "net": _money(Decimal(c) - Decimal(d)),
        }
        if group == "supplier":
            code, env = labels.get(k, ("?", "?"))
            item.update(supplier_id=str(k), supplier_code=code, supplier_env=env)
        items.append(item)
    return {
        "group": group,
        "date_from": start_d.isoformat(),
        "date_to": end_d.isoformat(),
        "items": items,
    }


@router.get("/margin")
async def margin(
    date_from: date | None = None,
    date_to: date | None = None,
    admin: User = Depends(require_admin),
    page: Page = Depends(get_page),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Per order with procurement jobs (by order creation date): revenue, supplier cost and the
    margin. Cost is the sum of the ledger rows linked to the order's jobs; jobs that have none
    yet fall back to the cost the supplier reported at issue time (``source: job``)."""
    start, end, start_d, end_d = _range(date_from, date_to)
    tid = admin.tenant_id
    has_jobs = (
        select(ProcurementJob.order_id)
        .where(ProcurementJob.tenant_id == tid, ProcurementJob.state != "voided")
        .distinct()
    )
    # a refunded order earned nothing: it is not part of the margin (its cost stays in the ledger)
    base = [
        Order.tenant_id == tid,
        Order.id.in_(has_jobs),
        Order.status != "refunded",
        Order.created_at.between(start, end),
    ]
    total = await db.scalar(select(func.count(Order.id)).where(*base)) or 0
    orders = (
        await db.execute(
            select(Order.id, Order.order_no, Order.status, Order.amount_minor, Order.currency,
                   Order.paid_at)
            .where(*base)
            .order_by(Order.created_at.desc(), Order.id)
            .limit(page.page_size)
            .offset(page.offset)
        )
    ).all()  # fmt: skip
    ids = [o.id for o in orders]
    ledger: dict[uuid.UUID, dict[str, Decimal]] = {}
    jobs_cost: dict[uuid.UUID, dict[str, Decimal]] = {}
    job_counts: dict[uuid.UUID, list[int]] = {}
    if ids:
        for oid, cur, amt in (
            await db.execute(
                select(ProcurementJob.order_id, L.currency, func.sum(L.amount))
                .join(L, L.job_id == ProcurementJob.id)
                .where(
                    ProcurementJob.tenant_id == tid, L.tenant_id == tid,
                    ProcurementJob.order_id.in_(ids),
                )
                .group_by(ProcurementJob.order_id, L.currency)
            )
        ).all():  # fmt: skip
            ledger.setdefault(oid, {})[cur] = -Decimal(amt)
        has_ledger = select(L.job_id).where(L.tenant_id == tid, L.job_id.is_not(None))
        for oid, cur, amt in (
            await db.execute(
                select(J.order_id, J.currency, func.sum(J.cost))
                .where(
                    J.tenant_id == tid,
                    J.order_id.in_(ids),
                    J.cost.is_not(None),
                    J.currency.is_not(None),
                    J.id.not_in(has_ledger),
                )
                .group_by(J.order_id, J.currency)
            )
        ).all():  # fmt: skip
            jobs_cost.setdefault(oid, {})[cur] = Decimal(amt)
        for oid, state, n, unmatched in (
            await db.execute(
                select(
                    ProcurementJob.order_id, ProcurementJob.state, func.count(),
                    func.count(case((ProcurementJob.id.not_in(has_ledger), 1))),
                )
                .where(ProcurementJob.tenant_id == tid, ProcurementJob.order_id.in_(ids),
                       ProcurementJob.state != "voided")
                .group_by(ProcurementJob.order_id, ProcurementJob.state)
            )
        ).all():  # fmt: skip
            c = job_counts.setdefault(oid, [0, 0])
            c[0] += int(n)
            if state == "fulfilled":
                c[1] += int(unmatched)
    incomplete: set[uuid.UUID] = set()
    if ids:
        # a direct order whose subscription price is not in the balance currency: the wallet only
        # saw the service fee, so the ledger is NOT the whole cost (M6)
        incomplete = set(
            await db.scalars(
                select(J.order_id).where(
                    J.tenant_id == tid,
                    J.order_id.in_(ids),
                    J.kind == "direct",
                    J.state.in_(("fulfilled", "needs_manual")),
                    J.upstream_ref.is_not(None),
                    J.cost.is_(None),
                )
            )
        )
    items = []
    for o in orders:
        costs: dict[str, tuple[Decimal, str]] = {}
        for cur, amt in (jobs_cost.get(o.id) or {}).items():
            costs[cur] = (amt, "job")
        for cur, amt in (ledger.get(o.id) or {}).items():
            prev = costs.get(cur, (Decimal(0), "ledger"))
            costs[cur] = (amt + (prev[0] if prev[1] == "job" else Decimal(0)), "ledger")
        revenue = to_decimal(o.amount_minor)
        margin_v = None
        if len(costs) == 1 and o.currency in costs:
            margin_v = {"amount": _money(revenue - costs[o.currency][0]), "currency": o.currency}
        elif not costs:
            margin_v = None
        if o.id in incomplete:
            margin_v = None  # never a margin computed from the service fee alone
        n_jobs, n_unmatched = job_counts.get(o.id, [0, 0])
        items.append(
            {
                "order_id": str(o.id),
                "order_no": o.order_no,
                "status": o.status,
                "paid_at": _iso(o.paid_at),
                "revenue": _money(revenue),
                "revenue_currency": o.currency,
                "jobs": n_jobs,
                "unreconciled_jobs": n_unmatched,
                "cost": [
                    {"currency": cur, "amount": _money(amt), "source": src}
                    for cur, (amt, src) in sorted(costs.items())
                ],
                "margin": margin_v,
                "cost_incomplete": o.id in incomplete,
            }
        )
    out = page.wrap(items, int(total))
    out.update(date_from=start_d.isoformat(), date_to=end_d.isoformat())
    return out


def _ledger_row(r: Any, sup: dict[uuid.UUID, str]) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "supplier_id": str(r.supplier_id),
        "supplier_code": sup.get(r.supplier_id),
        "external_id": r.external_id,
        "type": r.type,
        "amount": _money(r.amount),
        "currency": r.currency,
        "balance_after": _dec(r.balance_after),
        "card_mask": r.card_mask,
        "ref_id": r.ref_id,
        "job_id": str(r.job_id) if r.job_id else None,
        "occurred_at": _iso(r.occurred_at),
    }


async def _suppliers(db: AsyncSession, tid: uuid.UUID) -> dict[uuid.UUID, str]:
    rows = await db.execute(
        select(SupplierAccount.id, SupplierAccount.code, SupplierAccount.env).where(
            SupplierAccount.tenant_id == tid
        )
    )
    return {i: f"{c}/{e}" for i, c, e in rows.all()}


@router.get("/unreconciled")
async def unreconciled(
    admin: User = Depends(require_admin),
    page: Page = Depends(get_page),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Ledger debits that match no procurement job (a card operation, a manual top-up, a CDK
    whose reference the job does not know yet). Staff reconcile them by hand."""
    where = [L.tenant_id == admin.tenant_id, L.job_id.is_(None), L.amount < 0]
    total = await db.scalar(select(func.count(L.id)).where(*where)) or 0
    rows = (
        await db.scalars(
            select(L)
            .where(*where)
            .order_by(L.occurred_at.desc(), L.id)
            .limit(page.page_size)
            .offset(page.offset)
        )
    ).all()
    sup = await _suppliers(db, admin.tenant_id)
    return page.wrap([_ledger_row(r, sup) for r in rows], int(total))


@router.get("/export.csv")
async def export_csv(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> Response:
    await enforce(res.redis, "procurement:export", str(admin.id), 10, 600)
    start, end, _s, _e = _range(date_from, date_to)
    tid = admin.tenant_id
    rows = (
        await db.scalars(
            select(L)
            .where(L.tenant_id == tid, L.occurred_at >= start, L.occurred_at < end)
            .order_by(L.occurred_at.desc(), L.id)
            .limit(CSV_MAX_ROWS)
        )
    ).all()
    sup = await _suppliers(db, tid)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["occurred_at", "supplier", "external_id", "type", "amount", "currency", "balance_after",
         "card", "ref_id", "job_id"]
    )  # fmt: skip
    for r in rows:
        # amounts are Decimal strings (a leading "-" is a number, not a formula); every text
        # cell the vendor controls goes through the formula-injection guard
        w.writerow(
            [
                _iso(r.occurred_at) or "",
                _csv_cell(sup.get(r.supplier_id, "")),
                _csv_cell(r.external_id),
                _csv_cell(r.type),
                _money(r.amount),
                _csv_cell(r.currency),
                _dec(r.balance_after) or "",
                _csv_cell(r.card_mask or ""),
                _csv_cell(r.ref_id or ""),
                str(r.job_id) if r.job_id else "",
            ]
        )
    await audit(
        db,
        tenant_id=tid,
        action="procurement.cost_exported",
        actor_id=admin.id,
        actor_role=admin.role,
        target="supplier_ledger",
        detail={"rows": len(rows)},
        ip=client_ip(request),
    )
    await db.commit()
    return Response(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="procurement-ledger.csv"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
