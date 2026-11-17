"""Owner settings of the procurement features of issue #27 (review-v12 §20), live regions.

* ``GET/PUT /admin/procurement/options``: the region allowlist and the half-price check method
  (``tenants.settings['procurement_options']``), audited.
* ``GET /admin/procurement/regions?product=gpt``: the vendor's CURRENT ``payment_regions`` (read by
  the procurement process: the API holds no supplier secrets), each marked ``allowed`` by the
  allowlist. A vendor that cannot be read gives ``available: false`` and no regions.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.deps import client_ip, get_db, get_res
from app.core.errors import NotFound, Unavailable, Validation
from app.core.ratelimit import enforce
from app.core.resources import Resources
from app.models import (
    HalfPriceCheck,
    Order,
    Product,
    ProductVariant,
    SupplierAccount,
    SupplierOffer,
    Tenant,
    User,
)
from app.modules.admin.suppliers import platform_admin
from app.modules.catalog import regions as rg

router = APIRouter(prefix="/admin/procurement", tags=["admin"])


class OptionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region_allowlist: list[str] | None = Field(None, max_length=30)
    half_price_check: Literal["vendor_preflight", "chatgpt_direct", "manual"] | None = None


async def vendor_regions(
    res: Resources, tenant_id: uuid.UUID, product: str
) -> list[dict[str, str]] | None:
    """Ask the procurement process for the vendor's live regions; ``None`` = not readable now."""
    token = res.settings.procurement_token
    if len(token) < 32:
        raise Unavailable("The procurement service is not configured", code="procurement_off")
    try:
        resp = await res.procurement.post(
            "/internal/regions",
            json={"tenant_id": str(tenant_id), "product": product},
            headers={"X-Internal-Token": token},
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        rows = resp.json().get("regions")
    except (ValueError, AttributeError):
        return None
    if not isinstance(rows, list):
        return None
    return [
        {"country": r["country"], "currency": r["currency"]}
        for r in rows
        if isinstance(r, dict)
        and isinstance(r.get("country"), str)
        and isinstance(r.get("currency"), str)
    ]


async def _settings(db: AsyncSession, tid: uuid.UUID) -> dict[str, Any]:
    return dict(await db.scalar(select(Tenant.settings).where(Tenant.id == tid)) or {})


@router.get("/options")
async def get_options(
    admin: User = Depends(platform_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    cur = rg.settings_of(await _settings(db, admin.tenant_id))
    return {**cur, "half_methods": list(rg.HALF_METHODS)}


@router.put("/options")
async def put_options(
    body: OptionsIn,
    request: Request,
    admin: User = Depends(platform_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    await enforce(res.redis, "procurement:options", str(admin.id), 60, 3600)
    t = await db.scalar(select(Tenant).where(Tenant.id == admin.tenant_id).with_for_update())
    if t is None:
        raise NotFound("Tenant not found")
    ts = dict(t.settings or {})
    before = rg.settings_of(ts)
    patch = body.model_dump(exclude_none=True)
    new = rg.settings_of({rg.SETTINGS_KEY: {**before, **patch}})
    if new != before:
        t.settings = {**ts, rg.SETTINGS_KEY: new}
        await audit(
            db,
            tenant_id=admin.tenant_id,
            action="procurement.options_updated",
            actor_id=admin.id,
            actor_role=admin.role,
            target="tenant:procurement_options",
            before=before,
            after=new,
            ip=client_ip(request),
        )
        await db.commit()
    return {**new, "half_methods": list(rg.HALF_METHODS)}


@router.get("/regions")
async def regions(
    admin: User = Depends(platform_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
    product: Literal["gpt", "claude", "grok"] = Query("gpt"),
) -> dict:
    await enforce(res.redis, "procurement:regions", str(admin.id), 60, 600)
    allow = set(rg.settings_of(await _settings(db, admin.tenant_id))["region_allowlist"])
    tid = admin.tenant_id
    await db.rollback()
    rows = await vendor_regions(res, tid, product)
    if rows is None:
        return {"product": product, "available": False, "regions": []}
    return {
        "product": product,
        "available": True,
        "regions": [{**r, "allowed": r["country"] in allow} for r in rows],
    }


# --------------------------------------------------------------------------- generator
MAX_VARIANTS = 50


class GenerateIn(BaseModel):
    """Plans x regions for ONE product through ONE supplier (issue #27, review-v12 §20.3)."""

    model_config = ConfigDict(extra="forbid")
    product_id: uuid.UUID
    supplier_id: uuid.UUID
    vendor_product: Literal["gpt", "claude", "grok"] = "gpt"
    plans: list[str] = Field(min_length=1, max_length=12)
    regions: list[str] = Field(min_length=1, max_length=10)
    card_id: int | None = Field(None, ge=1, le=10**12)  # upstream_direct: the funding card
    cost_estimate: str | None = Field(None, pattern=r"^[0-9]{1,9}(\.[0-9]{1,6})?$")
    # per-region cost (a PH plan does not cost what a US plan costs); falls back to cost_estimate
    cost_estimates: dict[str, str] | None = Field(None, max_length=10)


def _plan_name(plan: str, region: str) -> dict[str, str]:
    label = f"{plan.replace('_', ' ').title()} · {region}"
    return {"en": label, "zh-CN": label}


@router.post("/generate-variants")
async def generate_variants(
    body: GenerateIn,
    request: Request,
    admin: User = Depends(platform_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    """Create one variant per missing (plan, region) with ONE source each. Idempotent: existing
    combinations are kept untouched. Prices are NOT set (``price_set`` is false, the product's
    price is only a placeholder, variant archived, source disabled): the variant cannot be
    activated until staff set a price explicitly that covers the per-region cost estimate; then
    check the source. Regions must be in the owner allowlist AND currently offered by the vendor;
    a region that is not is reported in ``skipped`` and nothing is forced."""
    import re

    from app.modules.admin.procurement_sources import _direct_checks, _valid_params
    from app.modules.admin.variants import cost_floor_minor

    await enforce(res.redis, "procurement:generate", str(admin.id), 20, 3600)
    tid, uid, role = admin.tenant_id, admin.id, admin.role  # plain values: rollback expires ORM
    product = await db.scalar(
        select(Product).where(Product.id == body.product_id, Product.tenant_id == tid)
    )
    if product is None:
        raise NotFound("Product not found")
    kind = {"upstream_cdk": "cdk", "upstream_direct": "direct"}.get(product.delivery_mode)
    if kind is None:
        raise Validation("Only CDK and direct top-up products have plan x region variants",
                         code="bad_mode")  # fmt: skip
    if kind == "cdk" and body.vendor_product != "gpt":
        raise Validation("CDK codes are for ChatGPT plans only", code="cdk_gpt_only")
    if kind == "direct" and body.card_id is None:
        raise Validation("A funding card id is required for direct top-ups", code="card_required")
    supplier = await db.scalar(
        select(SupplierAccount).where(
            SupplierAccount.id == body.supplier_id, SupplierAccount.tenant_id == tid
        )
    )
    if supplier is None or supplier.adapter != "zovocard":
        raise NotFound("Supplier not found")
    plan_re, region_re = re.compile(r"^[a-z0-9_]{1,40}$"), re.compile(r"^[A-Z]{2}$")
    plans = list(dict.fromkeys(body.plans))
    regions = list(dict.fromkeys(body.regions))
    if not all(plan_re.fullmatch(p) for p in plans) or not all(
        region_re.fullmatch(r) for r in regions
    ):
        raise Validation("Invalid plan or region", code="bad_params")
    for r, c in (body.cost_estimates or {}).items():
        if not region_re.fullmatch(r) or not re.fullmatch(r"^[0-9]{1,9}(\.[0-9]{1,6})?$", c):
            raise Validation("Invalid cost estimate", code="bad_params")
    allow = set(rg.settings_of(await _settings(db, tid))["region_allowlist"])
    supplier_id, supplier_env = supplier.id, supplier.env
    await db.rollback()
    offered = await vendor_regions(res, tid, body.vendor_product)
    if offered is None:
        raise Unavailable("The vendor's region list cannot be read now", code="regions_unavailable")
    currency_of = {r["country"]: r["currency"] for r in offered}
    skipped: list[dict[str, str]] = []
    usable: list[str] = []
    for r in regions:
        if r not in allow:
            skipped.append({"region": r, "reason": "not_allowed"})
        elif r not in currency_of:
            skipped.append({"region": r, "reason": "not_offered"})
        else:
            usable.append(r)
    supplier = await db.scalar(select(SupplierAccount).where(SupplierAccount.id == supplier_id))
    product = await db.scalar(
        select(Product)
        .where(Product.id == body.product_id, Product.tenant_id == tid)
        .with_for_update()
    )
    assert supplier is not None and product is not None
    rows = list(
        await db.scalars(
            select(ProductVariant).where(
                ProductVariant.tenant_id == tid, ProductVariant.product_id == product.id
            )
        )
    )
    from app.core import costmodel as cm

    cost_settings = cm.from_tenant(await _settings(db, tid))
    have = {(v.options["plan"], v.options["region"]) for v in rows if isinstance(v.options, dict)}
    created: list[dict[str, str]] = []
    kept: list[dict[str, str]] = []
    n = len(rows)
    for plan in plans:
        for region in usable:
            if (plan, region) in have:
                kept.append({"plan": plan, "region": region})
                continue
            params: dict[str, Any] = {"plan": plan, "product": body.vendor_product}
            if kind == "direct":
                params["card_id"] = body.card_id
            if body.vendor_product == "gpt":
                params["payment_country"] = region
                if kind == "direct":
                    params["payment_currency"] = currency_of[region]
            if not _valid_params(kind, params):
                raise Validation("These parameters cannot be sent to a supplier", code="bad_params")
            if n >= MAX_VARIANTS:
                raise Validation(f"at most {MAX_VARIANTS} variants per product", code="too_many")
            await _direct_checks(db, res, kind, params, supplier, enabling=False)
            raw_cost = (body.cost_estimates or {}).get(region) or body.cost_estimate
            cost = Decimal(raw_cost) if raw_cost else None
            # floor = the converted cost estimate (rounded up): quotes use the floor, so a quote
            # can never go below the cost; no FX rate yet = 0 (staff set it before activation)
            floor = (
                cost_floor_minor(cost_settings, cost, supplier.currency, product.currency)
                if cost is not None
                else None
            )
            v = ProductVariant(
                tenant_id=tid,
                product_id=product.id,
                name_i18n=_plan_name(plan, region),
                price_minor=product.price_minor,  # placeholder: archived until staff price it
                floor_price_minor=floor or 0,
                sort=n,
                status="archived",  # staff set the price, then activate
                price_set=False,  # activation is refused until a price is set explicitly
                options={"plan": plan, "region": region},
                sold=0,
            )
            db.add(v)
            await db.flush()
            offer = SupplierOffer(
                id=uuid.uuid4(),
                tenant_id=tid,
                supplier_id=supplier_id,
                product_id=product.id,
                variant_id=v.id,
                kind=kind,
                params=params,
                cost_estimate=cost,
                currency=supplier.currency,
                priority=100,
                enabled=False,
            )
            db.add(offer)
            await db.flush()
            await audit(
                db,
                tenant_id=tid,
                action="variant.generated",
                actor_id=uid,
                actor_role=role,
                target=f"variant:{v.id}",
                after={
                    "options": v.options,
                    "status": v.status,
                    "price_minor": product.price_minor,
                    "price_set": False,
                },
                ip=client_ip(request),
                detail={
                    "product_id": str(product.id),
                    "offer_id": str(offer.id),
                    "supplier_id": str(supplier_id),
                    "supplier_env": supplier_env,
                },
            )
            n += 1
            created.append({"plan": plan, "region": region, "variant_id": str(v.id)})
    await db.commit()
    return {"created": created, "kept": kept, "skipped": skipped}


# --------------------------------------------------------------------------- manual checks
class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool


@router.get("/half-checks")
async def list_half_checks(
    admin: User = Depends(platform_admin),
    db: AsyncSession = Depends(get_db),
    status: Literal[
        "manual_pending", "manual_approved", "manual_refused", "all"
    ] = "manual_pending",
) -> dict:
    """Manual half-price checks (newest first) with the order they hold, if any."""
    from app.modules.orders.halfcheck import MANUAL_SLA, OPEN

    q = (
        select(*OPEN, Order.order_no, Order.status.label("order_status"))
        .outerjoin(Order, Order.id == HalfPriceCheck.order_id)
        .where(HalfPriceCheck.tenant_id == admin.tenant_id, HalfPriceCheck.method == "manual")
        .order_by(HalfPriceCheck.created_at.desc())
        .limit(200)
    )
    if status != "all":
        q = q.where(HalfPriceCheck.status == status)
    rows = (await db.execute(q)).all()
    now = datetime.now(timezone.utc)
    return {
        "items": [
            {
                "id": str(r.id),
                "status": r.status,
                "plan": r.plan,
                "product_id": str(r.product_id),
                "variant_id": str(r.variant_id) if r.variant_id else None,
                "order_id": str(r.order_id) if r.order_id else None,
                "order_no": r.order_no,
                "order_status": r.order_status,
                "created_at": r.created_at.isoformat(),
                "decided_at": r.decided_at.isoformat() if r.decided_at else None,
                # a manual check waits for staff: how long, and past the SLA (a PAID order held)
                "waiting_minutes": int((now - r.created_at).total_seconds() // 60)
                if r.status == "manual_pending"
                else None,
                "overdue": r.status == "manual_pending"
                and r.order_id is not None
                and now - r.created_at > MANUAL_SLA,
            }
            for r in rows
        ]
    }


@router.post("/half-checks/{check_id}/decision")
async def decide_half_check(
    check_id: uuid.UUID,
    body: DecisionIn,
    request: Request,
    admin: User = Depends(platform_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    """Approve or refuse a ``manual`` check. Approve, then retry the held procurement job; refuse,
    then refund the order through the normal refund action."""
    from app.modules.orders import halfcheck

    await enforce(res.redis, "procurement:halfdecide", str(admin.id), 60, 3600)
    return await halfcheck.decide(db, admin, check_id, body.approve, client_ip(request))
