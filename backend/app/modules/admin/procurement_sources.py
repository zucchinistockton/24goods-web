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
from app.modules.admin.variants import source_cost_guard
from app.modules.catalog.regions import is_half_plan, options_of, source_matches_options
from app.procurement.funding import funding_card_clash
from app.suppliers.base import OfferSpec
from app.suppliers.zovocard import direct as dr
from app.suppliers.zovocard.direct import direct_spec
from app.suppliers.zovocard.supplier import cdk_issue_body, vcc_spec
from app.suppliers.zzshu import zzshu_spec

router = APIRouter(prefix="/admin/procurement/sources", tags=["admin"])

MAX_OFFERS_PER_TARGET = 10
COST_RE = r"^[0-9]{1,9}(\.[0-9]{1,6})?$"
ALLOWED_PARAMS = (
    "plan", "product", "payment_country", "payment_currency", "card_id",
    "card_mode", "vendor_switch", "product_code", "init_amount", "recharge_amount",
    "first_name", "last_name",
)  # fmt: skip
AMOUNT_RE = r"^[0-9]{1,6}(\.[0-9]{1,2})?$"


class OfferParams(BaseModel):
    """Offer parameters. ``cdk``: ``plan`` (+ ``payment_country``). ``vcc`` (virtual card): the
    supplier's ``product_code``, the opening ``init_amount`` and an optional ``recharge_amount``
    top-up (decimal strings, at most 2 decimals), plus an optional cardholder name. Which set
    applies follows the TARGET product's delivery mode, never the request."""

    plan: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,40}$")
    # ``cdk``: GPT only (vendor appendix A.5; defaults to gpt). ``direct``: REQUIRED and one of the
    # vendor's closed list (an unknown product is silently turned into a GPT order by the vendor)
    product: Literal["gpt", "claude", "grok"] | None = None
    payment_country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    payment_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")  # direct, GPT only
    # direct: the funding card (a card of OUR vendor account) the supplier pays with
    card_id: int | None = Field(default=None, ge=1, le=10**12)
    # direct (issue #29): how the funding card is chosen. ``fixed`` (default) needs ``card_id``;
    # ``auto_existing`` lets the vendor's card-pool schedule pick; ``auto_issue`` (a NEW card per
    # order) is reserved and refused with ``card_mode_unavailable``. ``vendor_switch`` lets the
    # vendor switch card after a refused payment (extra failed attempts cost ``failed_fee``)
    card_mode: str | None = Field(default=None, max_length=16)
    vendor_switch: bool | None = None
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
DIRECT_KEYS = (
    "product", "plan", "card_id", "card_mode", "vendor_switch",
    "payment_country", "payment_currency",
)  # fmt: skip
CDK_KEYS = ("product", "plan", "payment_country")


def _params(p: OfferParams, kind: str = "cdk") -> dict[str, Any]:
    """The parameters of ONE kind only; an offer never carries the other kinds' keys."""
    data = p.model_dump(exclude_none=True)
    if kind == "vcc":
        return {k: v for k, v in data.items() if k in VCC_KEYS}
    if kind == "direct":  # no default product: it must be named (enum lock)
        out = {k: v for k, v in data.items() if k in DIRECT_KEYS}
        if out.get("card_mode", "fixed") != "fixed":
            out.pop("card_id", None)  # an auto mode never uses a configured card
        if out.get("card_mode") == "fixed":
            out.pop("card_mode")  # the default is not stored: today's offers keep their params
        if out.get("vendor_switch") is False:
            out.pop("vendor_switch")
        return out
    data.setdefault("product", "gpt")
    return {k: v for k, v in data.items() if k in CDK_KEYS}


def _card_mode_guard(kind: str, p: OfferParams) -> None:
    """``auto_issue`` is reserved (the vendor documents no way to issue a card on a direct order,
    review-v12 §21.4): refused with its own code, before the generic parameter check."""
    if kind == "direct" and p.card_mode not in (None, *dr.CARD_MODES):
        raise Validation(
            "This card mode is not available",
            code="card_mode_unavailable" if p.card_mode == dr.CARD_MODE_RESERVED else "bad_params",
        )


