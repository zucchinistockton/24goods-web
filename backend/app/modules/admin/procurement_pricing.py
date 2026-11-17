"""Cost model and price recommendations (issue #28, review-v12 §19). Admins only (the same rule
as the cost report); staff-only data: no buyer-facing endpoint reads any of it.

* ``GET/PUT /settings``: the fee schedule + FX rate + default target margin (audited).
* ``PUT /target``: a per product / variant target margin override (audited).
* ``GET /units``: per sellable unit and supplier source, the estimate with every component,
  gross / net margin and the recommended price. Estimates only: nothing here changes a price.
* ``POST /apply``: the staff action "apply". It recomputes the recommendation on the server (the
  client names the unit, never an amount, AGENTS.md §1.3) and then goes through the normal
  audited product / variant price edit.
* ``GET /actual`` and ``/actual.csv``: per order, from the supplier ledger (``procurement_cost``)
  plus the gateway fee; rows the ledger cannot fully explain are ``cost_incomplete``.

Every query filters by the caller's tenant. Amounts are ``Decimal`` strings next to a currency.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import costmodel as cm
from app.core.audit import audit
from app.core.deps import Page, client_ip, get_db, get_page, get_res, require_admin
from app.core.errors import Conflict, NotFound, Validation
from app.core.money import percent_of, to_decimal
from app.core.ratelimit import enforce
from app.core.resources import Resources
from app.models import (
    AfterSales,
    Order,
    Payment,
    Product,
    ProductVariant,
    SupplierOffer,
    Tenant,
    User,
)
from app.modules.admin import procurement_cost as cost_report
from app.modules.admin import variants as variant_svc
from app.modules.admin.products import patch as product_patch
from app.modules.admin.schemas import ProductPatch, VariantPatch
from app.modules.admin.service import CSV_MAX_ROWS, _csv_cell
from app.modules.payments.channels import entry_cfg, provider

router = APIRouter(prefix="/admin/procurement/pricing", tags=["admin"])

MAX_ROWS_PER_PAGE = 1000


class FxIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    per_usd: str = Field(pattern=r"^[0-9]{1,6}(\.[0-9]{1,6})?$")
    confirm: bool = False  # needed for a move of more than ``fx_max_change``


class SettingsIn(BaseModel):
    """Partial update: only the fields sent change. Amounts are USD decimal strings, rates are
    fractions (0.02 = 2 %)."""

    model_config = ConfigDict(extra="forbid")
    card_open_fee: str | None = Field(None, max_length=16)
    topup_fee_rate: str | None = Field(None, max_length=16)
    topup_fee_fixed: str | None = Field(None, max_length=16)
    failed_fee: str | None = Field(None, max_length=16)
    failed_free_count: int | None = None
    auth_reversal_rate: str | None = Field(None, max_length=16)
    chargeback_tier1_rate: str | None = Field(None, max_length=16)
    chargeback_tier1_min_tx: int | None = None
    chargeback_tier1_surcharge: str | None = Field(None, max_length=16)
    chargeback_tier2_rate: str | None = Field(None, max_length=16)
    chargeback_tier2_deposit: str | None = Field(None, max_length=16)
    expected_failure_rate: str | None = Field(None, max_length=16)
    target_margin: str | None = Field(None, max_length=16)
    round_step_minor: int | None = None
    service_fee_direct: dict[str, str] | None = None
    uses_per_card: dict[str, int] | None = None
    fx_bands: dict[str, dict[str, str]] | None = None
    fx_max_age_days: int | None = None
    fx_max_change: str | None = Field(None, max_length=16)
    fx: FxIn | None = None


class TargetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: uuid.UUID
    variant_id: uuid.UUID | None = None
    margin: str | None = Field(None, max_length=16)  # null clears the override


class ApplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: uuid.UUID
    variant_id: uuid.UUID | None = None
    offer_id: uuid.UUID | None = None
    current_minor: int = Field(ge=0, le=10**12)  # the price the staff member saw
    recommended_minor: int = Field(ge=1, le=10**12)  # the recommendation the staff member saw
    fingerprint: str = Field(pattern=r"^[0-9a-f]{16}$")  # hash of the inputs that produced it
    confirm_large_drop: bool = False


def _channel(res: Resources, ts: dict[str, Any]) -> tuple[str, Decimal]:
    """Our payment gateway's fee on the entry channel (reuse of the payment settings: the zpay
    fee in config, none for self-hosted USDT)."""
    cfg = entry_cfg(ts)
    rate = Decimal(res.settings.zpay_fee_permille) / 1000 if provider(cfg.id) == "zpay" else 0
    return cfg.id, Decimal(rate)


async def _tenant_row(db: AsyncSession, tid: uuid.UUID) -> Tenant:
    t = await db.scalar(select(Tenant).where(Tenant.id == tid).with_for_update())
    if t is None:
        raise NotFound("Tenant not found")
    return t


def _view(st: dict[str, Any], channel: str, rate: Decimal) -> dict[str, Any]:
    return {
        "settings": {k: v for k, v in st.items() if k not in ("targets",)},
        "informational": list(cm.INFO_FIELDS),
        "targets": st["targets"],
        "channel": {"id": channel, "fee_rate": cm.q(rate)},
        "currency": "USD",
    }


@router.get("/settings")
async def get_settings(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    ts = dict(await db.scalar(select(Tenant.settings).where(Tenant.id == admin.tenant_id)) or {})
    ch, rate = _channel(res, ts)
    return _view(cm.from_tenant(ts), ch, rate)


@router.put("/settings")
async def put_settings(
    body: SettingsIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    await enforce(res.redis, "pricing:settings", str(admin.id), 60, 3600)
    patch = body.model_dump(exclude_none=True, exclude={"fx"})
    try:
        cm.validate_patch(patch)
    except cm.CostSettingsError as exc:
        raise Validation(str(exc), code=exc.code) from None
    tenant = await _tenant_row(db, admin.tenant_id)
    cur_ts = dict(tenant.settings or {})
    before = cm.from_tenant(cur_ts)
    new = cm.merged(before, patch)
    if body.fx is not None:
        try:
            new = cm.set_rate(
                new, body.fx.currency, body.fx.per_usd, str(admin.id), confirm=body.fx.confirm
            )
        except cm.CostSettingsError as exc:
            raise Validation(str(exc), code=exc.code) from None
    if new == before:
        ch, rate = _channel(res, cur_ts)
        return _view(new, ch, rate)
    tenant.settings = {**cur_ts, cm.SETTINGS_KEY: new}
    await audit(
        db,
        tenant_id=admin.tenant_id,
        action="pricing.settings_updated",
        actor_id=admin.id,
        actor_role=admin.role,
        target="tenant:cost_model",
        before={k: v for k, v in before.items() if k != "targets"},
        after={k: v for k, v in new.items() if k != "targets"},
        ip=client_ip(request),
        detail={"fields": sorted(patch) + (["fx"] if body.fx else [])},
    )
    await db.commit()
    ch, rate = _channel(res, tenant.settings)
    return _view(new, ch, rate)


@router.put("/target")
async def put_target(
    body: TargetIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    await enforce(res.redis, "pricing:settings", str(admin.id), 60, 3600)
    tid = admin.tenant_id
    prod = await db.scalar(
        select(Product.id).where(Product.id == body.product_id, Product.tenant_id == tid)
    )
    if prod is None:
        raise NotFound("Product not found")
    if body.variant_id is not None:
        ok = await db.scalar(
            select(ProductVariant.id).where(
                ProductVariant.id == body.variant_id,
                ProductVariant.product_id == body.product_id,
                ProductVariant.tenant_id == tid,
            )
        )
        if ok is None:
            raise NotFound("Variant not found")
    value = None
    if body.margin is not None:
        d = cm._dec(body.margin)
        if d is None or d >= 1:
            raise Validation("margin: a fraction below 1 (0.30 = 30 %)", code="bad_cost_settings")
        value = cm._s(d)
    tenant = await _tenant_row(db, tid)
    cur_ts = dict(tenant.settings or {})
    st = cm.from_tenant(cur_ts)
    key = cm.target_key(body.product_id, body.variant_id)
    old = st["targets"].get(key)
    targets = dict(st["targets"])
    if value is None:
        targets.pop(key, None)
    elif key in targets or len(targets) < cm.MAX_TARGETS:
        targets[key] = value
    else:
        raise Validation("Too many per-product targets", code="too_many_targets")
    tenant.settings = {**cur_ts, cm.SETTINGS_KEY: {**st, "targets": targets}}
    await audit(
        db,
        tenant_id=tid,
        action="pricing.target_updated",
        actor_id=admin.id,
        actor_role=admin.role,
        target=f"product:{body.product_id}",
        before={"target_margin": old},
        after={"target_margin": value},
        ip=client_ip(request),
        detail={"variant_id": str(body.variant_id) if body.variant_id else None},
    )
    await db.commit()
    return {"key": key, "margin": value}


# ---------------------------------------------------------------------------------------------
# unit estimates


def _title(i18n: Any) -> str:
    if isinstance(i18n, dict):
        return str(i18n.get("en") or next(iter(i18n.values()), ""))[:120]
    return ""


def _source(offer: SupplierOffer | None, st: dict[str, Any], cost_minor: int | None,
            currency: str) -> tuple[cm.Source, str | None]:  # fmt: skip
    """The estimate's input for one offer (or the product's manual cost when it has no offer)
    and the reason its supplier price is unknown, if it is."""
    if offer is None:
        per = cm.rate_for(st, currency)
        usd = cm.usd_from_minor(cost_minor, per) if cost_minor else None  # 0 / missing: unknown
        return cm.Source("manual", usd), (None if usd is not None else "supplier_price")
    p = offer.params or {}
    direct = p.get("product") if offer.kind == "direct" else None
    code = str(p.get("product_code") or "").lower()
    tier = "pro20x" if offer.kind == "vcc" and ("pro" in code or "20x" in code) else "standard"
    usd = None
    if offer.cost_estimate is not None and offer.cost_estimate > 0:
        per = cm.rate_for(st, offer.currency)
        usd = Decimal(offer.cost_estimate) / per if per else None
    switch = offer.kind == "direct" and p.get("vendor_switch") is True
    src = cm.Source(offer.kind, usd, direct if direct in cm.DIRECT_PRODUCTS else None, tier, switch)
    return src, (None if usd is not None else "supplier_price")


def _row(
    st: dict[str, Any],
    channel_rate: Decimal,
    p: Product,
    v: ProductVariant | None,
    offer: SupplierOffer | None,
    supplier_code: str | None,
) -> dict[str, Any]:
    price_minor = v.price_minor if v is not None else p.price_minor
    cur = p.currency
    per = cm.rate_for(st, cur)
    price_usd = cm.usd_from_minor(price_minor, per)
    src, _why = _source(offer, st, p.cost_minor, cur)
    est = cm.estimate(st, src, price_usd, channel_rate)
    margin = cm.target_margin(st, p.id, v.id if v else None)
    rec_usd = cm.recommend_usd(st, src, channel_rate, margin)
    rec: dict[str, Any] | None = None
    if rec_usd is not None and per is not None:
        minor = cm.recommend_minor(rec_usd, per, st["round_step_minor"])
        rec = {
            "amount": cm.q(to_decimal(minor), cm.CENT),
            "minor": minor,
            "currency": cur,
            "usd": cm.q(rec_usd),
            "diff_minor": minor - price_minor,
            "diff": cm.q(to_decimal(minor - price_minor), cm.CENT),
        }
    elif rec_usd is None and src.supplier_price_usd is not None and src.supplier_price_usd > 0:
        est.incomplete.append("target_unreachable")
    stale = cm.fx_stale(st, cur, datetime.now(timezone.utc))
    # what the recommendation was computed from: any change to it invalidates a shown price
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "s": {
                    k: ({c: e["per_usd"] for c, e in v_.items()} if k == "fx" else v_)
                    for k, v_ in st.items()
                    if k != "targets"
                },
                "m": str(margin),
                "g": str(channel_rate),
                "src": [src.kind, str(src.supplier_price_usd), src.direct_product, src.card_tier],
                "rec": rec["minor"] if rec else None,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]
    return {
        "key": ":".join(str(x) for x in (p.id, v.id if v else "", offer.id if offer else "")),
        "product_id": str(p.id),
        "variant_id": str(v.id) if v else None,
        "offer_id": str(offer.id) if offer else None,
        "region": None,  # reserved for #27
        "title": _title(p.title_i18n),
        "variant_name": _title(v.name_i18n) if v else None,
        "status": v.status if v else p.status,
        "source": {
            "kind": src.kind,
            "supplier": supplier_code,
            "enabled": offer.enabled if offer else None,
            "priority": offer.priority if offer else None,
        },
        "price": {
            "minor": price_minor,
            "amount": cm.q(to_decimal(price_minor), cm.CENT),
            "currency": cur,
            "usd": cm.q(price_usd),
        },
        "cost_usd": {
            "supplier_price": cm.q(est.supplier_price),
            "service_fee": cm.q(est.service_fee),
            "topup_fee": cm.q(est.topup_fee),
            "card_share": cm.q(est.card_share),
            "failure_cost": cm.q(est.failure_cost),
            "switch_cost": cm.q(est.switch_cost),
            "channel_fee": cm.q(est.channel_fee),
            "total": cm.q(est.total_cost),
        },
        "gross_usd": cm.q(est.gross),
        "net_usd": cm.q(est.net),
        "gross_margin_pct": cm.pct(est.gross_margin),
        "net_margin_pct": cm.pct(est.net_margin),
        "target_margin_pct": cm.pct(margin),
        "recommended": rec,
        "fingerprint": fingerprint,
        "fx_stale": stale,
        "large_drop": bool(rec and rec["minor"] * 2 < price_minor),
        "incomplete": est.incomplete,
        "cost_incomplete": bool(est.incomplete),
    }


async def _unit_rows(
    db: AsyncSession,
    tid: uuid.UUID,
    st: dict[str, Any],
    channel_rate: Decimal,
    products: list[Product],
) -> list[dict[str, Any]]:
    from app.models import SupplierAccount

    if not products:
        return []
    pids = [p.id for p in products]
    variants = (
        await db.scalars(
            select(ProductVariant)
            .where(
                ProductVariant.tenant_id == tid,
                ProductVariant.product_id.in_(pids),
                ProductVariant.status != "archived",
            )
            .order_by(ProductVariant.sort, ProductVariant.id)
        )
    ).all()
    offers = (
        await db.execute(
            select(SupplierOffer, SupplierAccount.code)
            .join(SupplierAccount, SupplierAccount.id == SupplierOffer.supplier_id)
            .where(
                SupplierOffer.tenant_id == tid,
                SupplierAccount.tenant_id == tid,
                SupplierOffer.product_id.in_(pids),
            )
            .order_by(SupplierOffer.priority, SupplierOffer.id)
        )
    ).all()
    by_prod: dict[uuid.UUID, list[ProductVariant]] = {}
    for v in variants:
        by_prod.setdefault(v.product_id, []).append(v)
    by_target: dict[tuple[uuid.UUID, uuid.UUID | None], list[tuple[SupplierOffer, str]]] = {}
    for o, code in offers:
        by_target.setdefault((o.product_id, o.variant_id), []).append((o, code))
    out: list[dict[str, Any]] = []
    for p in products:
        vs = by_prod.get(p.id) or [None]
        for v in vs:
            # a variant uses its own sources, else the product's (sources "per variant")
            offs = by_target.get((p.id, v.id if v else None)) or by_target.get((p.id, None)) or []
            if not offs:
                out.append(_row(st, channel_rate, p, v, None, None))
            for o, code in offs:
                out.append(_row(st, channel_rate, p, v, o, code))
    return out


@router.get("/units")
async def units(
    admin: User = Depends(require_admin),
    page: Page = Depends(get_page),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    tid = admin.tenant_id
    ts = dict(await db.scalar(select(Tenant.settings).where(Tenant.id == tid)) or {})
    st = cm.from_tenant(ts)
    _ch, rate = _channel(res, ts)
    where = [Product.tenant_id == tid, Product.archived_at.is_(None)]
    from sqlalchemy import func

    total = await db.scalar(select(func.count(Product.id)).where(*where)) or 0
    products = list(
        (
            await db.scalars(
                select(Product)
                .where(*where)
                .order_by(Product.created_at.desc(), Product.id)
                .limit(page.page_size)
                .offset(page.offset)
            )
        ).all()
    )
    rows = await _unit_rows(db, tid, st, rate, products)
    out = page.wrap(rows[:MAX_ROWS_PER_PAGE], int(total))
    out["truncated"] = len(rows) > MAX_ROWS_PER_PAGE  # never a silent cut
    out["fx"] = st["fx"]
    return out


@router.post("/apply")
async def apply(
    body: ApplyIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    """Apply the recommended price of one unit through the normal audited price edit.

    The tenant settings row and the product / variant row are locked BEFORE the comparison, and
    the standard patch runs in the same transaction: a concurrent price edit or settings change
    ends in a 409, never in a silent overwrite. The request carries the price AND the
    recommendation the staff member saw (plus a fingerprint of its inputs); the server applies
    only a recomputed recommendation that is exactly that."""
    await enforce(res.redis, "pricing:apply", str(admin.id), 60, 3600)
    tid = admin.tenant_id
    # Lock order: the row whose price changes FIRST, then the tenant settings row. A normal price
    # edit locks the product and then inserts an audit row, whose foreign key takes a KEY SHARE
    # lock on the tenant row; taking the tenant row first (FOR UPDATE) deadlocked against it.
    # populate_existing: never a stale identity-map copy.
    lock = {"populate_existing": True}
    p = await db.scalar(
        select(Product)
        .where(Product.id == body.product_id, Product.tenant_id == tid)
        .with_for_update()
        .execution_options(**lock)
    )
    if p is None or p.archived_at is not None:
        raise NotFound("Product not found")
    v = None
    if body.variant_id is not None:
        v = await db.scalar(
            select(ProductVariant)
            .where(
                ProductVariant.id == body.variant_id,
                ProductVariant.product_id == p.id,
                ProductVariant.tenant_id == tid,
            )
            .with_for_update()
            .execution_options(**lock)
        )
        if v is None:
            raise NotFound("Variant not found")
    # FOR NO KEY UPDATE: serialises against a settings change (FOR UPDATE) but not against the
    # foreign-key KEY SHARE locks every audit / order insert of this tenant takes
    tenant = await db.scalar(
        select(Tenant)
        .where(Tenant.id == tid)
        .with_for_update(key_share=True)
        .execution_options(**lock)
    )
    if tenant is None:
        raise NotFound("Tenant not found")
    ts = dict(tenant.settings or {})
    st = cm.from_tenant(ts)
    _ch, rate = _channel(res, ts)
    cands = [
        r
        for r in await _unit_rows(db, tid, st, rate, [p])
        if r["variant_id"] == (str(v.id) if v else None)
    ]
    if body.offer_id is not None:
        cands = [r for r in cands if r["offer_id"] == str(body.offer_id)]
    elif len(cands) > 1:
        raise Validation("Name the source (offer_id) to apply", code="offer_required")
    if not cands:
        raise NotFound("Source not found")
    row = cands[0]
    if row["source"]["enabled"] is False:
        raise Validation("This source is disabled", code="offer_disabled")
    rec = row["recommended"]
    if rec is None or row["cost_incomplete"]:
        raise Validation(
            "No recommendation for this unit (cost incomplete)", code="no_recommendation"
        )
    if row["fx_stale"]:
        raise Conflict("The exchange rate is out of date: update it first", code="fx_stale")
    current = row["price"]["minor"]
    if (
        current != body.current_minor
        or rec["minor"] != body.recommended_minor
        or row["fingerprint"] != body.fingerprint
    ):
        raise Conflict(
            "The price or the recommendation changed: reload", code="recommendation_changed"
        )
    if rec["minor"] == current:
        return {"applied": False, "noop": True, "price": {"minor": current, "currency": p.currency}}
    floor = v.floor_price_minor if v else p.floor_price_minor
    if rec["minor"] < floor:
        raise Validation("The recommendation is below the floor price", code="below_floor")
    if rec["minor"] * 2 < current and not body.confirm_large_drop:
        raise Validation(
            "The recommendation is more than 50 % below the current price",
            code="confirm_large_drop",
        )
    new_price = rec["amount"]
    await audit(
        db,
        tenant_id=tid,
        action="pricing.recommendation_applied",
        actor_id=admin.id,
        actor_role=admin.role,
        target=f"variant:{v.id}" if v else f"product:{p.id}",
        before={"price_minor": current, "currency": p.currency},
        after={"price_minor": rec["minor"], "currency": p.currency},
        ip=client_ip(request),
        detail={
            "offer_id": row["offer_id"],
            "target_margin_pct": row["target_margin_pct"],
            "fingerprint": row["fingerprint"],
            "large_drop_confirmed": bool(body.confirm_large_drop and rec["minor"] * 2 < current),
        },
    )
    ip = client_ip(request)
    if v is not None:
        await variant_svc.patch(db, p.id, v.id, VariantPatch(price=new_price), admin, ip)
    else:
        await product_patch(db, p.id, ProductPatch(price=new_price), admin, ip, res.settings)
    return {
        "applied": True,
        "price": {"minor": rec["minor"], "amount": new_price, "currency": p.currency},
    }


# ---------------------------------------------------------------------------------------------
# actual per order


def _dec_str(d: Decimal) -> str:
    return format(d.quantize(cm.CENT), "f")


async def _actual_items(
    db: AsyncSession,
    admin: User,
    res: Resources,
    date_from: date | None,
    date_to: date | None,
    page: Page,
) -> dict[str, Any]:
    """The existing margin report plus USD conversion, the gateway fee and the net profit. A row
    stays ``cost_incomplete`` when the ledger lacks data, a cost currency has no rate or the sale
    has no rate: nothing is guessed."""
    tid = admin.tenant_id
    ts = dict(await db.scalar(select(Tenant.settings).where(Tenant.id == tid)) or {})
    st = cm.from_tenant(ts)
    rep = await cost_report.margin(date_from, date_to, admin, page, db)
    ids = [uuid.UUID(i["order_id"]) for i in rep["items"]]
    orders: dict[uuid.UUID, Any] = {}
    pay_channel: dict[uuid.UUID, str] = {}
    adjusted: set[uuid.UUID] = set()
    if ids:
        for o in (
            await db.execute(
                select(
                    Order.id, Order.channel, Order.amount_minor, Order.fee_minor,
                    Order.reseller_markup_minor,
                ).where(Order.tenant_id == tid, Order.id.in_(ids))
            )
        ).all():  # fmt: skip
            orders[o.id] = o
        for oid, ch in (
            await db.execute(
                select(Payment.order_id, Payment.channel).where(
                    Payment.tenant_id == tid, Payment.order_id.in_(ids)
                )
            )
        ).all():
            pay_channel[oid] = ch
        # any after-sales case that is not rejected may have changed what we earned or paid
        adjusted = set(
            await db.scalars(
                select(AfterSales.order_id).where(
                    AfterSales.tenant_id == tid,
                    AfterSales.order_id.in_(ids),
                    AfterSales.status != "rejected",
                )
            )
        )
    for it in rep["items"]:
        oid = uuid.UUID(it["order_id"])
        o = orders.get(oid)
        reasons: list[str] = []
        if it["cost_incomplete"]:
            reasons.append("ledger_incomplete")
        if it["unreconciled_jobs"]:
            reasons.append("unreconciled")
        if oid in adjusted:
            reasons.append("after_sales_adjusted")
        # revenue = what the platform earned: the channel surcharge (pass-through, it exists to
        # cover the gateway) and the reseller's markup (the reseller's income) are not revenue
        paid = Decimal(it.pop("revenue"))
        it.pop("margin", None)
        net_minor = (o.amount_minor - o.fee_minor - o.reseller_markup_minor) if o else 0
        revenue = to_decimal(net_minor)
        it.update(paid=_dec_str(paid), revenue=_dec_str(revenue))
        per = cm.rate_for(st, it["revenue_currency"])
        if per is None:
            reasons.append("fx_rate")
        rev_usd = revenue / per if per else None
        channel = (o.channel if o else None) or pay_channel.get(oid)
        gw_usd: Decimal | None = None
        if channel is None:
            reasons.append("channel_unknown")  # never assume zpay
        elif per is not None:
            gw = Decimal(0)
            if provider(channel) == "zpay":
                gw = to_decimal(percent_of(net_minor, res.settings.zpay_fee_permille))
            gw_usd = gw / per
        cost_usd: Decimal | None = Decimal(0) if it["cost"] else None
        if cost_usd is None:
            reasons.append("no_cost")
        for c in it["cost"]:
            r = cm.rate_for(st, c["currency"])
            if r is None or cost_usd is None:
                cost_usd = None
                reasons.append("fx_rate")
            else:
                cost_usd += Decimal(c["amount"]) / r
        incomplete = bool(reasons)
        gross = None if incomplete or rev_usd is None else rev_usd - cost_usd  # type: ignore[operator]
        net = None if gross is None or gw_usd is None else gross - gw_usd
        it.update(
            revenue_usd=cm.q(rev_usd),
            cost_usd=cm.q(cost_usd),
            channel_fee_usd=cm.q(gw_usd),
            fee_estimated=gw_usd is not None,  # the gateway fee is not stored per payment
            gross_usd=cm.q(gross),
            net_usd=cm.q(net),
            gross_margin_pct=cm.pct(gross / rev_usd) if gross is not None and rev_usd else None,
            net_margin_pct=cm.pct(net / rev_usd) if net is not None and rev_usd else None,
            cost_incomplete=incomplete,
            reasons=sorted(set(reasons)),
        )
    return rep


@router.get("/actual")
async def actual(
    date_from: date | None = None,
    date_to: date | None = None,
    admin: User = Depends(require_admin),
    page: Page = Depends(get_page),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict:
    return await _actual_items(db, admin, res, date_from, date_to, page)


@router.get("/actual.csv")
async def actual_csv(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> Response:
    await enforce(res.redis, "procurement:export", str(admin.id), 10, 600)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["order_no", "status", "paid_at", "revenue", "currency", "revenue_usd", "cost_usd",
         "channel_fee_usd", "gross_usd", "net_usd", "net_margin_pct", "cost_incomplete",
         "reasons"]
    )  # fmt: skip
    n = 0
    for pg in range(1, CSV_MAX_ROWS // 100 + 1):
        rep = await _actual_items(db, admin, res, date_from, date_to, Page(pg, 100))
        for it in rep["items"]:
            n += 1
            w.writerow(
                [
                    _csv_cell(it["order_no"]),
                    _csv_cell(it["status"]),
                    it["paid_at"] or "",
                    it["revenue"],
                    _csv_cell(it["revenue_currency"]),
                    it["revenue_usd"] or "",
                    it["cost_usd"] or "",
                    it["channel_fee_usd"] or "",
                    it["gross_usd"] or "",
                    it["net_usd"] or "",
                    it["net_margin_pct"] or "",
                    "yes" if it["cost_incomplete"] else "no",
                    ";".join(it["reasons"]),
                ]
            )
        if pg * 100 >= rep["total"]:
            break
    await audit(
        db,
        tenant_id=admin.tenant_id,
        action="procurement.margin_exported",
        actor_id=admin.id,
        actor_role=admin.role,
        target="pricing:actual",
        detail={"rows": n},
        ip=client_ip(request),
    )
    await db.commit()
    return Response(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="actual-margins.csv"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
