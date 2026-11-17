"""Owner settings of the procurement features of issue #27 (review-v12 §20), live regions.

* ``GET/PUT /admin/procurement/options``: the region allowlist and the half-price check method
  (``tenants.settings['procurement_options']``), audited.
* ``GET /admin/procurement/regions?product=gpt``: the vendor's CURRENT ``payment_regions`` (read by
  the procurement process: the API holds no supplier secrets), each marked ``allowed`` by the
  allowlist. A vendor that cannot be read gives ``available: false`` and no regions.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.deps import client_ip, get_db, get_res
from app.core.errors import NotFound, Unavailable
from app.core.ratelimit import enforce
from app.core.resources import Resources
from app.models import Tenant, User
from app.modules.admin.suppliers import platform_admin
from app.modules.catalog import regions as rg

router = APIRouter(prefix="/admin/procurement", tags=["admin"])


class OptionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region_allowlist: list[str] | None = Field(None, max_length=30)
    half_price_check: Literal["vendor_preflight", "chatgpt_direct", "manual"] | None = None


async def vendor_regions(
    res: Resources, tenant_id: uuid.UUID, product: str
) -> list[dict[str, str]] | None:
    """Ask the procurement process for the vendor's live regions; ``None`` = not readable now."""
    token = res.settings.procurement_token
    if len(token) < 32:
        raise Unavailable("The procurement service is not configured", code="procurement_off")
    try:
        resp = await res.procurement.post(
            "/internal/regions",
            json={"tenant_id": str(tenant_id), "product": product},
            headers={"X-Internal-Token": token},
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        rows = resp.json().get("regions")
    except (ValueError, AttributeError):
        return None
    if not isinstance(rows, list):
        return None
    return [
        {"country": r["country"], "currency": r["currency"]}
        for r in rows
        if isinstance(r, dict)
        and isinstance(r.get("country"), str)
        and isinstance(r.get("currency"), str)
    ]


async def _settings(db: AsyncSession, tid: uuid.UUID) -> dict[str, Any]:
    return dict(await db.scalar(select(Tenant.settings).where(Tenant.id == tid)) or {})


@router.get("/options")
async def get_options(
    admin: User = Depends(platform_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    cur = rg.settings_of(await _settings(db, admin.tenant_id))
    return {**cur, "half_methods": list(rg.HALF_METHODS)}


@router.put("/options")
async def put_options(
    body: OptionsIn,
    request: Request,
    admin: User = Depends(platform_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    await enforce(res.redis, "procurement:options", str(admin.id), 60, 3600)
    t = await db.scalar(select(Tenant).where(Tenant.id == admin.tenant_id).with_for_update())
    if t is None:
        raise NotFound("Tenant not found")
    ts = dict(t.settings or {})
    before = rg.settings_of(ts)
    patch = body.model_dump(exclude_none=True)
    new = rg.settings_of({rg.SETTINGS_KEY: {**before, **patch}})
    if new != before:
        t.settings = {**ts, rg.SETTINGS_KEY: new}
        await audit(
            db,
            tenant_id=admin.tenant_id,
            action="procurement.options_updated",
            actor_id=admin.id,
            actor_role=admin.role,
            target="tenant:procurement_options",
            before=before,
            after=new,
            ip=client_ip(request),
        )
        await db.commit()
    return {**new, "half_methods": list(rg.HALF_METHODS)}


@router.get("/regions")
async def regions(
    admin: User = Depends(platform_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
    product: Literal["gpt", "claude", "grok"] = Query("gpt"),
) -> dict:
    await enforce(res.redis, "procurement:regions", str(admin.id), 60, 600)
    allow = set(rg.settings_of(await _settings(db, admin.tenant_id))["region_allowlist"])
    tid = admin.tenant_id
    await db.rollback()
    rows = await vendor_regions(res, tid, product)
    if rows is None:
        return {"product": product, "available": False, "regions": []}
    return {
        "product": product,
        "available": True,
        "regions": [{**r, "allowed": r["country"] in allow} for r in rows],
    }
