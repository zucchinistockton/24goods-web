"""Supplier health: probes (admin "Test connection") and the periodic balance refresh.

Database sessions are never held open across a supplier HTTP call: read the row, close,
call the supplier, then write the result in a fresh tenant-pinned transaction.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.core.audit import audit
from app.core.db import tenant_session
from app.models import SupplierAccount, Tenant
from app.procurement.context import ProcurementContext
from app.suppliers.base import Balance, ProbeReport, Supplier, SupplierError, clean_detail
from app.suppliers.registry import SupplierConfigError, build_supplier

log = logging.getLogger("app.procurement")

PLATFORM = "platform"


class SupplierNotFound(LookupError):
    pass


@dataclass(frozen=True)
class SupplierRef:
    id: uuid.UUID
    tenant_id: uuid.UUID
    code: str
    adapter: str
    env: str
    health: str


def make_supplier(ctx: ProcurementContext, ref: SupplierRef) -> Supplier:
    return build_supplier(
        code=ref.code,
        adapter=ref.adapter,
        env=ref.env,
        environ=ctx.environ,
        app_env=ctx.settings.env,
        allow_production_writes=ctx.settings.procurement_allow_production_writes,
        http=ctx.http,
    )


async def load_ref(
    ctx: ProcurementContext, tenant_id: uuid.UUID, supplier_id: uuid.UUID
) -> SupplierRef:
    """The supplier row of ``tenant_id``. Secrets in the env file belong to the platform, so a
    supplier of any other tenant is never probed with them (reported as not found)."""
    async with tenant_session(ctx.maker, tenant_id) as db:
        kind = await db.scalar(select(Tenant.kind).where(Tenant.id == tenant_id))
        if kind != PLATFORM:
            raise SupplierNotFound
        row = await db.scalar(
            select(SupplierAccount).where(
                SupplierAccount.tenant_id == tenant_id, SupplierAccount.id == supplier_id
            )
        )
        if row is None:
            raise SupplierNotFound
        return SupplierRef(row.id, row.tenant_id, row.code, row.adapter, row.env, row.health)


def _health_of(report: ProbeReport) -> tuple[str, str | None]:
    if report.ok:
        return "ok", None
    detail = report.errors[0][:64] if report.errors else "probe_failed"
    if report.balance is not None:  # authenticated, something secondary failed
        return "degraded", detail
    return "down", detail


async def record_health(
    ctx: ProcurementContext,
    ref: SupplierRef,
    health: str,
    detail: str | None,
    *,
    balance: Balance | None,
    probed: bool,
) -> None:
    now = datetime.now(timezone.utc)
    detail = clean_detail(detail) if detail is not None else None
    values: dict[str, object] = {"health": health, "health_detail": detail}
    if probed:
        values["last_probe_at"] = now
    if balance is not None:
        values["last_balance"] = balance.spendable
        values["last_balance_at"] = now
    async with tenant_session(ctx.maker, ref.tenant_id) as db:
        await db.execute(
            update(SupplierAccount)
            .where(SupplierAccount.tenant_id == ref.tenant_id, SupplierAccount.id == ref.id)
            .values(**values)
        )
        if health != ref.health:
            await audit(
                db,
                tenant_id=ref.tenant_id,
                action="supplier.health_changed",
                target=f"supplier:{ref.id}",
                before={"health": ref.health},
                after={"health": health, "detail": detail},
                severity="warning" if health == "down" else "info",
            )
        await db.commit()


async def probe(
    ctx: ProcurementContext, tenant_id: uuid.UUID, supplier_id: uuid.UUID
) -> ProbeReport:
    ref = await load_ref(ctx, tenant_id, supplier_id)
    report = await run_probe(ctx, ref)
    health, detail = _health_of(report)
    await record_health(ctx, ref, health, detail, balance=report.balance, probed=True)
    return report


async def run_probe(ctx: ProcurementContext, ref: SupplierRef) -> ProbeReport:
    now = datetime.now(timezone.utc)
    try:
        supplier = make_supplier(ctx, ref)
    except SupplierConfigError:
        log.warning("supplier %s (%s) is not configured in this process", ref.code, ref.env)
        return ProbeReport(ref.code, ref.env, ok=False, errors=("not_configured",), checked_at=now)
    try:
        return await asyncio.wait_for(supplier.probe(), ctx.settings.probe_timeout_s)
    except asyncio.TimeoutError:
        return ProbeReport(ref.code, ref.env, ok=False, errors=("probe_timeout",), checked_at=now)
    except SupplierError as exc:
        return ProbeReport(ref.code, ref.env, ok=False, errors=(exc.reason,), checked_at=now)
    finally:
        await supplier.aclose()


async def enabled_suppliers(ctx: ProcurementContext) -> list[SupplierRef]:
    """Enabled suppliers of platform tenants (``procurement_scan`` policy: SELECT only)."""
    async with tenant_session(ctx.maker, None) as db:
        rows = (
            await db.execute(
                select(SupplierAccount)
                .join(Tenant, Tenant.id == SupplierAccount.tenant_id)
                .where(SupplierAccount.enabled.is_(True), Tenant.kind == PLATFORM)
                .order_by(SupplierAccount.priority, SupplierAccount.created_at)
                .limit(100)
            )
        ).scalars()
        return [SupplierRef(r.id, r.tenant_id, r.code, r.adapter, r.env, r.health) for r in rows]


async def refresh_balance(ctx: ProcurementContext, ref: SupplierRef) -> Balance | None:
    """One balance refresh (GET only). Errors become health, never exceptions."""
    try:
        supplier = make_supplier(ctx, ref)
    except SupplierConfigError:
        await record_health(ctx, ref, "down", "not_configured", balance=None, probed=False)
        return None
    try:
        bal = await asyncio.wait_for(supplier.balance(), ctx.settings.probe_timeout_s)
    except asyncio.TimeoutError:
        await record_health(ctx, ref, "down", "timeout", balance=None, probed=False)
        return None
    except SupplierError as exc:
        health = "down" if exc.supplier_stop else "degraded"
        await record_health(ctx, ref, health, exc.reason, balance=None, probed=False)
        return None
    finally:
        await supplier.aclose()
    await record_health(ctx, ref, "ok", None, balance=bal, probed=False)
    return bal


async def refresh_all(ctx: ProcurementContext) -> int:
    refs = await enabled_suppliers(ctx)
    for ref in refs:
        try:
            await refresh_balance(ctx, ref)
        except Exception:  # one broken supplier must not stop the others
            log.exception("balance refresh failed for supplier %s", ref.id)
    return len(refs)


async def run_loop(ctx: ProcurementContext, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await refresh_all(ctx)
        except Exception:
            log.exception("supplier refresh pass failed")
        try:
            await asyncio.wait_for(stop.wait(), ctx.settings.balance_interval_s)
        except asyncio.TimeoutError:
            continue
