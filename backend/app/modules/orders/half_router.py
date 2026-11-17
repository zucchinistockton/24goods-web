"""``/half-checks``: the buyer's half-price eligibility check before payment (issue #27)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import client_ip, current_user, get_db, get_res
from app.core.ratelimit import enforce
from app.core.resources import Resources
from app.models import User
from app.modules.catalog.service import get_active_product
from app.modules.orders import halfcheck
from app.modules.orders.schemas import HalfCheckIn

router = APIRouter(prefix="/half-checks", tags=["orders"])
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


@router.get("/required")
async def required(
    product_id: uuid.UUID,
    request: Request,
    variant_id: uuid.UUID | None = Query(None),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    """Does this product / variant need the check, and by which method (the buyer is told whether
    to paste a session)."""
    await enforce(res.redis, "halfcheck:required", str(user.id), 60, 60)
    await enforce(res.redis, "halfcheck:required-ip", client_ip(request) or "-", 120, 60)
    await get_active_product(db, product_id)  # 404 for an inactive / unknown product
    target = await halfcheck.requirement(db, user.tenant_id, product_id, variant_id)
    if target is None:
        return {"required": False, "method": None, "plan": None}
    return {
        "required": True,
        "method": await halfcheck._method(db, user.tenant_id),
        "plan": target.plan,
    }


@router.post("")
async def create(
    body: HalfCheckIn,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> JSONResponse:
    await get_active_product(db, body.product_id)
    await enforce(res.redis, "halfcheck:user", str(user.id), 6, 600)
    await enforce(res.redis, "halfcheck:ip", client_ip(request) or "-", 30, 600)
    await enforce(res.redis, "halfcheck:ip-day", client_ip(request) or "-", 60, 86400)
    out = await halfcheck.create_check(
        db,
        res,
        user,
        client_ip(request),
        product_id=body.product_id,
        variant_id=body.variant_id,
        session=body.session,
    )
    return JSONResponse(out, headers=NO_STORE)
