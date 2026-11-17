"""Card pool, card rules, usage reset and the decline monitor (issue #29 part 2, review-v12 §21.8).

* ``GET /admin/procurement/cards/{suppliers,pool,schedule}``: staff, read only. The vendor is read
  by the procurement process (the API holds no vendor keys); our own counts come from
  ``procurement_jobs`` (last 30 days, tenant-filtered). No card number is ever returned: a card is
  an id, a last-four and a status.
* ``GET/PUT /admin/procurement/cards/rules``: administrators of the platform tenant. Ranges are
  validated here and again in the procurement process; every change writes a before / after audit
  row; a production supplier is refused unless ``APP_PROCUREMENT_ALLOW_PRODUCTION_WRITES`` is on.
* ``POST /admin/procurement/cards/reset-failed``: administrators, ``scope=failed`` only, explicit
  card ids only, ``confirm`` required, same production guard, audited.
* ``GET /admin/procurement/cards/risk``: staff. Decline rate of our direct orders over 7 and 30
  days against the cost model's chargeback thresholds.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import costmodel as cm
from app.core.audit import audit
from app.core.deps import client_ip, get_db, get_res, require_staff
from app.core.errors import Forbidden, InvalidState, NotFound, Unavailable, Validation
from app.core.ratelimit import enforce
from app.core.resources import Resources
from app.models import SupplierAccount, Tenant, User
from app.modules.admin.payment_gateways import session_admin
from app.procurement import risk

router = APIRouter(prefix="/admin/procurement/cards", tags=["admin"])

Product = Literal["gpt", "claude", "grok"]
EDITABLE = (
    "count_failures",
    "light_max_uses",
    "pro20_max_uses",
    "select_mode",
    "auto_switch_on_fail",
    "max_auto_switches",
    "strict_select",
)


async def platform_staff(
    request: Request, user: User = Depends(require_staff), db: AsyncSession = Depends(get_db)
) -> User:
    if getattr(request.state, "api_token", None) is not None:
        raise Forbidden("A staff session is required")
    if await db.scalar(select(Tenant.kind).where(Tenant.id == user.tenant_id)) != "platform":
        raise Forbidden("Suppliers are managed by the platform")
    return user


async def platform_admin_session(
    user: User = Depends(session_admin), db: AsyncSession = Depends(get_db)
) -> User:
    if await db.scalar(select(Tenant.kind).where(Tenant.id == user.tenant_id)) != "platform":
        raise Forbidden("Suppliers are managed by the platform")
    return user


async def _ask(res: Resources, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST to the procurement process; its errors become ours (never a raw vendor answer)."""
    token = res.settings.procurement_token
    if len(token) < 32:
        raise Unavailable("The procurement service is not configured", code="procurement_off")
    try:
        resp = await res.procurement.post(path, json=payload, headers={"X-Internal-Token": token})
    except httpx.HTTPError:
        raise Unavailable(
            "The procurement service is unreachable", code="supplier_unavailable"
        ) from None
    if resp.status_code == 200:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            return body
        raise Unavailable("Unreadable supplier answer", code="supplier_unavailable")
    if resp.status_code == 404:
        raise NotFound("No ZovoCard supplier")
    if resp.status_code == 409:
        raise InvalidState(
            "Writes to a production supplier are disabled", code="production_writes_disabled"
        )
    if resp.status_code == 422:
        raise Validation("The supplier request was refused", code="bad_request")
    if resp.status_code == 429:
        from app.core.errors import RateLimited

        raise RateLimited(retry_after=int(resp.headers.get("retry-after", "60") or 60))
    raise Unavailable("The supplier could not be read", code="supplier_unavailable")


def _sid(supplier_id: uuid.UUID | None) -> str | None:
    return str(supplier_id) if supplier_id else None


async def _cost_settings(db: AsyncSession, tid: uuid.UUID) -> dict[str, Any]:
    ts = await db.scalar(select(Tenant.settings).where(Tenant.id == tid))
    return cm.from_tenant(dict(ts or {}))


