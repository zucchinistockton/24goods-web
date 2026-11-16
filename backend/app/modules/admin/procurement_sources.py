"""Sources per variant (review-v12 §7): which supplier offers fill a product / variant, in which
order, with which routing mode. Admins of the platform tenant only (the suppliers and their
secrets belong to the platform). Every change is audited; every query filters by tenant.

"Check availability" asks the procurement process (internal token), which holds the supplier
secrets, exactly like the supplier "Test connection" button.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.deps import client_ip, get_db, get_res
from app.core.errors import Conflict, NotFound, RateLimited, Unavailable, Validation
from app.core.ratelimit import enforce
from app.core.resources import Resources
from app.models import Product, ProductVariant, SupplierAccount, SupplierOffer, User
from app.modules.admin.suppliers import _dec, _iso, platform_admin
from app.suppliers.base import OfferSpec
from app.suppliers.zovocard.supplier import cdk_issue_body, vcc_spec

router = APIRouter(prefix="/admin/procurement/sources", tags=["admin"])

MAX_OFFERS_PER_TARGET = 10
COST_RE = r"^[0-9]{1,9}(\.[0-9]{1,6})?$"
ALLOWED_PARAMS = (
    "plan", "product", "payment_country",
    "product_code", "init_amount", "recharge_amount", "first_name", "last_name",
)  # fmt: skip
AMOUNT_RE = r"^[0-9]{1,6}(\.[0-9]{1,2})?$"


class OfferParams(BaseModel):
    """Offer parameters. ``cdk``: ``plan`` (+ ``payment_country``). ``vcc`` (virtual card): the
    supplier's ``product_code``, the opening ``init_amount`` and an optional ``recharge_amount``
    top-up (decimal strings, at most 2 decimals), plus an optional cardholder name. Which set
    applies follows the TARGET product's delivery mode, never the request."""

    plan: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,40}$")
    product: Literal["gpt"] = "gpt"  # CDK issue is GPT-only (vendor appendix A.5)
    payment_country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    product_code: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,40}$")
    init_amount: str | None = Field(default=None, pattern=AMOUNT_RE)
    recharge_amount: str | None = Field(default=None, pattern=AMOUNT_RE)
    first_name: str | None = Field(default=None, pattern=r"^[A-Za-z]{1,32}$")
    last_name: str | None = Field(default=None, pattern=r"^[A-Za-z]{1,32}$")


class SourceIn(BaseModel):
    product_id: uuid.UUID
    variant_id: uuid.UUID | None = None
    supplier_id: uuid.UUID
    params: OfferParams
    # per-offer redemption cost in the supplier's balance currency (breaker reserve fallback)
    cost_estimate: str | None = Field(default=None, pattern=COST_RE)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    priority: int = Field(default=100, ge=0, le=10000)
    enabled: bool = False


class SourcePatch(BaseModel):
    params: OfferParams | None = None
    cost_estimate: str | None = Field(default=None, pattern=COST_RE)
    clear_cost: bool = False
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    priority: int | None = Field(default=None, ge=0, le=10000)
    enabled: bool | None = None


class OrderIn(BaseModel):
    product_id: uuid.UUID
    variant_id: uuid.UUID | None = None
    ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_OFFERS_PER_TARGET)


class RoutingIn(BaseModel):
    product_id: uuid.UUID
    variant_id: uuid.UUID | None = None
    mode: Literal["priority", "round_robin"]


def _require_cost(offer: SupplierOffer, supplier: SupplierAccount) -> None:
    """An offer is enabled only with a usable cost: the balance breaker reserves it per issued
    code and fails closed on an unknown one. Usable = a ``cost_estimate`` in the supplier's
    balance currency, or a cost the supplier itself named on the last availability check."""
    if offer.cost_estimate is not None and offer.currency == supplier.currency:
        return
    if offer.cost_known:
        return
    raise Validation(
        "Set a cost estimate in the supplier's balance currency before enabling this source",
        code="cost_required",
    )


VCC_KEYS = ("product_code", "init_amount", "recharge_amount", "first_name", "last_name")


def _params(p: OfferParams, kind: str = "cdk") -> dict[str, Any]:
    """The parameters of ONE kind only; an offer never carries the other kind's keys."""
    data = p.model_dump(exclude_none=True)
    if kind == "vcc":
        return {k: v for k, v in data.items() if k in VCC_KEYS}
    return {k: v for k, v in data.items() if k not in VCC_KEYS}


