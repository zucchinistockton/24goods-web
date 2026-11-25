"""Private EPay payment presentation. A browser view never settles an order."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import client_ip, current_user, get_db, get_res, get_tenant_id
from app.core.errors import NotFound
from app.core.money import fmt
from app.core.ratelimit import enforce
from app.core.resources import Resources
from app.models import Checkout, CustomDomain, Order, PaymentAttempt, User
from app.modules.payments.usdt import qr_svg

router = APIRouter(prefix="/pay/epay", tags=["payments"])

_ALIPAY_QR_HOST = "qr.alipay.com"


async def _shop_origin(request: Request, res: Resources, db: AsyncSession, tenant_id) -> str:
    """Origin of the shop the buyer is on. The Host header is only trusted when it is a
    verified custom domain of this tenant; anything else falls back to the configured base."""
    base = urlsplit(res.settings.public_base_url)
    host = (request.headers.get("host") or "").strip().lower().rstrip(".")
    hostname = host.split(":", 1)[0]
    if hostname and hostname != (base.hostname or ""):
        ok = await db.scalar(
            select(CustomDomain.id).where(
                CustomDomain.hostname == hostname,
                CustomDomain.tenant_id == tenant_id,
                CustomDomain.status == "active",
                CustomDomain.verified_at.is_not(None),
            )
        )
        if ok is not None:
            return urlunsplit((base.scheme, hostname, "", "", ""))
    return urlunsplit((base.scheme, base.netloc, "", "", ""))


def _gone(to: str = "/orders") -> RedirectResponse:
    r = RedirectResponse(to, status_code=302)
    r.headers["Cache-Control"] = "no-store"
    r.headers["Referrer-Policy"] = "no-referrer"
    return r


@router.get("/go/{attempt_id}")
async def hop(
    attempt_id: uuid.UUID,
    request: Request,
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> RedirectResponse:
    """First-party hop to the stored, already vetted gateway cashier link (QR target).
    The target is never taken from the request; this never settles anything."""
    await enforce(res.redis, "epay:go", f"{tenant_id}:{client_ip(request) or '-'}", 60, 60)
    att = await db.scalar(
        select(PaymentAttempt).where(
            PaymentAttempt.id == attempt_id, PaymentAttempt.tenant_id == tenant_id
        )
    )
    if (
        att is None
        or att.status != "ready"
        or att.provider_id is None
        or att.expires_at is None
        or att.expires_at <= datetime.now(timezone.utc)
    ):
        return _gone()
    ref = att.local_ref or ""
    model, col = (
        (Checkout, Checkout.checkout_no) if ref.startswith("C") else (Order, Order.order_no)
    )
    status = await db.scalar(select(model.status).where(col == ref, model.tenant_id == tenant_id))
    data = att.data or {}
    payload = data.get("payload")
    if status != "pending_payment" or data.get("kind") != "redirect" or not payload:
        return _gone()
    if urlsplit(payload).scheme != "https":
        return _gone()
    r = RedirectResponse(payload, status_code=302)
    r.headers["Cache-Control"] = "no-store"
    r.headers["Referrer-Policy"] = "no-referrer"
    return r


@router.get("/{no}")
async def payment_view(
    no: str,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    model, col = (Checkout, Checkout.checkout_no) if no.startswith("C") else (Order, Order.order_no)
    target = await db.scalar(
        select(model).where(col == no, model.tenant_id == user.tenant_id, model.buyer_id == user.id)
    )
    if target is None:
        raise NotFound("Payment not found")
    att = await db.scalar(
        select(PaymentAttempt)
        .where(
            PaymentAttempt.tenant_id == user.tenant_id,
            PaymentAttempt.local_ref == no,
            PaymentAttempt.provider_id.is_not(None),
        )
        .order_by(PaymentAttempt.created_at.desc())
        .limit(1)
    )
    if att is None:
        raise NotFound("Payment not found")
    expired = att.expires_at is None or att.expires_at <= datetime.now(timezone.utc)
    usable = target.status == "pending_payment" and not expired and att.status == "ready"
    data = att.data if usable else {}
    kind = data.get("kind")
    payload = data.get("payload", "")
    qr_svg_out: str | None = None
    open_url: str | None = None
    if kind == "redirect":
        origin = await _shop_origin(request, res, db, user.tenant_id)
        open_url = f"{origin}/api/v1/pay/epay/go/{att.id}"
        qr_svg_out = qr_svg(open_url)
    elif kind == "qr":
        qr_svg_out = qr_svg(payload)
        sp = urlsplit(payload)
        if sp.scheme == "https" and sp.hostname == _ALIPAY_QR_HOST:
            open_url = payload
    return {
        "attempt_id": str(att.id),
        "status": target.status,
        "attempt_status": att.status,
        "amount": fmt(att.amount_minor),
        "currency": att.currency,
        "expires_at": att.expires_at.isoformat() if att.expires_at else None,
        "expired": expired,
        "kind": kind,
        "open_url": open_url,
        "qr_svg": qr_svg_out,
    }