def _valid_params(kind: str, params: dict[str, Any], adapter: str = "zovocard") -> bool:
    spec = OfferSpec(kind=kind, params=params)  # type: ignore[arg-type]
    if adapter in ("vmcardio", "herosms"):  # ADR-055 issuer; ADR-057 SMS (sms_products, not offers)
        return False
    if adapter == "zzshu":  # ADR-054: direct only, plan + region, no card parameters
        return kind == "direct" and zzshu_spec(spec) is not None
    if kind == "vcc":
        return vcc_spec(spec) is not None
    if kind == "direct":
        return direct_spec(spec) is not None
    return cdk_issue_body(spec) is not None


async def _direct_checks(
    db: AsyncSession,
    res: Resources,
    offer_kind: str,
    params: dict[str, Any],
    supplier: SupplierAccount,
    *,
    enabling: bool,
) -> None:
    """Rules of a direct source that need the database / the environment: the funding card is
    never a card bought for a buyer (H1), and in production a real buyer's session never goes to a
    sandbox supplier (S-L1)."""
    if offer_kind != "direct":
        return
    if supplier.adapter == "zzshu":  # its cards are opened per order: no funding card to vet
        if enabling and res.settings.env == "prod" and supplier.env == "sandbox":
            raise Validation(
                "A sandbox supplier cannot take real buyers' sessions in production",
                code="sandbox_in_prod",
            )
        return
    if params.get("vendor_switch") is True and supplier.env == "production":
        # the vendor cannot restrict the switch pool yet: a switched order could pay with a card
        # bought for a buyer. Sandbox and the demo mock only (review-v12 §21.2)
        raise Validation(
            "Vendor card switch is not available on a production supplier",
            code="vendor_switch_unavailable",
        )
    if params.get("card_mode", "fixed") == "fixed" and await funding_card_clash(
        db, supplier.tenant_id, supplier.id, params.get("card_id", 0)
    ):
        raise Validation(
            "That card was bought for a buyer: it cannot be a funding card",
            code="funding_card_invalid",
        )
    if enabling and res.settings.env == "prod" and supplier.env == "sandbox":
        raise Validation(
            "A sandbox supplier cannot take real buyers' sessions in production",
            code="sandbox_in_prod",
        )


async def _options_guard(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    variant_id: uuid.UUID | None,
    kind: str,
    params: dict[str, Any],
) -> None:
    """A source of a plan x region variant buys exactly that plan in that region: otherwise a
    half-price sale could be fulfilled with a full-price plan, or in another region (payment
    audit 2026-11-17, finding 2)."""
    if variant_id is None or kind == "vcc":
        return
    opts = options_of(
        await db.scalar(
            select(ProductVariant.options).where(
                ProductVariant.id == variant_id, ProductVariant.tenant_id == tenant_id
            )
        )
    )
    if not source_matches_options(params, opts):
        raise Validation(
            "The source's plan / payment country must match the variant's plan and region",
            code="source_options_mismatch",
        )


def _plan_mix_guard(
    kind: str,
    params: dict[str, Any],
    enabled: bool,
    others: list[SupplierOffer],
    skip: uuid.UUID | None = None,
) -> None:
    """The enabled sources of one target never mix half-price and full-price plans, and a
    half-price target never mixes plan or country: failover or round-robin would otherwise reach
    the half-price source for an order that was never checked (payment audit round 2, finding 1)."""
    if kind == "vcc" or not enabled:
        return
    live = [dict(o.params or {}) for o in others if o.id != skip and o.enabled and o.kind != "vcc"]
    live.append(params)
    if len(live) < 2:
        return
    half = [is_half_plan(p.get("plan")) for p in live]
    if any(half) and (
        not all(half)
        or len({(p.get("plan"), p.get("product", "gpt"), p.get("payment_country")) for p in live})
        > 1
    ):
        raise Validation(
            "The enabled sources of a target cannot mix half-price and full-price plans, "
            "plans or countries",
            code="source_plan_mixed",
        )


