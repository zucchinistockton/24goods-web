"""Half-price eligibility check BEFORE payment (issue #27, review-v12 §20.4): API side.

Check-then-order: a plan with a half-price key (``plus_half``, Grok ``*_half``) cannot be ordered
without a passed check. ``create_order`` (the funnel of every order path: buy-now, cart, quotes)
refuses with ``half_check_required`` unless it is given a usable check of the same buyer, product
and variant; cart and quote paths pass none, so a half-price plan is buy-now only. ``pay_url``
re-checks the same rule for orders that exist already.

The buyer's account session is validated and sealed AT ONCE with the procurement public key (same
primitive as ``direct.py``; the AAD binds the blob to the check id) and inserted into
``half_price_checks``. Only the procurement process opens it: it reads eligibility with the method
the owner selected (``vendor_preflight`` / ``chatgpt_direct``), writes the outcome and nulls the
blob. The ``manual`` method seals nothing: staff decide in the console. Nothing here logs, audits
or returns the session, and the API role cannot read ``blob`` back (column privileges).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import Conflict, InvalidState, NotFound, Unavailable, Validation
from app.core.resources import Resources
from app.models import (
    HalfPriceCheck,
    Order,
    SupplierAccount,
    SupplierOffer,
    Tenant,
    User,
)
from app.modules.catalog import regions as rg
from app.modules.catalog.regions import is_half_plan
from app.suppliers.direct_credential import CredentialError, validate_credential

log = logging.getLogger("app.orders.halfcheck")

CHECK_TTL = timedelta(minutes=30)  # a passed check is valid for creating an order this long
USABLE = ("passed", "manual_pending", "manual_approved")
OPEN = tuple(
    getattr(HalfPriceCheck, c)
    for c in (
        "id", "tenant_id", "user_id", "product_id", "variant_id", "method", "vendor_product",
        "plan", "country", "status", "reason", "detail", "email_masked", "order_id", "created_at",
        "decided_at",
    )
)  # fmt: skip


@dataclass(frozen=True)
class TargetPlan:
    vendor_product: str
    plan: str
    country: str | None
    kind: str


async def requirement(
    db: AsyncSession, tenant_id: uuid.UUID, product_id: uuid.UUID, variant_id: uuid.UUID | None
) -> TargetPlan | None:
    """The primary (priority-first) enabled source of the target, when its plan is a half-price
    key; ``None`` = no check needed."""
    match = (
        SupplierOffer.variant_id == variant_id
        if variant_id is not None
        else SupplierOffer.variant_id.is_(None)
    )
    row = (
        await db.execute(
            select(SupplierOffer.kind, SupplierOffer.params)
            .join(SupplierAccount, SupplierAccount.id == SupplierOffer.supplier_id)
            .where(
                SupplierOffer.tenant_id == tenant_id,
                SupplierOffer.product_id == product_id,
                match,
                SupplierOffer.enabled.is_(True),
                SupplierOffer.kind.in_(("cdk", "direct")),
                SupplierAccount.enabled.is_(True),
            )
            .order_by(SupplierOffer.priority, SupplierOffer.created_at)
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    params = row.params or {}
    plan = params.get("plan")
    if not is_half_plan(plan):
        return None
    country = params.get("payment_country")
    return TargetPlan(
        str(params.get("product") or "gpt"),
        str(plan),
        country if isinstance(country, str) else None,
        row.kind,
    )


async def required_flags(
    db: AsyncSession, tenant_id: uuid.UUID, product_id: uuid.UUID, variant_ids: list[uuid.UUID]
) -> dict[uuid.UUID, bool]:
    """Per variant: does buying it need a half-price check (storefront flag)."""
    return {v: await requirement(db, tenant_id, product_id, v) is not None for v in variant_ids}


def view(row: Any) -> dict[str, Any]:
    d = row.detail or {}
    return {
        "id": str(row.id),
        "status": row.status,
        "reason": row.reason,
        "method": row.method,
        "plan": row.plan,
        "account": row.email_masked,
        "quote": d.get("quote"),
        "expires_at": (row.created_at + CHECK_TTL).isoformat(),
        "usable": row.status in USABLE and row.order_id is None,
    }


async def _method(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    ts = dict(await db.scalar(select(Tenant.settings).where(Tenant.id == tenant_id)) or {})
    return rg.settings_of(ts)["half_price_check"]


async def create_check(
    db: AsyncSession,
    res: Resources,
    user: User,
    ip: str | None,
    *,
    product_id: uuid.UUID,
    variant_id: uuid.UUID | None,
    session: str | None,
) -> dict[str, Any]:
    tid, uid = user.tenant_id, user.id
    target = await requirement(db, tid, product_id, variant_id)
    if target is None:
        raise Conflict("This item needs no eligibility check", code="half_check_not_required")
    method = await _method(db, tid)
    check_id = uuid.uuid4()
    values: dict[str, Any] = {
        "id": check_id,
        "tenant_id": tid,
        "user_id": uid,
        "product_id": product_id,
        "variant_id": variant_id,
        "method": method,
        "vendor_product": target.vendor_product,
        "plan": target.plan,
        "country": target.country,
    }
    email_masked = None
    if method == "manual":
        values["status"] = "manual_pending"  # nothing is read: staff decide
    else:
        if res.cred_sealer is None:
            raise Unavailable("The check is not available right now", code="half_check_unavailable")
        if not (session or "").strip():
            raise Validation("The account session is required", code="session_required")
        try:
            checked = validate_credential(target.vendor_product, session)
        except CredentialError as exc:
            raise Validation(
                "The account session cannot be used", code=f"session_{exc.code}"
            ) from None
        email_masked = checked.email_masked
        values["blob"] = res.cred_sealer.seal(
            checked.canonical, tenant_id=str(tid), order_id=str(check_id)
        )
        values["kid"] = res.cred_sealer.active_kid
        values["email_masked"] = email_masked
        values["status"] = "pending"
    del session
    # nothing older than the session lifetime lingers with a blob: the API removes its own
    # abandoned ``pending`` rows (procurement's sweeper wipes any blob after minutes)
    await db.execute(
        delete(HalfPriceCheck.__table__).where(
            HalfPriceCheck.__table__.c.tenant_id == tid,
            HalfPriceCheck.__table__.c.status == "pending",
            HalfPriceCheck.__table__.c.created_at < datetime.now(timezone.utc) - timedelta(hours=1),
        )
    )
    await db.execute(insert(HalfPriceCheck.__table__).values(**values))  # no RETURNING
    await audit(
        db,
        tenant_id=tid,
        action="half_check.started",
        actor_id=uid,
        actor_role=user.role,
        target=f"half_check:{check_id}",
        detail={"method": method, "plan": target.plan, "product_id": str(product_id)},
        ip=ip,
    )
    await db.commit()
    if method != "manual":
        await _run(res, db, tid, uid, check_id)
    row = (await db.execute(select(*OPEN).where(HalfPriceCheck.id == check_id))).one()
    return view(row)


async def _run(
    res: Resources, db: AsyncSession, tid: uuid.UUID, uid: uuid.UUID, check_id: uuid.UUID
) -> None:
    """Ask the procurement process (the only opener of the sealed session) to read eligibility."""
    token = res.settings.procurement_token
    ok = False
    if len(token) >= 32:
        try:
            resp = await res.procurement.post(
                "/internal/half-check",
                json={"tenant_id": str(tid), "check_id": str(check_id)},
                headers={"X-Internal-Token": token},
            )
            ok = resp.status_code == 200
        except httpx.HTTPError:
            ok = False
    if ok:
        return
    # procurement did not answer: remove the pending row (and with it the sealed session)
    await db.execute(
        delete(HalfPriceCheck.__table__).where(
            HalfPriceCheck.__table__.c.id == check_id,
            HalfPriceCheck.__table__.c.tenant_id == tid,
            HalfPriceCheck.__table__.c.status == "pending",
        )
    )
    await db.commit()
    raise Unavailable("The check is not available right now", code="half_check_unavailable")


async def claim(
    db: AsyncSession,
    user: User,
    check_id: uuid.UUID | None,
    *,
    product_id: uuid.UUID,
    variant_id: uuid.UUID | None,
) -> uuid.UUID:
    """The usable check an order is created with: the buyer's own, for this product and variant,
    unconsumed and fresh. Locked, so two orders cannot use one check."""
    if check_id is None:
        raise Validation("This plan needs an eligibility check first", code="half_check_required")
    row = (
        await db.execute(
            select(*OPEN)
            .where(HalfPriceCheck.id == check_id, HalfPriceCheck.tenant_id == user.tenant_id)
            .with_for_update()
        )
    ).first()
    now = datetime.now(timezone.utc)
    if (
        row is None
        or row.user_id != user.id
        or row.product_id != product_id
        or row.variant_id != variant_id
        or row.order_id is not None
        or row.status not in USABLE
        or now - row.created_at > CHECK_TTL
    ):
        raise Validation("The eligibility check is not valid", code="half_check_invalid")
    return check_id


async def attach(
    db: AsyncSession, tenant_id: uuid.UUID, check_id: uuid.UUID, order_id: Any
) -> None:
    await db.execute(
        update(HalfPriceCheck.__table__)
        .where(
            HalfPriceCheck.__table__.c.id == check_id,
            HalfPriceCheck.__table__.c.tenant_id == tenant_id,
            HalfPriceCheck.__table__.c.order_id.is_(None),
        )
        .values(order_id=order_id)
    )


async def order_gate(db: AsyncSession, order: Order) -> None:
    """Payment is refused for a half-price order without a usable, unrefused check (defence in
    depth: ``create_order`` already refuses to create one)."""
    if order.kind != "goods" or order.delivery_mode not in ("upstream_cdk", "upstream_direct"):
        return
    if order.product_id is None:
        return
    if await requirement(db, order.tenant_id, order.product_id, order.variant_id) is None:
        return
    status = await db.scalar(
        select(HalfPriceCheck.status).where(
            HalfPriceCheck.tenant_id == order.tenant_id, HalfPriceCheck.order_id == order.id
        )
    )
    if status is None or status not in USABLE:
        raise InvalidState(
            "This plan needs an eligibility check before payment",
            code="half_check_refused" if status == "manual_refused" else "half_check_required",
        )


async def decide(
    db: AsyncSession,
    admin: User,
    check_id: uuid.UUID,
    approve: bool,
    ip: str | None,
) -> dict[str, Any]:
    """Staff decision of a ``manual`` check (``manual_pending`` only)."""
    row = (
        await db.execute(
            select(*OPEN)
            .where(HalfPriceCheck.id == check_id, HalfPriceCheck.tenant_id == admin.tenant_id)
            .with_for_update()
        )
    ).first()
    if row is None:
        raise NotFound("Check not found")
    if row.status != "manual_pending":
        raise InvalidState("This check is not waiting for staff", code="check_not_pending")
    new = "manual_approved" if approve else "manual_refused"
    await db.execute(
        update(HalfPriceCheck.__table__)
        .where(
            HalfPriceCheck.__table__.c.id == check_id,
            HalfPriceCheck.__table__.c.tenant_id == admin.tenant_id,
        )
        .values(status=new, decided_at=datetime.now(timezone.utc))
    )
    await audit(
        db,
        tenant_id=admin.tenant_id,
        action="half_check.approved" if approve else "half_check.refused",
        actor_id=admin.id,
        actor_role=admin.role,
        target=f"half_check:{check_id}",
        before={"status": row.status},
        after={"status": new},
        detail={"order_id": str(row.order_id) if row.order_id else None},
        ip=ip,
    )
    await db.commit()
    return {"id": str(check_id), "status": new}


__all__ = ["TargetPlan", "view"]