# ------------------------------------------------------------------------------------ reads
@router.get("/suppliers")
async def suppliers(
    staff: User = Depends(platform_staff), db: AsyncSession = Depends(get_db)
) -> dict:
    rows = await db.scalars(
        select(SupplierAccount)
        .where(
            SupplierAccount.tenant_id == staff.tenant_id,
            SupplierAccount.adapter == "zovocard",
            SupplierAccount.enabled.is_(True),
        )
        .order_by(SupplierAccount.priority, SupplierAccount.created_at)
    )
    return {
        "items": [{"id": str(r.id), "code": r.code, "label": r.label, "env": r.env} for r in rows]
    }


@router.get("/pool")
async def pool(
    staff: User = Depends(platform_staff),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
    product: Product = Query("gpt"),
    supplier_id: uuid.UUID | None = None,
) -> dict:
    await enforce(res.redis, "procurement:cards", str(staff.id), 30, 600)
    tid = staff.tenant_id
    await db.rollback()
    data = await _ask(
        res,
        "/internal/cards/pool",
        {"tenant_id": str(tid), "supplier_id": _sid(supplier_id), "product": product},
    )
    sup = data.get("supplier") if isinstance(data.get("supplier"), dict) else {}
    try:
        sup_id = uuid.UUID(str(sup.get("id")))
    except ValueError:
        sup_id = None
    cm_ = await _cost_settings(db, tid)
    th = risk.thresholds_of(cm_)
    total, per_card = await risk.counts(db, tid, 30, supplier_id=sup_id)
    cards = []
    for c in data.get("cards") or []:
        s, d, f = per_card.get(str(c.get("id")), (0, 0, 0))
        cards.append({**c, "ours_30d": risk.view(d, s, f, th)})
    return {
        **{k: v for k, v in data.items() if k != "cards"},
        "cards": cards,
        "ours_30d": risk.view(total[1], total[0], total[2], th),
    }


@router.get("/schedule")
async def schedule(
    staff: User = Depends(platform_staff),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
    product: Product = Query("gpt"),
    plan: str = Query(min_length=1, max_length=48, pattern=r"^[A-Za-z0-9_.-]+$"),
    supplier_id: uuid.UUID | None = None,
) -> dict:
    await enforce(res.redis, "procurement:cards-schedule", str(staff.id), 30, 600)
    tid = staff.tenant_id
    await db.rollback()
    return await _ask(
        res,
        "/internal/cards/schedule",
        {"tenant_id": str(tid), "supplier_id": _sid(supplier_id), "product": product, "plan": plan},
    )


@router.get("/risk")
async def risk_monitor(
    staff: User = Depends(platform_staff),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
    supplier_id: uuid.UUID | None = None,
) -> dict:
    await enforce(res.redis, "procurement:cards-risk", str(staff.id), 60, 600)
    th = risk.thresholds_of(await _cost_settings(db, staff.tenant_id))
    windows: dict[str, Any] = {}
    worst = "ok"
    order = {"ok": 0, "warning": 1, "critical": 2}
    for days in risk.WINDOWS:
        total, per_card = await risk.counts(db, staff.tenant_id, days, supplier_id=supplier_id)
        overall = risk.view(total[1], total[0], total[2], th)
        by_card = [
            {"card_id": cid, **risk.view(d, s, f, th)}
            for cid, (s, d, f) in sorted(per_card.items(), key=lambda kv: (-kv[1][0], kv[0]))
            if s or f
        ]
        for v in (overall, *by_card):
            if order[v["level"]] > order[worst]:
                worst = v["level"]
        windows[str(days)] = {"overall": overall, "cards": by_card}
    return {
        "thresholds": {
            "tier1": str(th.tier1),
            "tier2": str(th.tier2),
            "warn_at": str(risk.WARN_AT),
            "min_sample": risk.MIN_SAMPLE,
            "tier1_min_tx": th.tier1_min_tx,
        },
        "level": worst,
        "windows": windows,
        # no documented vendor field or endpoint exposes chargebacks (review-v12 §21.8)
        "chargebacks": {"available": False, "reason": "not_available_from_vendor_api"},
    }


# ------------------------------------------------------------------------------------ rules
class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_id: uuid.UUID | None = None
    count_failures: bool | None = None
    light_max_uses: int | None = Field(None, ge=0, le=1000)
    pro20_max_uses: int | None = Field(None, ge=0, le=1000)
    select_mode: Literal["default", "lowest_usage", "newest"] | None = None
    auto_switch_on_fail: bool | None = None
    max_auto_switches: int | None = Field(None, ge=0, le=10)
    strict_select: bool | None = None