def _valid_params(kind: str, params: dict[str, Any]) -> bool:
    spec = OfferSpec(kind=kind, params=params)  # type: ignore[arg-type]
    return (vcc_spec(spec) if kind == "vcc" else cdk_issue_body(spec)) is not None


def _view(o: SupplierOffer, s: SupplierAccount) -> dict[str, Any]:
    return {
        "id": str(o.id),
        "product_id": str(o.product_id),
        "variant_id": str(o.variant_id) if o.variant_id else None,
        "supplier_id": str(o.supplier_id),
        "supplier_code": s.code,
        "supplier_env": s.env,
        "supplier_enabled": s.enabled,
        "supplier_breaker_open": s.breaker_open,
        "kind": o.kind,
        "params": {k: v for k, v in (o.params or {}).items() if k in ALLOWED_PARAMS},
        "cost_estimate": _dec(o.cost_estimate),
        "currency": o.currency,
        "priority": o.priority,
        "enabled": o.enabled,
        "last_check_ok": o.last_check_ok,
        "last_check_at": _iso(o.last_check_at),
        "created_at": _iso(o.created_at),
    }


def _snap(o: SupplierOffer) -> dict[str, Any]:
    return {
        "params": o.params,
        "cost_estimate": _dec(o.cost_estimate),
        "currency": o.currency,
        "priority": o.priority,
        "enabled": o.enabled,
    }


async def _target(
    db: AsyncSession, tenant_id: uuid.UUID, product_id: uuid.UUID, variant_id: uuid.UUID | None
) -> Product | ProductVariant:
    """The product, or the variant that belongs to it, of THIS tenant (else 404)."""
    product = await db.scalar(
        select(Product).where(Product.id == product_id, Product.tenant_id == tenant_id)
    )
    if product is None:
        raise NotFound("Product not found")
    if variant_id is None:
        return product
    variant = await db.scalar(
        select(ProductVariant).where(
            ProductVariant.id == variant_id,
            ProductVariant.product_id == product_id,
            ProductVariant.tenant_id == tenant_id,
        )
    )
    if variant is None:
        raise NotFound("Variant not found")
    return variant


def _match(product_id: uuid.UUID, variant_id: uuid.UUID | None):  # type: ignore[no-untyped-def]
    return (
        SupplierOffer.product_id == product_id,
        SupplierOffer.variant_id == variant_id
        if variant_id is not None
        else SupplierOffer.variant_id.is_(None),
    )


async def _offers(
    db: AsyncSession, tenant_id: uuid.UUID, product_id: uuid.UUID, variant_id: uuid.UUID | None
) -> list[tuple[SupplierOffer, SupplierAccount]]:
    rows = await db.execute(
        select(SupplierOffer, SupplierAccount)
        .join(SupplierAccount, SupplierAccount.id == SupplierOffer.supplier_id)
        .where(SupplierOffer.tenant_id == tenant_id, *_match(product_id, variant_id))
        .order_by(SupplierOffer.priority, SupplierAccount.priority, SupplierOffer.created_at)
    )
    return [(o, s) for o, s in rows.all()]


async def _own(
    db: AsyncSession, tenant_id: uuid.UUID, offer_id: uuid.UUID, *, lock: bool = False
) -> tuple[SupplierOffer, SupplierAccount]:
    stmt = (
        select(SupplierOffer, SupplierAccount)
        .join(SupplierAccount, SupplierAccount.id == SupplierOffer.supplier_id)
        .where(SupplierOffer.tenant_id == tenant_id, SupplierOffer.id == offer_id)
    )
    if lock:
        stmt = stmt.with_for_update(of=SupplierOffer)
    row = (await db.execute(stmt)).first()
    if row is None:
        raise NotFound("Source not found")
    return row[0], row[1]


def _routing(t: Product | ProductVariant) -> str:
    return t.procurement_routing or "priority"


