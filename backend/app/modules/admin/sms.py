"""SMS verification codes, admin side (ADR-057).

* ``GET  /admin/sms/products/{id}``: configuration (defaults when none), HeroSMS suppliers to
  choose from, and the availability procurement last computed. Platform staff.
* ``PUT  /admin/sms/products/{id}``: save it. Platform administrators in a session; audited
  before / after. The product must use ``upstream_sms``.
* ``GET  /admin/sms/orders/{id}``: the order's session and numbers (masked; never a code).
* ``GET  /admin/sms/stats``: measured code rate per route (numbers, codes, median seconds, cost
  per code) and session outcomes, the "real success rate" the owner asked for.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.deps import client_ip, get_db
from app.core.errors import NotFound, Validation
from app.models import Order, Product, SmsAttempt, SmsProduct, SmsSession, SupplierAccount, User
from app.modules.admin.procurement_cards import platform_admin_session, platform_staff
from app.modules.orders import sms as sms_svc
from app.suppliers.herosms import params as P

router = APIRouter(prefix="/admin/sms", tags=["admin"])


class RouteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    country: int = Field(ge=0, le=9999)
    max_price: str = Field(max_length=12)


class SmsParamsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: str = Field("dr", min_length=1, max_length=16, pattern=r"^[a-z0-9_]+$")
    routes: list[RouteIn] = Field(min_length=1, max_length=P.MAX_ROUTES)
    max_attempts: int = 5
    tries_per_route: int = 2
    window_minutes: int = 30
    number_wait_s: int = 480
    min_swap_s: int = 120
    start_hours: int = 72
    daily_cap: int = 30
    per_buyer_daily: int = 3
    cool_no_numbers_min: int = 15
    cool_no_code_streak: int = 3
    cool_no_code_min: int = 30


class SmsConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_id: uuid.UUID | None = None
    enabled: bool = False
    params: SmsParamsIn


async def _product(db: AsyncSession, tid: uuid.UUID, pid: uuid.UUID) -> Product:
    p = await db.scalar(select(Product).where(Product.tenant_id == tid, Product.id == pid))
    if p is None:
        raise NotFound("Product not found")
    return p


async def _suppliers(db: AsyncSession, tid: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await db.scalars(
            select(SupplierAccount)
            .where(SupplierAccount.tenant_id == tid, SupplierAccount.adapter == "herosms")
            .order_by(SupplierAccount.priority, SupplierAccount.code)
        )
    ).all()
    return [
        {
            "id": str(s.id),
            "code": s.code,
            "env": s.env,
            "enabled": s.enabled,
            "health": s.health,
            "balance": str(s.last_balance) if s.last_balance is not None else None,
        }
        for s in rows
    ]


def _view(cfg: SmsProduct | None) -> dict[str, Any]:
    return {
        "configured": cfg is not None,
        "supplier_id": str(cfg.supplier_id) if cfg and cfg.supplier_id else None,
        "enabled": bool(cfg and cfg.enabled),
        "params": P.normalize(cfg.params if cfg else {}),
        "status": {
            "ok": cfg.last_check_ok if cfg else None,
            "reason": cfg.last_check_reason if cfg else None,
            "stock": cfg.stock_hint if cfg else 0,
            "checked_at": cfg.last_check_at.isoformat() if cfg and cfg.last_check_at else None,
        },
    }


@router.get("/products/{product_id}")
async def get_config(
    product_id: uuid.UUID,
    user: User = Depends(platform_staff),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    p = await _product(db, user.tenant_id, product_id)
    cfg = await db.scalar(
        select(SmsProduct).where(
            SmsProduct.tenant_id == user.tenant_id, SmsProduct.product_id == p.id
        )
    )
    return {
        **_view(cfg),
        "delivery_mode": p.delivery_mode,
        "suppliers": await _suppliers(db, user.tenant_id),
    }


@router.put("/products/{product_id}")
async def put_config(
    product_id: uuid.UUID,
    body: SmsConfigIn,
    request: Request,
    user: User = Depends(platform_admin_session),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    tid = user.tenant_id
    p = await _product(db, tid, product_id)
    if p.delivery_mode != "upstream_sms":
        raise Validation("Set the product's delivery mode to SMS code first", code="sms_wrong_mode")
    raw = body.params.model_dump()
    raw["routes"] = [r.model_dump() for r in body.params.routes]
    params = P.normalize(raw)
    # refuse what the normaliser had to change (an out-of-range value is a typo, not a wish)
    for key in P.INT_LIMITS:
        if params[key] != raw[key]:
            raise Validation(f"{key} is out of range", code="sms_bad_params", extra={"field": key})
    if len(params["routes"]) != len(raw["routes"]):
        raise Validation(
            "Each route needs a distinct country and a price cap of 0.01-5 USD",
            code="sms_bad_params",
            extra={"field": "routes"},
        )
    if body.supplier_id is not None:
        sup = await db.scalar(
            select(SupplierAccount).where(
                SupplierAccount.tenant_id == tid, SupplierAccount.id == body.supplier_id
            )
        )
        if sup is None or sup.adapter != "herosms":
            raise Validation("Choose a HeroSMS supplier", code="sms_bad_supplier")
    elif body.enabled:
        raise Validation("Choose a HeroSMS supplier", code="sms_bad_supplier")
    cfg = await db.scalar(
        select(SmsProduct)
        .where(SmsProduct.tenant_id == tid, SmsProduct.product_id == p.id)
        .with_for_update()
    )
    before = _view(cfg) if cfg else None
    if cfg is None:
        cfg = SmsProduct(
            id=uuid.uuid4(),
            tenant_id=tid,
            product_id=p.id,
            supplier_id=body.supplier_id,
            enabled=body.enabled,
            params=params,
            stock_hint=0,
        )
        db.add(cfg)
    else:
        cfg.supplier_id = body.supplier_id
        cfg.enabled = body.enabled
        cfg.params = params
        cfg.updated_at = datetime.now(timezone.utc)
    await db.flush()
    after = _view(cfg)
    await audit(
        db,
        tenant_id=tid,
        action="sms.product_config_updated",
        actor_id=user.id,
        actor_role=user.role,
        target=f"product:{p.id}",
        before={k: before[k] for k in ("supplier_id", "enabled", "params")} if before else None,
        after={k: after[k] for k in ("supplier_id", "enabled", "params")},
        ip=client_ip(request),
    )
    await db.commit()
    return {**after, "delivery_mode": p.delivery_mode, "suppliers": await _suppliers(db, tid)}


@router.get("/orders/{order_id}")
async def order_view(
    order_id: uuid.UUID,
    user: User = Depends(platform_staff),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    exists = await db.scalar(
        select(Order.id).where(Order.tenant_id == user.tenant_id, Order.id == order_id)
    )
    if exists is None:
        raise NotFound("Order not found")
    return await sms_svc.staff_view(db, user.tenant_id, order_id)


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


@router.get("/stats")
async def stats(
    days: int = Query(7, ge=1, le=90),
    user: User = Depends(platform_staff),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    tid = user.tenant_id
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        await db.execute(
            select(
                SmsAttempt.country,
                SmsAttempt.operator,
                SmsAttempt.state,
                SmsAttempt.cost,
                SmsAttempt.bought_at,
                SmsAttempt.code_at,
            ).where(SmsAttempt.tenant_id == tid, SmsAttempt.bought_at >= since)
        )
    ).all()
    routes: dict[int, dict[str, Any]] = {}
    for country, operator, state, cost, bought, code_at in rows:
        r = routes.setdefault(
            country,
            {
                "country": country,
                "numbers": 0,
                "codes": 0,
                "no_code": 0,
                "open": 0,
                "spent": Decimal(0),
                "secs": [],
                "operators": {},
            },
        )
        r["numbers"] += 1
        op = r["operators"].setdefault(operator or "-", {"numbers": 0, "codes": 0})
        op["numbers"] += 1
        if state == "code":
            r["codes"] += 1
            op["codes"] += 1
            r["spent"] += cost or Decimal(0)  # only numbers with a code are charged
            if code_at is not None:
                r["secs"].append((code_at - bought).total_seconds())
        elif state == "waiting":
            r["open"] += 1
        else:
            r["no_code"] += 1
    out_routes = []
    for r in sorted(routes.values(), key=lambda x: -x["numbers"]):
        closed = r["codes"] + r["no_code"]
        out_routes.append(
            {
                "country": r["country"],
                "numbers": r["numbers"],
                "codes": r["codes"],
                "no_code": r["no_code"],
                "open": r["open"],
                "code_rate": round(r["codes"] / closed, 3) if closed else None,
                "median_seconds": _median(r["secs"]),
                "spent_usd": str(r["spent"]),
                "cost_per_code_usd": str((r["spent"] / r["codes"]).quantize(Decimal("0.0001")))
                if r["codes"]
                else None,
                "operators": r["operators"],
            }
        )
    sess_rows = (
        await db.execute(
            select(SmsSession.state, SmsSession.fail_reason, func.count())
            .where(SmsSession.tenant_id == tid, SmsSession.created_at >= since)
            .group_by(SmsSession.state, SmsSession.fail_reason)
        )
    ).all()
    sessions: dict[str, Any] = {"ready": 0, "active": 0, "delivered": 0, "failed": 0, "reasons": {}}
    for state, reason, n in sess_rows:
        sessions[state] = sessions.get(state, 0) + int(n)
        if state == "failed":
            sessions["reasons"][reason or "unknown"] = int(n)
    done = sessions["delivered"] + sessions["failed"]
    sessions["success_rate"] = round(sessions["delivered"] / done, 3) if done else None
    return {"days": days, "routes": out_routes, "sessions": sessions}
