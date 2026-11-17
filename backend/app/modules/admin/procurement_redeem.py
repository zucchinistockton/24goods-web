"""Redemption admin of the staff console (issue #31, review-v12 §23).

* ``GET /admin/procurement/redeem/attempts``: staff. The proxy's attempt log, searchable by masked
  code, outcome, step, date and order reference; paginated. A row has a masked code, a coarse
  reason and a keyed IP-class hash: never a code, token, session or address.
* ``GET /admin/procurement/redeem/stats``: staff. Per-day attempts / completed / failed, the
  success rate and the top failure reasons of the last N days (Decimal maths, UTC days).
* ``POST /admin/procurement/redeem/lookup``: staff. A pasted code (or an order reference): our side
  (order, masked buyer, delivery time, recorded attempts) plus the vendor's status, read by the
  procurement process (the API holds no vendor keys). The code is hashed in memory for matching
  (``CardSealer.fingerprint``, the scheme of delivered codes), sent once to procurement and never
  stored, logged or audited.
* ``POST /admin/procurement/redeem/cdk/{disable|enable}``: administrators, session only, ``confirm``
  required, production-write guard in procurement, before / after audit row (statuses and a masked
  code only).
* ``GET/PUT /admin/procurement/redeem/settings``: administrators. Maintenance switch, announcement
  text (en + zh, plain text, capped) and per-IP limits within the vendor-safe bounds; stored in
  ``tenants.redeem_settings`` (the proxy reads it), audited with before / after.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator
from sqlalchemy import Date, and_, cast, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.deps import Page, client_ip, get_db, get_page, get_res
from app.core.errors import InvalidState, NotFound, RateLimited, Unavailable, Validation
from app.core.notify_tg import mask_email
from app.core.ratelimit import enforce
from app.core.resources import Resources
from app.models import CardStock, Order, RedeemAttempt, Tenant, User
from app.modules.admin.procurement_cards import platform_admin_session, platform_staff
from app.modules.catalog.service import escape_like
from app.procurement import redeem_log as rl
from app.procurement.redeem_admin import CODE_RE

router = APIRouter(prefix="/admin/procurement/redeem", tags=["admin"])

Outcome = Literal["ok", "completed", "failed", "error", "rate_limited", "invalid", "maintenance"]
Step = Literal["preview", "preflight", "redeem", "result"]


# ------------------------------------------------------------------------------------ helpers
async def _cdk_call(
    res: Resources, path: str, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """POST to the procurement process: ``(status, body)``; ``(0, {})`` when it cannot be reached.
    The pasted code only travels inside ``payload``, in memory."""
    token = res.settings.procurement_token
    if len(token) < 32:
        return 0, {}
    try:
        resp = await res.procurement.post(path, json=payload, headers={"X-Internal-Token": token})
    except httpx.HTTPError:
        return 0, {}
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body if isinstance(body, dict) else {}


def _err_code(body: dict[str, Any]) -> str:
    err = body.get("error")
    return str(err.get("code", "")) if isinstance(err, dict) else ""


def _valid_code(code: str) -> str:
    code = code.strip()
    if not CODE_RE.fullmatch(code):
        raise Validation("That does not look like a redemption code", code="bad_code")
    return code


def _fp(res: Resources, tid: uuid.UUID, code: str) -> str:
    return res.sealer.fingerprint(str(tid), code)


def _row(a: RedeemAttempt, order_no: str | None) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "at": a.at.isoformat(),
        "step": a.step,
        "outcome": a.outcome,
        "reason": a.reason,
        "code": rl.display_mask(a.code_head, a.code_tail) if a.code_head else None,
        "plan": a.plan,
        "order_no": order_no,
        "ip_class": a.ip_hash[:10] if a.ip_hash else None,
    }


async def _order_map(db: AsyncSession, tid: uuid.UUID, fps: set[str]) -> dict[str, str]:
    """code fingerprint -> order number, for codes we sold (tenant-filtered both ways)."""
    if not fps:
        return {}
    rows = (
        await db.execute(
            select(CardStock.fingerprint, Order.order_no)
            .join(Order, Order.id == CardStock.order_id)
            .where(
                CardStock.tenant_id == tid,
                Order.tenant_id == tid,
                CardStock.fingerprint.in_(fps),
            )
        )
    ).all()
    return {r.fingerprint: r.order_no for r in rows}


# ------------------------------------------------------------------------------------ log
@router.get("/attempts")
async def attempts(
    staff: Any = Depends(platform_staff),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
    page: Page = Depends(get_page),
    q: str | None = Query(None, max_length=40),
    outcome: Outcome | None = None,
    step: Step | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    order_ref: str | None = Query(None, max_length=16),
) -> dict:
    await enforce(res.redis, "procurement:redeem-log", str(staff.id), 120, 600)
    tid = staff.tenant_id
    where = [RedeemAttempt.tenant_id == tid]
    if outcome:
        where.append(RedeemAttempt.outcome == outcome)
    if step:
        where.append(RedeemAttempt.step == step)
    if date_from:
        where.append(RedeemAttempt.at >= datetime.combine(date_from, time.min, timezone.utc))
    if date_to:
        where.append(
            RedeemAttempt.at < datetime.combine(date_to + timedelta(days=1), time.min, timezone.utc)
        )
    if q and q.strip():
        for sep in ("…", "...", "*", " "):
            if sep in q:
                head, _, tail = q.strip().partition(sep)
                break
        else:
            head, tail = q.strip(), None
        if tail is None:  # one token: the start or the end of the code
            like = escape_like(head)
            where.append(
                or_(
                    RedeemAttempt.code_head.like(f"{like}%", escape="\\"),
                    RedeemAttempt.code_tail == head,
                )
            )
        else:
            if head:
                where.append(RedeemAttempt.code_head == head)
            if tail.strip():
                where.append(RedeemAttempt.code_tail == tail.strip())
    if order_ref and order_ref.strip():
        oid = await db.scalar(
            select(Order.id).where(Order.tenant_id == tid, Order.order_no == order_ref.strip())
        )
        fps = (
            list(
                await db.scalars(
                    select(CardStock.fingerprint).where(
                        CardStock.tenant_id == tid, CardStock.order_id == oid
                    )
                )
            )
            if oid
            else []
        )
        where.append(RedeemAttempt.code_fp.in_(fps) if fps else RedeemAttempt.id.is_(None))
    total = await db.scalar(select(func.count()).select_from(RedeemAttempt).where(*where)) or 0
    rows = list(
        await db.scalars(
            select(RedeemAttempt)
            .where(*where)
            .order_by(RedeemAttempt.at.desc(), RedeemAttempt.id)
            .offset(page.offset)
            .limit(page.page_size)
        )
    )
    orders = await _order_map(db, tid, {r.code_fp for r in rows if r.code_fp})
    return page.wrap([_row(r, orders.get(r.code_fp or "")) for r in rows], total) | {
        "retention_days": rl.RETENTION_DAYS
    }


@router.get("/stats")
async def stats(
    staff: Any = Depends(platform_staff),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
    days: int = Query(30, ge=1, le=90),
) -> dict:
    await enforce(res.redis, "procurement:redeem-stats", str(staff.id), 60, 600)
    tid = staff.tenant_id
    today = datetime.now(timezone.utc).date()
    since = datetime.combine(today - timedelta(days=days - 1), time.min, timezone.utc)
    day = cast(func.timezone("UTC", RedeemAttempt.at), Date)
    failed = or_(
        RedeemAttempt.outcome == "failed",
        and_(RedeemAttempt.step == "redeem", RedeemAttempt.outcome == "error"),
    )
    by_day = (
        await db.execute(
            select(
                day,
                func.count().filter(RedeemAttempt.step == "preview"),
                func.count().filter(RedeemAttempt.outcome == "completed"),
                func.count().filter(failed),
            )
            .where(RedeemAttempt.tenant_id == tid, RedeemAttempt.at >= since)
            .group_by(day)
        )
    ).all()
    reasons = (
        await db.execute(
            select(RedeemAttempt.reason, func.count())
            .where(
                RedeemAttempt.tenant_id == tid,
                RedeemAttempt.at >= since,
                RedeemAttempt.reason.is_not(None),
                RedeemAttempt.outcome.in_(("failed", "error")),
            )
            .group_by(RedeemAttempt.reason)
        )
    ).all()
    return rl.build_stats(
        [(r[0], int(r[1]), int(r[2]), int(r[3])) for r in by_day],
        [(str(r[0]), int(r[1])) for r in reasons],
        days,
        today,
    )


# ------------------------------------------------------------------------------------ lookup
class LookupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str | None = Field(None, max_length=128, repr=False)
    order_ref: str | None = Field(None, max_length=16)


async def _ours(
    db: AsyncSession, tid: uuid.UUID, order_id: uuid.UUID | None
) -> dict[str, Any] | None:
    if order_id is None:
        return None
    o = (
        await db.execute(
            select(
                Order.order_no, Order.status, Order.paid_at, Order.delivered_at, Order.buyer_id
            ).where(Order.id == order_id, Order.tenant_id == tid)
        )
    ).first()
    if o is None:
        return None
    email = await db.scalar(select(User.email).where(User.id == o.buyer_id, User.tenant_id == tid))
    return {
        "order_no": o.order_no,
        "status": o.status,
        "paid_at": o.paid_at.isoformat() if o.paid_at else None,
        "delivered_at": o.delivered_at.isoformat() if o.delivered_at else None,
        "buyer": mask_email(email) if email else None,
    }


@router.post("/lookup")
async def lookup(
    body: LookupIn,
    staff: Any = Depends(platform_staff),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    await enforce(res.redis, "procurement:redeem-lookup", str(staff.id), 30, 600)
    tid = staff.tenant_id
    if bool(body.code) == bool(body.order_ref):
        raise Validation("Give a code or an order reference", code="code_or_order")
    code: str | None = None
    if body.code:
        code = _valid_code(body.code)
        fps = [_fp(res, tid, code)]
        stock = (
            await db.execute(
                select(CardStock.order_id, CardStock.status).where(
                    CardStock.tenant_id == tid, CardStock.fingerprint == fps[0]
                )
            )
        ).first()
        order_id = stock.order_id if stock else None
        masked = rl.display_mask(*rl.mask_code(code))
    else:
        order_id = await db.scalar(
            select(Order.id).where(
                Order.tenant_id == tid, Order.order_no == (body.order_ref or "").strip()
            )
        )
        if order_id is None:
            raise NotFound("No such order")
        fps = list(
            await db.scalars(
                select(CardStock.fingerprint).where(
                    CardStock.tenant_id == tid, CardStock.order_id == order_id
                )
            )
        )
        masked = None
    ours = await _ours(db, tid, order_id)
    rows = (
        list(
            await db.scalars(
                select(RedeemAttempt)
                .where(RedeemAttempt.tenant_id == tid, RedeemAttempt.code_fp.in_(fps))
                .order_by(RedeemAttempt.at.desc())
                .limit(50)
            )
        )
        if fps
        else []
    )
    order_no = ours["order_no"] if ours else None
    attempt_rows = [_row(a, order_no) for a in rows]  # before the rollback expires the objects
    vendor: dict[str, Any] | None = None
    if code is not None:
        await db.rollback()  # no connection held during the vendor read
        status, data = await _cdk_call(
            res, "/internal/redeem/cdk-status", {"tenant_id": str(tid), "code": code}
        )
        if status == 200:
            vendor = {
                "available": True,
                "found": bool(data.get("found")),
                "status": data.get("status"),
                "plan": data.get("plan"),
                "env": data.get("env"),
            }
        else:
            vendor = {"available": False, "reason": _err_code(data) or "supplier_unavailable"}
    return {
        "code": masked,
        "ours": ours,
        "attempts": attempt_rows,
        "vendor": vendor,
        "sold_by_us": ours is not None,
    }


class CdkSetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=8, max_length=128, repr=False)
    confirm: Literal[True]  # the console asks first; the API refuses a request without it
    # a code we did not sell (no card_stock fingerprint) is refused unless this is set as well
    force_unsold: bool = False


@router.post("/cdk/{action}")
async def cdk_set(
    action: Literal["disable", "enable"],
    body: CdkSetIn,
    request: Request,
    admin: Any = Depends(platform_admin_session),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    await enforce(res.redis, "procurement:redeem-write", str(admin.id), 30, 3600)
    tid, uid, role = admin.tenant_id, admin.id, admin.role
    code = _valid_code(body.code)
    masked = rl.display_mask(*rl.mask_code(code))
    stock = (
        await db.execute(
            select(CardStock.id, CardStock.order_id).where(
                CardStock.tenant_id == tid, CardStock.fingerprint == _fp(res, tid, code)
            )
        )
    ).first()
    if stock is None and not body.force_unsold:
        await db.rollback()
        raise InvalidState(
            "This code was not sold by this platform; confirm to force the change",
            code="code_not_ours",
        )
    stock_order = stock.order_id if stock is not None else None
    order_no = (
        await db.scalar(
            select(Order.order_no).where(Order.id == stock_order, Order.tenant_id == tid)
        )
        if stock_order
        else None
    )
    await db.rollback()
    status, data = await _cdk_call(
        res, "/internal/redeem/cdk-set", {"tenant_id": str(tid), "code": code, "action": action}
    )
    if status != 200:
        ec = _err_code(data)
        if status == 429:
            raise RateLimited(retry_after=60)
        if status == 404 or ec == "cdk_not_found":
            raise NotFound("The supplier has no such code")
        if ec == "production_writes_disabled":
            raise InvalidState(
                "Writes to a production supplier are disabled", code="production_writes_disabled"
            )
        if ec == "supplier_disabled":
            raise InvalidState("The supplier is disabled", code="supplier_disabled")
        if status == 409:
            raise InvalidState("The supplier refused this change", code="cdk_rejected")
        if status == 422:
            raise Validation("The supplier request was refused", code="bad_request")
        raise Unavailable("The supplier could not be reached", code="supplier_unavailable")
    before = {"status": data.get("before")}
    after = {"status": data.get("after")}
    await audit(
        db,
        tenant_id=tid,
        action=f"procurement.redeem_cdk_{action}d",
        actor_id=uid,
        actor_role=role,
        target="redeem_cdk",
        before=before,
        after=after,
        ip=client_ip(request),
        detail={
            "code": masked,  # never the full code
            "order_no": order_no,
            "cdk_id": data.get("id"),
            "supplier_id": data.get("supplier"),
            "env": data.get("env"),
            **({"force_unsold": True} if stock is None else {}),
        },
        severity="warning",
    )
    await db.commit()
    return {"action": action, "before": before, "after": after}


# ------------------------------------------------------------------------------------ settings
class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    maintenance: StrictBool | None = None
    maintenance_en: str | None = Field(None, max_length=rl.MAINTENANCE_MAX)
    maintenance_zh: str | None = Field(None, max_length=rl.MAINTENANCE_MAX)
    announcement_en: str | None = Field(None, max_length=rl.ANNOUNCE_MAX)
    announcement_zh: str | None = Field(None, max_length=rl.ANNOUNCE_MAX)
    # a step -> per-minute limit per client IP (null = back to the deployment default)
    ip_limits: dict[Step, StrictInt | None] | None = None

    @field_validator("ip_limits")
    @classmethod
    def _bounds(cls, v: dict[str, int | None] | None) -> dict[str, int | None] | None:
        for step, n in (v or {}).items():
            lo, hi = rl.LIMIT_BOUNDS[step]
            if n is not None and not lo <= n <= hi:
                raise ValueError(f"{step} limit must be between {lo} and {hi}")
        return v


def _view(s: dict[str, Any], defaults: dict[str, int]) -> dict[str, Any]:
    return {
        **s,
        "defaults": defaults,
        "bounds": {k: {"min": lo, "max": hi} for k, (lo, hi) in rl.LIMIT_BOUNDS.items()},
        "caps": {"announcement": rl.ANNOUNCE_MAX, "maintenance": rl.MAINTENANCE_MAX},
    }


def _env_defaults(_: Resources) -> dict[str, int]:
    """The built-in per-IP limits (the procurement process may override them by env)."""
    from app.procurement.config import ProcurementSettings
    from app.procurement.redeem import parse_limits

    return parse_limits(ProcurementSettings().redeem_ip_limits)


@router.get("/settings")
async def get_settings(
    admin: Any = Depends(platform_admin_session),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    await enforce(res.redis, "procurement:redeem-settings", str(admin.id), 60, 600)
    raw = await db.scalar(select(Tenant.redeem_settings).where(Tenant.id == admin.tenant_id))
    return _view(rl.normalize_settings(raw), _env_defaults(res))


@router.put("/settings")
async def put_settings(
    body: SettingsIn,
    request: Request,
    admin: Any = Depends(platform_admin_session),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    await enforce(res.redis, "procurement:redeem-settings-write", str(admin.id), 30, 3600)
    tid = admin.tenant_id
    patch = body.model_dump(exclude_unset=True)
    if not patch:
        raise Validation("Nothing to change", code="nothing_to_change")
    if rl.has_link(
        *(patch.get(k) for k in patch if k.startswith(("announcement_", "maintenance_")))
    ):
        raise Validation("Links are not allowed in these texts", code="announcement_no_links")
    raw = await db.scalar(select(Tenant.redeem_settings).where(Tenant.id == tid).with_for_update())
    before = rl.normalize_settings(raw)
    merged: dict[str, Any] = {**before, "ip_limits": dict(before["ip_limits"])}
    for key, value in patch.items():
        if key == "ip_limits":
            for step, n in (value or {}).items():
                if n is None:
                    merged["ip_limits"].pop(step, None)
                else:
                    merged["ip_limits"][step] = n
        elif value is not None:
            merged[key] = value
    after = rl.normalize_settings(merged)
    await db.execute(update(Tenant).where(Tenant.id == tid).values(redeem_settings=after))
    await audit(
        db,
        tenant_id=tid,
        action="procurement.redeem_settings_updated",
        actor_id=admin.id,
        actor_role=admin.role,
        target="redeem_settings",
        before=before,
        after=after,
        ip=client_ip(request),
        detail={"fields": sorted(patch)},
        severity="warning" if "maintenance" in patch else "info",
    )
    await db.commit()
    return _view(after, _env_defaults(res))
