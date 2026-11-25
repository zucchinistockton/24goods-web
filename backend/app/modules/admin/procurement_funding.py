"""Automatic top-ups of direct orders' funding cards: settings and log (ADR-048).

* ``GET /admin/procurement/funding``: platform staff. The settings (automatic top-ups, caps, BIN
  priority), what today's top-ups used of the daily cap, and the latest top-ups (card id and
  amount only: no card number exists here).
* ``PUT /admin/procurement/funding``: platform administrators in a session, with a FRESH
  authenticator code (owner 2026-11-19: everything that moves money goes through 2FA; the
  account must have 2FA on). Validated by :mod:`app.core.funding`, audited before / after.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import funding as fs
from app.core.audit import audit
from app.core.deps import client_ip, get_db, get_res
from app.core.errors import Validation
from app.core.ratelimit import enforce
from app.core.resources import Resources
from app.models import ProcurementFunding, Tenant, User
from app.modules.admin.procurement_cards import platform_admin_session, platform_staff
from app.modules.auth import twofactor

router = APIRouter(prefix="/admin/procurement/funding", tags=["admin"])
COUNTED = ("planned", "sent", "done")
SPENDING = ("recharge", "open")  # a sweep brings money back (ADR-049)


def _day_start() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime.combine(now.date(), time(0), tzinfo=timezone.utc)


async def _view(db: AsyncSession, tid: Any, limit: int = 50) -> dict[str, Any]:
    st = fs.normalize(await db.scalar(select(Tenant.funding_settings).where(Tenant.id == tid)))
    spent = await db.scalar(
        select(func.coalesce(func.sum(ProcurementFunding.amount), 0)).where(
            ProcurementFunding.tenant_id == tid,
            ProcurementFunding.kind.in_(SPENDING),
            ProcurementFunding.state.in_(COUNTED),
            ProcurementFunding.created_at >= _day_start(),
        )
    )
    rows = (
        await db.execute(
            select(ProcurementFunding)
            .where(ProcurementFunding.tenant_id == tid)
            .order_by(ProcurementFunding.created_at.desc())
            .limit(limit)
        )
    ).scalars()
    spent_d = Decimal(spent or 0).quantize(Decimal("0.01"))
    return {
        "settings": st,
        "today": {
            "spent_usd": str(spent_d),
            "left_usd": str(max(Decimal(0), Decimal(st["daily_cap_usd"]) - spent_d)),
        },
        "topups": [
            {
                "id": str(r.id),
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "state": r.state,
                "kind": r.kind,
                "card_id": r.card_id,
                "amount": str(Decimal(r.amount).quantize(Decimal("0.01"))),
                "currency": r.currency,
                "order_id": str(r.order_id) if r.order_id else None,
                "job_id": str(r.job_id) if r.job_id else None,
                "error": r.error,
            }
            for r in rows
        ],
    }


@router.get("")
async def get_funding(
    limit: int = Query(50, ge=1, le=200),
    staff: User = Depends(platform_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _view(db, staff.tenant_id, limit)


class FundingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto_recharge: StrictBool | None = None
    auto_open_cards: StrictBool | None = None
    sweep_leftover: StrictBool | None = None
    upgrade_pay_difference: StrictBool | None = None
    cancel_renewal_before_upgrade: StrictBool | None = None
    wallet_alert_usd: Decimal | None = Field(default=None, ge=0, le=100000)
    daily_cap_usd: Decimal | None = Field(default=None, ge=0, le=10000)
    zzshu_card_margin_pct: Decimal | None = Field(default=None, ge=0, le=100)
    zzshu_points_alert: int | None = Field(default=None, ge=0, le=1000)
    per_topup_max_usd: Decimal | None = Field(default=None, ge=0, le=5000)
    bin_priority: list[str] | None = Field(default=None, max_length=fs.MAX_BINS)
    totp_code: str = Field(min_length=6, max_length=8, repr=False)


@router.put("")
async def put_funding(
    body: FundingIn,
    request: Request,
    admin: User = Depends(platform_admin_session),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    await enforce(res.redis, "procurement:funding-write", str(admin.id), 20, 3600)
    patch = body.model_dump(exclude_none=True, exclude={"totp_code"})
    if not patch:
        raise Validation("Nothing to change", code="nothing_to_change")
    clean = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in patch.items()}
    try:
        fs.validate(clean)  # a bad value is refused before the code is spent
    except ValueError as exc:
        raise Validation(f"{exc}: not a valid value", code="bad_funding_settings") from None
    # then the code (spent once): a wrong code changes nothing
    await twofactor.step_up(db, res.redis, res.settings, admin, body.totp_code, client_ip(request))
    tid, uid, role = admin.tenant_id, admin.id, admin.role
    raw = await db.scalar(
        select(Tenant.funding_settings).where(Tenant.id == tid).with_for_update()
    )  # (a deferred column: read and written explicitly, under the row lock)
    before = fs.normalize(raw)
    after = fs.validate(clean, before)
    await db.execute(update(Tenant).where(Tenant.id == tid).values(funding_settings=after))
    await audit(
        db,
        tenant_id=tid,
        action="procurement.funding_settings_updated",
        actor_id=uid,
        actor_role=role,
        target=f"tenant:{tid}",
        before=before,
        after=after,
        ip=client_ip(request),
        detail={"fields": sorted(patch), "two_factor": True},
        severity="warning",
    )
    await db.commit()
    return await _view(db, tid)