def _same_vendor_product(
    kind: str, params: dict[str, Any], others: list[SupplierOffer], skip: uuid.UUID | None = None
) -> None:
    """Checkout asks the buyer for ONE kind of account session per target, so every direct source
    of a target buys the same vendor product."""
    if kind != "direct":
        return
    for o in others:
        if (
            o.id != skip
            and o.kind == "direct"
            and (o.params or {}).get("product") != params.get("product")
        ):
            raise Validation(
                "Every direct source of a product must buy the same vendor product",
                code="mixed_products",
            )


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
    mode = (
        target.delivery_mode
        if isinstance(target, Product)
        else await db.scalar(select(Product.delivery_mode).where(Product.id == target.product_id))
    )
    kind = {"upstream_vcc": "vcc", "upstream_direct": "direct"}.get(mode or "", "cdk")
    supplier = await db.scalar(
        select(SupplierAccount).where(
            SupplierAccount.id == body.supplier_id, SupplierAccount.tenant_id == tid
        )
    )
    if supplier is None:
        raise NotFound("Supplier not found")
    _card_mode_guard(kind, body.params)
    params = _params(body.params, kind)
    if not _valid_params(kind, params, supplier.adapter):
        raise Validation("These source parameters cannot be sent to a supplier", code="bad_params")
    await _options_guard(db, tid, body.variant_id, kind, params)
    existing = await _offers(db, tid, body.product_id, body.variant_id)
    _same_vendor_product(kind, params, [o for o, _ in existing])
    await _direct_checks(db, res, kind, params, supplier, enabling=body.enabled)
    _plan_mix_guard(kind, params, body.enabled, [o for o, _ in existing])
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
        await source_cost_guard(
            db, tid, offer, others_enabled=[o for o, _ in existing if o.enabled and o.kind != "vcc"]
        )
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
        _card_mode_guard(offer.kind, body.params)
        params = _params(body.params, offer.kind)
        if not _valid_params(offer.kind, params, supplier.adapter):
            raise Validation(
                "These source parameters cannot be sent to a supplier", code="bad_params"
            )
        if params != offer.params:
            await _options_guard(db, admin.tenant_id, offer.variant_id, offer.kind, params)
            if offer.kind == "direct":
                await _direct_checks(
                    db, res, "direct", params, supplier, enabling=bool(offer.enabled)
                )
                _same_vendor_product(
                    "direct",
                    params,
                    [
                        o
                        for o, _ in await _offers(
                            db, admin.tenant_id, offer.product_id, offer.variant_id
                        )
                    ],
                    skip=offer.id,
                )
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
        if body.enabled:
            await _options_guard(
                db, admin.tenant_id, offer.variant_id, offer.kind, dict(offer.params or {})
            )
            await _direct_checks(
                db, res, offer.kind, dict(offer.params or {}), supplier, enabling=True
            )
    if offer.enabled and (body.enabled or body.clear_cost or body.currency or body.params):
        _require_cost(offer, supplier)  # an enabled source never loses its cost
    if offer.enabled:
        with db.no_autoflush:
            others = await _offers(db, admin.tenant_id, offer.product_id, offer.variant_id)
        if body.enabled or body.params is not None:
            _plan_mix_guard(
                offer.kind, dict(offer.params or {}), True, [o for o, _ in others], skip=offer.id
            )
        if offer.kind != "vcc" and (
            body.enabled
            or body.clear_cost
            or body.cost_estimate is not None
            or body.currency
            or body.params is not None
        ):
            with db.no_autoflush:
                await source_cost_guard(
                    db,
                    admin.tenant_id,
                    offer,
                    others_enabled=[o for o, _ in others if o.id != offer.id and o.enabled],
                )
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
        was = bool((before["params"] or {}).get("vendor_switch"))
        now_on = bool((after["params"] or {}).get("vendor_switch"))
        if was != now_on:  # weakens "one order per card" and may cost failed fees (§21.2)
            await audit(
                db,
                tenant_id=admin.tenant_id,
                action="procurement.vendor_switch_changed",
                actor_id=admin.id,
                actor_role=admin.role,
                target=f"supplier_offer:{offer.id}",
                before={"vendor_switch": was},
                after={"vendor_switch": now_on},
                ip=client_ip(request),
                severity="warning",
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
