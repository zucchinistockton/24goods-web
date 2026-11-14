"""Supplier configuration (review-v12 §7, ADR-038). Session admins of the platform tenant only.

The rows hold no secrets: supplier credentials live in the procurement process environment.
"Test connection" asks the procurement process (internal network, ``APP_PROCUREMENT_TOKEN``)
to run a read-only probe, the way the API asks delivery to reveal codes; the API never sees a
supplier API secret. Every change is audited.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.deps import client_ip, get_db, get_res
from app.core.errors import (
    Conflict,
    Forbidden,
    InvalidState,
    NotFound,
    RateLimited,
    Unavailable,
    Validation,
)
from app.core.ratelimit import enforce
from app.core.resources import Resources
from app.models import SupplierAccount, Tenant, User
from app.modules.admin.payment_gateways import session_admin
from app.suppliers.base import clean_detail, supplier_code_problem

router = APIRouter(prefix="/admin/suppliers", tags=["admin"])

Adapter = Literal["zovocard", "mock", "manual"]
Env = Literal["sandbox", "production"]
AMOUNT_RE = r"^[0-9]{1,9}(\.[0-9]{1,2})?$"
DEV_ENVS = ("dev", "test")


class SupplierIn(BaseModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    adapter: Adapter
    label: str = Field(min_length=1, max_length=80)
    env: Env = "sandbox"
    priority: int = Field(default=100, ge=0, le=10000)
    min_spendable: str = Field(default="0", pattern=AMOUNT_RE)
    enabled: bool = False


class SupplierPatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=80)
    env: Env | None = None
    priority: int | None = Field(default=None, ge=0, le=10000)
    min_spendable: str | None = Field(default=None, pattern=AMOUNT_RE)
    enabled: bool | None = None


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def _dec(v: Decimal | None) -> str | None:
    """numeric(20,6) -> plain string without trailing zeros or exponent ("5", "6.27")."""
    return None if v is None else format(v.normalize(), "f")


def _view(r: SupplierAccount) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "code": r.code,
        "adapter": r.adapter,
        "label": r.label,
        "enabled": r.enabled,
        "env": r.env,
        "priority": r.priority,
        "min_spendable": _dec(r.min_spendable),
        "currency": r.currency,
        "health": r.health,
        "health_detail": r.health_detail,
        "last_probe_at": _iso(r.last_probe_at),
        "last_balance": _dec(r.last_balance),
        "last_balance_at": _iso(r.last_balance_at),
        "created_at": _iso(r.created_at),
        "updated_at": _iso(r.updated_at),
    }


def _snap(r: SupplierAccount) -> dict[str, Any]:
    return {
        "label": r.label,
        "env": r.env,
        "priority": r.priority,
        "min_spendable": _dec(r.min_spendable),
        "enabled": r.enabled,
    }


async def platform_admin(
    admin: User = Depends(session_admin), db: AsyncSession = Depends(get_db)
) -> User:
    """Supplier secrets belong to the platform deployment: reseller tenants never manage them."""
    kind = await db.scalar(select(Tenant.kind).where(Tenant.id == admin.tenant_id))
    if kind != "platform":
        raise Forbidden("Suppliers are managed by the platform")
    return admin


def _adapters(res: Resources) -> list[str]:
    base = ["zovocard", "manual"]
    return [*base, "mock"] if res.settings.env in DEV_ENVS else base


def _check_production(res: Resources, env: str, enabled: bool) -> None:
    if enabled and env == "production" and not res.settings.procurement_allow_production:
        raise InvalidState(
            "Production suppliers stay disabled until the security review",
            code="production_locked",
        )


async def _get(db: AsyncSession, tenant_id: uuid.UUID, supplier_id: uuid.UUID) -> SupplierAccount:
    row = await db.scalar(
        select(SupplierAccount).where(
            SupplierAccount.tenant_id == tenant_id, SupplierAccount.id == supplier_id
        )
    )
    if row is None:
        raise NotFound("Supplier not found")
    return row


@router.get("")
async def list_suppliers(
    admin: User = Depends(platform_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    rows = await db.scalars(
        select(SupplierAccount)
        .where(SupplierAccount.tenant_id == admin.tenant_id)
        .order_by(SupplierAccount.priority, SupplierAccount.created_at)
        .limit(100)
    )
    return {
        "items": [_view(r) for r in rows],
        "adapters": _adapters(res),
        "production_locked": not res.settings.procurement_allow_production,
    }


@router.post("", status_code=201)
async def create_supplier(
    body: SupplierIn,
    request: Request,
    admin: User = Depends(platform_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    await enforce(res.redis, "supplier:config", str(admin.id), 30, 3600)
    if body.adapter not in _adapters(res):
        raise Forbidden("This adapter is not available here")
    problem = supplier_code_problem(body.code, body.adapter)
    if problem is not None:
        raise Validation(
            "Use the adapter name or <adapter>_<suffix>; codes never end in _sandbox",
            code="supplier_code",
            extra={"reason": problem},
        )
    _check_production(res, body.env, body.enabled)
    row = SupplierAccount(
        id=uuid.uuid4(),
        tenant_id=admin.tenant_id,
        code=body.code,
        adapter=body.adapter,
        label=body.label.strip() or body.code,
        env=body.env,
        priority=body.priority,
        min_spendable=Decimal(body.min_spendable),
        enabled=body.enabled,
        settings={},
        created_by=admin.id,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise Conflict("A supplier with this code and environment exists", code="taken") from None
    await audit(
        db,
        tenant_id=admin.tenant_id,
        action="supplier.created",
        actor_id=admin.id,
        actor_role=admin.role,
        target=f"supplier:{row.id}",
        after={"code": row.code, "adapter": row.adapter, **_snap(row)},
        ip=client_ip(request),
    )
    await db.commit()
    await db.refresh(row)
    return _view(row)


@router.patch("/{supplier_id}")
async def update_supplier(
    supplier_id: uuid.UUID,
    body: SupplierPatch,
    request: Request,
    admin: User = Depends(platform_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    await enforce(res.redis, "supplier:config", str(admin.id), 30, 3600)
    row = await _get(db, admin.tenant_id, supplier_id)
    before = _snap(row)
    patch = body.model_dump(exclude_unset=True, exclude_none=True)
    env = patch.get("env", row.env)
    enabled = patch.get("enabled", row.enabled)
    _check_production(res, env, enabled)
    if "label" in patch:
        row.label = patch["label"].strip() or row.code
    if "env" in patch:
        row.env = patch["env"]
        row.health, row.health_detail = "unknown", None  # the other account is untested
    if "priority" in patch:
        row.priority = patch["priority"]
    if "min_spendable" in patch:
        row.min_spendable = Decimal(patch["min_spendable"])
    if "enabled" in patch:
        row.enabled = patch["enabled"]
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise Conflict("A supplier with this code and environment exists", code="taken") from None
    after = _snap(row)
    if after != before:
        await audit(
            db,
            tenant_id=admin.tenant_id,
            action="supplier.updated",
            actor_id=admin.id,
            actor_role=admin.role,
            target=f"supplier:{row.id}",
            before=before,
            after=after,
            ip=client_ip(request),
        )
    await db.commit()
    await db.refresh(row)
    return _view(row)


@router.post("/{supplier_id}/probe")
async def probe_supplier(
    supplier_id: uuid.UUID,
    request: Request,
    admin: User = Depends(platform_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    await enforce(res.redis, "supplier:probe", str(admin.id), 10, 600)
    row = await _get(db, admin.tenant_id, supplier_id)
    # plain values: the rollback below expires every ORM object of this session
    sid, env = row.id, row.env
    tid, uid, role = admin.tenant_id, admin.id, admin.role
    await db.rollback()  # no transaction held open across the procurement call
    token = res.settings.procurement_token
    if len(token) < 32:
        raise Unavailable("The procurement service is not configured", code="procurement_off")
    try:
        resp = await res.procurement.post(
            "/internal/probe",
            json={"tenant_id": str(tid), "supplier_id": str(sid)},
            headers={"X-Internal-Token": token},
        )
    except httpx.HTTPError:
        raise Unavailable(
            "The procurement service is unreachable", code="procurement_unreachable"
        ) from None
    if resp.status_code == 404:
        raise NotFound("Supplier not found")
    if resp.status_code == 429:
        raise RateLimited(retry_after=int(resp.headers.get("Retry-After", "600")))
    try:
        report = resp.json().get("report") if resp.status_code == 200 else None
    except (ValueError, AttributeError):
        report = None
    if not isinstance(report, dict):
        raise Unavailable("The procurement service failed", code="procurement_error")
    await audit(
        db,
        tenant_id=tid,
        action="supplier.probed",
        actor_id=uid,
        actor_role=role,
        target=f"supplier:{sid}",
        detail={
            "env": env,
            "ok": bool(report.get("ok")),
            "errors": [clean_detail(e, 80) for e in (report.get("errors") or [])][:10],
        },
        ip=client_ip(request),
    )
    await db.commit()
    fresh = await db.scalar(
        select(SupplierAccount)
        .where(SupplierAccount.tenant_id == tid, SupplierAccount.id == sid)
        .execution_options(populate_existing=True)
    )
    return {"report": report, "supplier": _view(fresh) if fresh is not None else None}
