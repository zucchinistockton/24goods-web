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


async def _price_guard(
    db: AsyncSession, p: Product, v: ProductVariant, price: int, *, explicit_price: bool
) -> None:
    """A variant is only ever ACTIVE with a price staff set on purpose that covers its cost
    (payment audit 2026-11-17, finding 5): a generated variant starts unpriced; its price must be
    sent in the same request that activates it (or before), and be >= the highest cost estimate
    of its sources (converted with the staff-entered FX rate; no rate = refused)."""
    from app.core import costmodel as cm
    from app.models import SupplierOffer
    from app.modules.content.site import tenant_settings

    if not v.price_set and not explicit_price:
        raise Validation(
            "Set a price for this variant before activating it", code="variant_unpriced"
        )
    offers = list(
        await db.scalars(
            select(SupplierOffer).where(
                SupplierOffer.tenant_id == v.tenant_id,
                SupplierOffer.product_id == v.product_id,
                SupplierOffer.variant_id == v.id,
                SupplierOffer.cost_estimate.is_not(None),
            )
        )
    )
    if not offers:
        return
    st = cm.from_tenant(await tenant_settings(db, v.tenant_id))
    price_usd = cm.usd_from_minor(price, cm.rate_for(st, p.currency))
    costs = []
    for o in offers:
        per = cm.rate_for(st, o.currency)
        if per is None or per <= 0 or o.cost_estimate is None:
            costs.append(None)
        else:
            costs.append(Decimal(o.cost_estimate) / per)
    if price_usd is None or any(c is None for c in costs):
        raise Validation("Set the FX rate in Costs first, so the price can be checked",
                         code="fx_rate_missing")  # fmt: skip
    if price_usd < max(c for c in costs if c is not None):
        raise Validation("The price is below the cost estimate of this variant",
                         code="price_below_cost")  # fmt: skip


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
        v.options = new_opts
    if "subscription" in data:
        sb = body.subscription
        v.subscription = sb.model_dump() if sb is not None else None
    explicit_price = data.get("price") is not None
    if v.status == "active" and (explicit_price or before["status"] != "active" or not v.price_set):
        await _price_guard(db, p, v, price, explicit_price=explicit_price)
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