@router.get("")
async def list_sources(
    product_id: uuid.UUID,
    variant_id: uuid.UUID | None = Query(default=None),
    admin: User = Depends(platform_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    target = await _target(db, admin.tenant_id, product_id, variant_id)
    items = [_view(o, s) for o, s in await _offers(db, admin.tenant_id, product_id, variant_id)]
    return {
        "product_id": str(product_id),
        "variant_id": str(variant_id) if variant_id else None,
        "routing": _routing(target),
        "items": items,
    }


@router.post("", status_code=201)
async def add_source(
    body: SourceIn,
    request: Request,
    admin: User = Depends(platform_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    await enforce(res.redis, "procurement:sources", str(admin.id), 60, 3600)
    tid = admin.tenant_id
    target = await _target(db, tid, body.product_id, body.variant_id)
    kind = (
        "vcc"
        if (
            target.delivery_mode
            if isinstance(target, Product)
            else await db.scalar(
                select(Product.delivery_mode).where(Product.id == target.product_id)
            )
        )
        == "upstream_vcc"
        else "cdk"
    )
    supplier = await db.scalar(
        select(SupplierAccount).where(
            SupplierAccount.id == body.supplier_id, SupplierAccount.tenant_id == tid
        )
    )
    if supplier is None:
        raise NotFound("Supplier not found")
    params = _params(body.params, kind)
    if not _valid_params(kind, params):
        raise Validation("These source parameters cannot be sent to a supplier", code="bad_params")
    existing = await _offers(db, tid, body.product_id, body.variant_id)
    if len(existing) >= MAX_OFFERS_PER_TARGET:
        raise Validation("Too many sources for this target", code="too_many_sources")
    if any(o.supplier_id == supplier.id and o.params == params for o, _ in existing):
        raise Conflict("This supplier is already a source with the same plan", code="taken")
    offer = SupplierOffer(
        id=uuid.uuid4(),
        tenant_id=tid,
        supplier_id=supplier.id,
        product_id=body.product_id,
        variant_id=body.variant_id,
        kind=kind,
        params=params,
        cost_estimate=Decimal(body.cost_estimate) if body.cost_estimate is not None else None,
        currency=body.currency,
        priority=body.priority,
        enabled=body.enabled,
    )
    if offer.enabled:
        _require_cost(offer, supplier)
    db.add(offer)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise Conflict("This source already exists", code="taken") from None
    await audit(
        db,
        tenant_id=tid,
        action="procurement.offer_created",
        actor_id=admin.id,
        actor_role=admin.role,
        target=f"supplier_offer:{offer.id}",
        after={
            "supplier_id": str(supplier.id),
            "product_id": str(body.product_id),
            "variant_id": str(body.variant_id) if body.variant_id else None,
            **_snap(offer),
        },
        ip=client_ip(request),
    )
    await db.commit()
    await db.refresh(offer)
    return _view(offer, supplier)


@router.post("/order")
async def sort_sources(
    body: OrderIn,
    request: Request,
    admin: User = Depends(platform_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    """Priority from list position (10, 20, ...); the list names every source exactly once."""
    await enforce(res.redis, "procurement:sources", str(admin.id), 60, 3600)
    tid = admin.tenant_id
    target = await _target(db, tid, body.product_id, body.variant_id)
    rows = await db.scalars(
        select(SupplierOffer)
        .where(SupplierOffer.tenant_id == tid, *_match(body.product_id, body.variant_id))
        .with_for_update()
    )
    offers = {o.id: o for o in rows}
    if len(set(body.ids)) != len(body.ids) or set(body.ids) != set(offers):
        raise Validation("The order must list every source of this target exactly once")
    before = {str(i): o.priority for i, o in offers.items()}
    for n, oid in enumerate(body.ids, start=1):
        offers[oid].priority = n * 10
    await audit(
        db,
        tenant_id=tid,
        action="procurement.offers_reordered",
        actor_id=admin.id,
        actor_role=admin.role,
        target=f"product:{body.product_id}",
        before=before,
        after={str(i): offers[i].priority for i in body.ids},
        detail={"variant_id": str(body.variant_id) if body.variant_id else None},
        ip=client_ip(request),
    )
    await db.commit()
    items = [_view(o, s) for o, s in await _offers(db, tid, body.product_id, body.variant_id)]
    return {
        "product_id": str(body.product_id),
        "variant_id": str(body.variant_id) if body.variant_id else None,
        "routing": _routing(target),
        "items": items,
    }


@router.put("/routing")
async def set_routing(
    body: RoutingIn,
    request: Request,
    admin: User = Depends(platform_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    await enforce(res.redis, "procurement:sources", str(admin.id), 60, 3600)
    tid = admin.tenant_id
    target = await _target(db, tid, body.product_id, body.variant_id)
    before = _routing(target)
    table = ProductVariant if body.variant_id is not None else Product
    await db.execute(
        update(table)
        .where(table.id == target.id, table.tenant_id == tid)
        .values(procurement_routing=body.mode)
    )
    if before != body.mode:
        await audit(
            db,
            tenant_id=tid,
            action="procurement.routing_changed",
            actor_id=admin.id,
            actor_role=admin.role,
            target=f"{'variant' if body.variant_id else 'product'}:{target.id}",
            before={"routing": before},
            after={"routing": body.mode},
            ip=client_ip(request),
        )
    await db.commit()
    return {
        "product_id": str(body.product_id),
        "variant_id": str(body.variant_id) if body.variant_id else None,
        "routing": body.mode,
    }


@router.patch("/{offer_id}")
async def update_source(
    offer_id: uuid.UUID,
    body: SourcePatch,
    request: Request,
    admin: User = Depends(platform_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    await enforce(res.redis, "procurement:sources", str(admin.id), 60, 3600)
    offer, supplier = await _own(db, admin.tenant_id, offer_id, lock=True)
    before = _snap(offer)
    if body.params is not None:
        params = _params(body.params, offer.kind)
        if not _valid_params(offer.kind, params):
            raise Validation(
                "These source parameters cannot be sent to a supplier", code="bad_params"
            )
        if params != offer.params:
            offer.params = params
            offer.last_check_ok, offer.last_check_at = None, None  # an untested request
            offer.cost_known = False  # the supplier's cost belonged to the old plan
    if body.clear_cost:
        offer.cost_estimate = None
    elif body.cost_estimate is not None:
        offer.cost_estimate = Decimal(body.cost_estimate)
    if body.currency is not None:
        offer.currency = body.currency
    if body.priority is not None:
        offer.priority = body.priority
    if body.enabled is not None:
        offer.enabled = body.enabled
    if offer.enabled and (body.enabled or body.clear_cost or body.currency or body.params):
        _require_cost(offer, supplier)  # an enabled source never loses its cost
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise Conflict("This source already exists", code="taken") from None
    after = _snap(offer)
    if after != before:
        await audit(
            db,
            tenant_id=admin.tenant_id,
            action="procurement.offer_updated",
            actor_id=admin.id,
            actor_role=admin.role,
            target=f"supplier_offer:{offer.id}",
            before=before,
            after=after,
            ip=client_ip(request),
        )
    await db.commit()
    await db.refresh(offer)
    return _view(offer, supplier)


@router.post("/{offer_id}/check")
async def check_source(
    offer_id: uuid.UUID,
    request: Request,
    admin: User = Depends(platform_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    """ "Check availability": the procurement process asks the supplier (GET only) and stores
    the result the storefront stock display relies on."""
    await enforce(res.redis, "procurement:check", str(admin.id), 30, 600)
    await _own(db, admin.tenant_id, offer_id)
    tid, uid, role = admin.tenant_id, admin.id, admin.role
    await db.rollback()  # no transaction held open across the procurement call
    token = res.settings.procurement_token
    if len(token) < 32:
        raise Unavailable("The procurement service is not configured", code="procurement_off")
    try:
        resp = await res.procurement.post(
            "/internal/check-offer",
            json={"tenant_id": str(tid), "offer_id": str(offer_id)},
            headers={"X-Internal-Token": token},
        )
    except httpx.HTTPError:
        raise Unavailable(
            "The procurement service is unreachable", code="procurement_unreachable"
        ) from None
    if resp.status_code == 404:
        raise NotFound("Source not found")
    if resp.status_code == 429:
        raise RateLimited(retry_after=int(resp.headers.get("Retry-After", "600")))
    try:
        data = resp.json() if resp.status_code == 200 else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("available"), bool):
        raise Unavailable("The procurement service failed", code="procurement_error")
    await audit(
        db,
        tenant_id=tid,
        action="procurement.offer_checked",
        actor_id=uid,
        actor_role=role,
        target=f"supplier_offer:{offer_id}",
        detail={"available": data["available"], "reason": str(data.get("reason") or "")[:64]},
        ip=client_ip(request),
    )
    await db.commit()
    return {
        "available": data["available"],
        "reason": data.get("reason"),
        "checked_at": data.get("checked_at"),
    }
