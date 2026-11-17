"""Admin CRUD for product variants (review-v11 W1). Every price / stock / status change writes
an audit row with before/after (invariant 3); every query filters by tenant (invariant 4)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import Conflict, InvalidState, NotFound, Validation
from app.core.money import MoneyError, parse_amount
from app.core.scoped import get_scoped
from app.models import Product, ProductVariant, User
from app.modules.admin.schemas import VariantIn, VariantPatch
from app.modules.catalog import variants as svc

MAX_VARIANTS = 50


def _floor(value: str) -> int:
    if value.strip() in ("0", "0.0", "0.00"):
        return 0
    return parse_amount(value)


def _summary(v: ProductVariant) -> dict[str, Any]:
    return {
        "name_i18n": v.name_i18n,
        "price_minor": v.price_minor,
        "floor_price_minor": v.floor_price_minor,
        "manual_stock": v.manual_stock,
        "sort": v.sort,
        "status": v.status,
        "after_sales_hours": v.after_sales_hours,
        "after_sales_months": v.after_sales_months,
        "subscription": v.subscription,
        "options": v.options,
        "price_set": v.price_set,
    }


def cost_floor_minor(
    st: dict[str, Any], cost: Decimal, cost_currency: str, price_currency: str
) -> int | None:
    """The cost estimate (in the source's currency) as minor units of the price currency, rounded
    UP (so a floor set to it is never below the cost); ``None`` without both FX rates."""
    from app.core import costmodel as cm

    src, dst = cm.rate_for(st, cost_currency), cm.rate_for(st, price_currency)
    if src is None or dst is None or src <= 0 or dst <= 0:
        return None
    return int((cost / src * dst * 100).to_integral_value(rounding="ROUND_CEILING"))


def cost_problem(
    st: dict[str, Any],
    p: Product,
    price: int,
    floor: int,
    offers: list[Any],
    *,
    has_options: bool,
) -> tuple[str, str] | None:
    """``(code, message)`` when the sources of a variant are not covered by its price AND
    floor, else ``None``. Cost = each source's ``cost_estimate`` in USD (staff FX rates).
    Not included: promotions, coupons and payment-channel fees (the margin report shows those)."""
    from app.core import costmodel as cm

    if has_options or offers:
        if not offers or any(o.cost_estimate is None for o in offers):
            return ("cost_unknown", "Every source needs a cost estimate before activation")
    if not offers:
        return None
    price_usd = cm.usd_from_minor(price, cm.rate_for(st, p.currency))
    floor_usd = cm.usd_from_minor(floor, cm.rate_for(st, p.currency))
    costs: list[Decimal | None] = []
    for o in offers:
        per = cm.rate_for(st, o.currency)
        costs.append(None if per is None or per <= 0 else Decimal(o.cost_estimate) / per)
    if price_usd is None or floor_usd is None or any(c is None for c in costs):
        return ("fx_rate_missing", "Set the FX rate in Costs first, so the price can be checked")
    top = max(c for c in costs if c is not None)
    if price_usd < top or floor_usd < top:
        return ("price_below_cost", "The price or floor price is below the cost estimate")
    return None


async def _variant_offers(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    variant_id: uuid.UUID,
    *,
    enabled_only: bool = False,
) -> list[Any]:
    from app.models import SupplierOffer

    q = select(SupplierOffer).where(
        SupplierOffer.tenant_id == tenant_id,
        SupplierOffer.product_id == product_id,
        SupplierOffer.variant_id == variant_id,
    )
    if enabled_only:
        q = q.where(SupplierOffer.enabled.is_(True))
    return list(await db.scalars(q))


async def _price_guard(
    db: AsyncSession,
    p: Product,
    v: ProductVariant,
    price: int,
    floor: int,
    *,
    explicit_price: bool,
) -> None:
    """A variant is only ever ACTIVE with a price staff set on purpose that covers its cost
    (payment audit 2026-11-17, finding 5; round 2, finding 5): the price AND the floor price
    (quotes use the floor) are >= the highest cost estimate of its sources (converted with
    the staff-entered FX rate; no rate = refused), and a variant with plan x region options or with
    sources must have a source, and a cost estimate on every source (``cost_unknown``)."""
    from app.core import costmodel as cm
    from app.modules.content.site import tenant_settings

    if not v.price_set and not explicit_price:
        raise Validation(
            "Set a price for this variant before activating it", code="variant_unpriced"
        )
    offers = await _variant_offers(db, v.tenant_id, v.product_id, v.id) if v.id else []
    st = cm.from_tenant(await tenant_settings(db, v.tenant_id))
    bad = cost_problem(st, p, price, floor, offers, has_options=v.options is not None)
    if bad is not None:
        raise Validation(bad[1], code=bad[0])


async def source_cost_guard(
    db: AsyncSession, tenant_id: uuid.UUID, offer: Any, *, others_enabled: list[Any]
) -> None:
    """Enabling a source, or raising its cost, on an ACTIVE variant: refused (``price_below_cost``
    / ``cost_unknown``) while the variant's price or floor would not cover the new cost."""
    from app.core import costmodel as cm
    from app.modules.content.site import tenant_settings

    if offer.variant_id is None or not offer.enabled:
        return
    v = await db.scalar(
        select(ProductVariant).where(
            ProductVariant.id == offer.variant_id, ProductVariant.tenant_id == tenant_id
        )
    )
    if v is None or v.status != "active":
        return
    p = await db.scalar(
        select(Product).where(Product.id == v.product_id, Product.tenant_id == tenant_id)
    )
    if p is None:
        return
    st = cm.from_tenant(await tenant_settings(db, tenant_id))
    bad = cost_problem(
        st,
        p,
        v.price_minor,
        v.floor_price_minor,
        [*others_enabled, offer],
        has_options=v.options is not None,
    )
    if bad is not None:
        raise Validation(bad[1], code=bad[0])


async def _options_change_guard(
    db: AsyncSession, v: ProductVariant, new_opts: dict[str, Any] | None
) -> None:
    """The procurement engine pins sources to the variant's LIVE ``options``: they do not change
    while an order of the variant is waiting for fulfilment, and never to something an enabled
    source does not buy (payment audit round 2, finding 2)."""
    from app.models import Order
    from app.modules.catalog.regions import source_matches_options

    open_orders = await db.scalar(
        select(Order.id)
        .where(
            Order.tenant_id == v.tenant_id,
            Order.variant_id == v.id,
            Order.status.in_(("pending_payment", "paid", "manual_review")),
        )
        .limit(1)
    )
    if open_orders is not None:
        raise Conflict(
            "This variant has orders that are not fulfilled yet", code="variant_has_open_orders"
        )
    offers = await _variant_offers(db, v.tenant_id, v.product_id, v.id, enabled_only=True)
    if any(
        o.kind != "vcc" and not source_matches_options(dict(o.params or {}), new_opts)
        for o in offers
    ):
        raise Validation(
            "An enabled source does not match the new plan and region",
            code="source_options_mismatch",
        )


async def _options_taken(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    options: dict[str, Any],
    except_id: uuid.UUID | None = None,
) -> bool:
    """Another variant of the product already has this (plan, region), any status."""
    q = select(ProductVariant.id).where(
        ProductVariant.tenant_id == tenant_id,
        ProductVariant.product_id == product_id,
        ProductVariant.options["plan"].astext == options["plan"],
        ProductVariant.options["region"].astext == options["region"],
    )
    if except_id is not None:
        q = q.where(ProductVariant.id != except_id)
    return (await db.scalar(q.limit(1))) is not None


async def _product(db: AsyncSession, tenant_id: uuid.UUID, product_id: uuid.UUID) -> Product:
    p = await db.scalar(
        select(Product)
        .where(Product.id == product_id, Product.tenant_id == tenant_id)
        .with_for_update()
    )
    if p is None:
        raise NotFound("Product not found")
    return p


async def _get(
    db: AsyncSession, tenant_id: uuid.UUID, product_id: uuid.UUID, variant_id: uuid.UUID
) -> ProductVariant:
    v = await get_scoped(db, ProductVariant, variant_id, tenant_id)
    if v is None or v.product_id != product_id:
        raise NotFound("Variant not found")
    return v


async def admin_views(
    db: AsyncSession, tenant_id: uuid.UUID, products: list[Product]
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    rows = await svc.variants_for(db, tenant_id, [p.id for p in products], include_archived=True)
    cards = await svc.stocked_counts(db, tenant_id, [(p, rows[p.id]) for p in products])
    by_id = {p.id: p for p in products}
    return {
        pid: [svc.admin_variant_view(v, by_id[pid], cards) for v in vs] for pid, vs in rows.items()
    }


async def list_variants(
    db: AsyncSession, tenant_id: uuid.UUID, product_id: uuid.UUID
) -> list[dict[str, Any]]:
    p = await db.scalar(
        select(Product).where(Product.id == product_id, Product.tenant_id == tenant_id)
    )
    if p is None:
        raise NotFound("Product not found")
    return (await admin_views(db, tenant_id, [p]))[p.id]


def _terms(data: dict[str, Any]) -> None:
    if data.get("after_sales_hours") is not None and data.get("after_sales_months") is not None:
        raise Validation("set either after_sales_hours or after_sales_months, not both")


async def create(
    db: AsyncSession, product_id: uuid.UUID, body: VariantIn, admin: User, ip: str | None
) -> dict[str, Any]:
    p = await _product(db, admin.tenant_id, product_id)
    try:
        price = parse_amount(body.price)
        floor = _floor(body.floor_price)
    except MoneyError as exc:
        raise Validation(f"price: {exc}") from None
    if floor > price:
        raise Validation("floor_price must not exceed price")
    _terms(body.model_dump())
    n = len(await svc.variants_for(db, admin.tenant_id, [p.id], include_archived=True))
    if n >= MAX_VARIANTS:
        raise Validation(f"at most {MAX_VARIANTS} variants per product")
    options = body.options.model_dump() if body.options else None
    if options is not None and await _options_taken(db, admin.tenant_id, p.id, options):
        raise Conflict("A variant with this plan and region exists", code="variant_options_taken")
    v = ProductVariant(
        tenant_id=admin.tenant_id,
        product_id=p.id,
        options=options,
        name_i18n=body.name_i18n,
        price_minor=price,
        floor_price_minor=floor,
        manual_stock=body.manual_stock if p.delivery_mode == "manual" else None,
        sort=body.sort,
        status=body.status,
        after_sales_hours=body.after_sales_hours,
        after_sales_months=body.after_sales_months,
        subscription=body.subscription.model_dump() if body.subscription else None,
        sold=0,
    )
    if v.status == "active":
        await _price_guard(db, p, v, price, floor, explicit_price=True)
    db.add(v)
    await db.flush()
    await audit(
        db,
        tenant_id=admin.tenant_id,
        action="variant.created",
        actor_id=admin.id,
        actor_role=admin.role,
        target=f"variant:{v.id}",
        after=_summary(v),
        ip=ip,
        detail={"product_id": str(p.id)},
    )
    await db.commit()
    await db.refresh(v)
    return _one(v, p, await svc.card_counts(db, admin.tenant_id, [v.id]))


def _one(v: ProductVariant, p: Product, cards: dict[uuid.UUID, int]) -> dict[str, Any]:
    return svc.admin_variant_view(v, p, cards)


async def patch(
    db: AsyncSession,
    product_id: uuid.UUID,
    variant_id: uuid.UUID,
    body: VariantPatch,
    admin: User,
    ip: str | None,
) -> dict[str, Any]:
    p = await _product(db, admin.tenant_id, product_id)
    v = await _get(db, admin.tenant_id, product_id, variant_id)
    await db.refresh(v, with_for_update=True)
    before = _summary(v)
    data = body.model_dump(exclude_unset=True)
    _terms(data)
    try:
        price = parse_amount(data["price"]) if data.get("price") is not None else v.price_minor
        floor = (
            _floor(data["floor_price"])
            if data.get("floor_price") is not None
            else v.floor_price_minor
        )
    except MoneyError as exc:
        raise Validation(f"price: {exc}") from None
    if floor > price:
        raise Validation("floor_price must not exceed price")
    if data.get("name_i18n") is not None:
        v.name_i18n = data["name_i18n"]
    for key in ("sort", "status"):
        if data.get(key) is not None:
            setattr(v, key, data[key])
    if "manual_stock" in data and p.delivery_mode == "manual":
        v.manual_stock = data["manual_stock"]  # explicit null = unlimited
    # explicit null clears (= inherit the product's)
    if "after_sales_hours" in data:
        v.after_sales_hours = data["after_sales_hours"]
        if data["after_sales_hours"] is not None:
            v.after_sales_months = None
    if "after_sales_months" in data:
        v.after_sales_months = data["after_sales_months"]
        if data["after_sales_months"] is not None:
            v.after_sales_hours = None
    if "options" in data:
        new_opts = body.options.model_dump() if body.options else None
        if new_opts is not None and await _options_taken(
            db, admin.tenant_id, p.id, new_opts, except_id=v.id
        ):
            raise Conflict(
                "A variant with this plan and region exists", code="variant_options_taken"
            )
        if new_opts != v.options:
            await _options_change_guard(db, v, new_opts)
        v.options = new_opts
    if "subscription" in data:
        sb = body.subscription
        v.subscription = sb.model_dump() if sb is not None else None
    explicit_price = data.get("price") is not None
    if v.status == "active" and (
        explicit_price
        or "floor_price" in data
        or "options" in data
        or before["status"] != "active"
        or not v.price_set
    ):
        await _price_guard(db, p, v, price, floor, explicit_price=explicit_price)
    if explicit_price:
        v.price_set = True
    v.price_minor, v.floor_price_minor = price, floor
    after = _summary(v)
    await audit(
        db,
        tenant_id=admin.tenant_id,
        action="variant.updated",
        actor_id=admin.id,
        actor_role=admin.role,
        target=f"variant:{v.id}",
        before=before,
        after=after,
        ip=ip,
        detail={
            "product_id": str(p.id),
            "fields": sorted(data),
            "price_changed": before["price_minor"] != price,
        },
    )
    await db.commit()
    await db.refresh(v)
    return _one(v, p, await svc.card_counts(db, admin.tenant_id, [v.id]))


async def _in_use(db: AsyncSession, variant_id: uuid.UUID) -> bool:
    return bool(
        await db.scalar(
            text(
                """SELECT EXISTS (SELECT 1 FROM orders WHERE variant_id = :v)
                      OR EXISTS (SELECT 1 FROM card_stock WHERE variant_id = :v)
                      OR EXISTS (SELECT 1 FROM quotes WHERE variant_id = :v)"""
            ),
            {"v": variant_id},
        )
    )


async def delete(
    db: AsyncSession, product_id: uuid.UUID, variant_id: uuid.UUID, admin: User, ip: str | None
) -> str:
    """Hard delete when nothing references the variant, else archive. Returns the outcome."""
    await _product(db, admin.tenant_id, product_id)
    v = await _get(db, admin.tenant_id, product_id, variant_id)
    before = _summary(v)
    if await _in_use(db, v.id):
        if v.status == "archived":
            raise InvalidState("Variant is already archived")
        v.status = "archived"
        outcome, action = "archived", "variant.archived"
    else:
        await db.delete(v)
        outcome, action = "deleted", "variant.deleted"
    await audit(
        db,
        tenant_id=admin.tenant_id,
        action=action,
        actor_id=admin.id,
        actor_role=admin.role,
        target=f"variant:{variant_id}",
        before=before,
        after=_summary(v) if outcome == "archived" else None,
        ip=ip,
        detail={"product_id": str(product_id)},
    )
    await db.commit()
    return outcome