def _editable(rule: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if rule is None else {k: rule.get(k) for k in EDITABLE}


@router.get("/rules")
async def get_rules(
    admin: User = Depends(platform_admin_session),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
    supplier_id: uuid.UUID | None = None,
) -> dict:
    await enforce(res.redis, "procurement:cards-rules", str(admin.id), 60, 600)
    tid = admin.tenant_id
    cost_switches = int((await _cost_settings(db, tid))["max_auto_switches"])
    await db.rollback()
    data = await _ask(
        res, "/internal/cards/rules", {"tenant_id": str(tid), "supplier_id": _sid(supplier_id)}
    )
    rules = data.get("rules") if isinstance(data.get("rules"), dict) else {}
    out = {}
    for p, r in rules.items():
        vs = r.get("max_auto_switches")
        # the vendor switches only when auto_switch_on_fail is on; the cost model has its own max
        eff = vs if r.get("auto_switch_on_fail") is not False else 0
        out[p] = {**r, "matches_cost_model": eff == cost_switches if eff is not None else None}
    return {
        "supplier": data.get("supplier"),
        "rules": out,
        "cost_model_max_auto_switches": cost_switches,
    }


@router.put("/rules/{product}")
async def put_rule(
    product: Product,
    body: RuleIn,
    request: Request,
    admin: User = Depends(platform_admin_session),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    await enforce(res.redis, "procurement:cards-write", str(admin.id), 30, 3600)
    patch = body.model_dump(exclude_none=True, exclude={"supplier_id"})
    if not patch:
        raise Validation("Nothing to change", code="nothing_to_change")
    tid, uid, role = admin.tenant_id, admin.id, admin.role
    await db.rollback()
    data = await _ask(
        res,
        "/internal/cards/rules-set",
        {"tenant_id": str(tid), "supplier_id": _sid(body.supplier_id), "product": product,
         "patch": patch},
    )  # fmt: skip
    before, after = _editable(data.get("before")), _editable(data.get("after"))
    await audit(
        db,
        tenant_id=tid,
        action="procurement.card_rules_updated",
        actor_id=uid,
        actor_role=role,
        target=f"card_rules:{product}",
        before=before,
        after=after,
        ip=client_ip(request),
        detail={
            "product": product,
            "supplier_id": data.get("supplier"),
            "env": data.get("env"),
            "fields": sorted(patch),
        },
        severity="warning",
    )
    await db.commit()
    return {"product": product, "before": before, "after": after}


class ResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_id: uuid.UUID | None = None
    card_ids: list[int] = Field(min_length=1, max_length=50)
    confirm: Literal[True]  # the console asks first; the API refuses a request without it


@router.post("/reset-failed")
async def reset_failed(
    body: ResetIn,
    request: Request,
    admin: User = Depends(platform_admin_session),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    if any(c < 1 or c >= 10**12 for c in body.card_ids):
        raise Validation("card_ids must be vendor card ids", code="bad_card_id")
    await enforce(res.redis, "procurement:cards-write", str(admin.id), 30, 3600)
    tid, uid, role = admin.tenant_id, admin.id, admin.role
    await db.rollback()
    data = await _ask(
        res,
        "/internal/cards/reset-failed",
        {"tenant_id": str(tid), "supplier_id": _sid(body.supplier_id),
         "card_ids": sorted(set(body.card_ids))},
    )  # fmt: skip
    await audit(
        db,
        tenant_id=tid,
        action="procurement.card_usage_reset",
        actor_id=uid,
        actor_role=role,
        target="card_usage:failed",
        ip=client_ip(request),
        detail={
            "scope": "failed",
            "card_ids": data.get("card_ids"),
            "reset": data.get("reset"),
            "supplier_id": data.get("supplier"),
            "env": data.get("env"),
            "at": datetime.now(timezone.utc).isoformat(),
        },
        severity="warning",
    )
    await db.commit()
    return {"scope": "failed", "card_ids": data.get("card_ids"), "reset": data.get("reset")}
