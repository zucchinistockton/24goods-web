"""ZovoCard supplier adapter. M1: read-only operations only (probe, balance, availability, plan
and card-product listing). ``submit`` / ``query`` / ``void`` / ``verify_webhook`` arrive in M2.

Parsers are built from the shapes of live responses (review-v12 §11): ``data.plans{}`` is keyed
by plan key and camelCase; ``data.registry[]`` is snake_case. Amounts that arrive as JSON floats
are turned into ``Decimal(str(x))``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.suppliers.base import (
    Availability,
    Balance,
    Capability,
    JobSpec,
    OfferSpec,
    Outcome,
    ProbeReport,
    Result,
    SupplierError,
    SupplierEvent,
    dec,
)
from app.suppliers.zovocard.client import ZovoClient

log = logging.getLogger("app.suppliers.zovocard")

DIRECT_PRODUCTS = ("gpt", "claude", "grok")  # products with a plans catalogue (``x`` has none)
PLANS_TTL_S = 60.0


@dataclass(frozen=True)
class RegistryEntry:
    key: str
    product: str | None
    acc_plan_key: str | None
    purchasable: Any  # kept raw: availability needs an identity check against True
    label: str | None = None
    checkout_currency: str | None = None
    checkout_amount_minor: int | None = None
    service_fee_usd_minor: int | None = None


@dataclass(frozen=True)
class PlanCatalog:
    product: str
    version: int | None
    plans: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    registry: Mapping[str, RegistryEntry] = field(default_factory=dict)
    regions: tuple[tuple[str, str], ...] = ()

    def is_purchasable(self, key: str) -> bool:
        return is_purchasable(self, key)

    def purchasable_keys(self) -> list[str]:
        return [k for k in self.registry if is_purchasable(self, k)]


@dataclass(frozen=True)
class CardProduct:
    product_code: str
    issuer: str | None
    network: str | None
    issuing_area: str | None
    open_fee: Decimal | None
    min_amount: Decimal | None
    max_amount: Decimal | None
    enabled: Any  # raw; usable only when it is exactly True


def is_purchasable(catalog: PlanCatalog, key: str) -> bool:
    """review-v12 §5/§11: ``registry[key].purchasable is True and
    plans[registry[key].acc_plan_key].enabled is True``. Strict identity checks: a missing field,
    ``1``, ``"true"`` or ``None`` all mean unavailable."""
    entry = catalog.registry.get(key)
    if entry is None or entry.purchasable is not True:
        return False
    if not isinstance(entry.acc_plan_key, str) or not entry.acc_plan_key:
        return False
    plan = catalog.plans.get(entry.acc_plan_key)
    if not isinstance(plan, Mapping):
        return False
    return plan.get("enabled") is True


def _int_or_none(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _str_or_none(v: Any, limit: int = 64) -> str | None:
    return v[:limit] if isinstance(v, str) and v else None


def parse_plans(product: str, data: Any) -> PlanCatalog:
    if not isinstance(data, Mapping):
        raise SupplierError("malformed plans payload", outcome=Outcome.RETRYABLE)
    plans_raw = data.get("plans")
    plans: dict[str, Mapping[str, Any]] = {}
    if isinstance(plans_raw, Mapping):
        for k, v in plans_raw.items():
            if isinstance(k, str) and isinstance(v, Mapping):
                plans[k] = dict(v)
    registry: dict[str, RegistryEntry] = {}
    reg_raw = data.get("registry")
    if isinstance(reg_raw, list):
        for r in reg_raw:
            if not isinstance(r, Mapping) or not isinstance(r.get("key"), str):
                continue
            key = r["key"]
            if key in registry:  # duplicate keys: refuse to guess which one is right
                registry[key] = RegistryEntry(key, None, None, None)
                continue
            registry[key] = RegistryEntry(
                key=key,
                product=_str_or_none(r.get("product")),
                acc_plan_key=_str_or_none(r.get("acc_plan_key")),
                purchasable=r.get("purchasable"),
                label=_str_or_none(r.get("label"), 80),
                checkout_currency=_str_or_none(r.get("checkout_currency"), 8),
                checkout_amount_minor=_int_or_none(r.get("checkout_amount_minor")),
                service_fee_usd_minor=_int_or_none(r.get("service_fee_usd_minor")),
            )
    regions: list[tuple[str, str]] = []
    reg = data.get("payment_regions")
    if isinstance(reg, list):
        for item in reg:
            if isinstance(item, Mapping):
                c, cur = item.get("country"), item.get("currency")
                if isinstance(c, str) and isinstance(cur, str):
                    regions.append((c[:4], cur[:8]))
    return PlanCatalog(
        product=product,
        version=_int_or_none(data.get("version")),
        plans=plans,
        registry=registry,
        regions=tuple(regions),
    )


def parse_balance(data: Any) -> Balance:
    if not isinstance(data, Mapping):
        raise SupplierError("malformed balance payload", outcome=Outcome.RETRYABLE)
    try:
        spendable = dec(data.get("spendable_balance"))
    except ValueError:
        raise SupplierError("balance lacks spendable_balance", outcome=Outcome.RETRYABLE) from None
    total: Decimal | None
    try:
        total = dec(data.get("balance"))
    except ValueError:
        total = None
    currency = data.get("currency")
    if not isinstance(currency, str) or not (1 <= len(currency) <= 8):
        currency = "USD"
    return Balance(spendable=spendable, currency=currency.upper(), total=total)


def _dec_or_none(v: Any) -> Decimal | None:
    try:
        return dec(v)
    except ValueError:
        return None


def parse_card_products(data: Any) -> list[CardProduct]:
    items = data if isinstance(data, list) else []
    out: list[CardProduct] = []
    for p in items:
        if not isinstance(p, Mapping) or not isinstance(p.get("product_code"), str):
            continue
        out.append(
            CardProduct(
                product_code=p["product_code"][:64],
                issuer=_str_or_none(p.get("issuer"), 16),
                network=_str_or_none(p.get("network"), 32),
                issuing_area=_str_or_none(p.get("issuing_area"), 64),
                open_fee=_dec_or_none(p.get("open_fee")),
                min_amount=_dec_or_none(p.get("min_amount")),
                max_amount=_dec_or_none(p.get("max_amount")),
                # the documented /products shape has no ``enabled``: listed means openable
                enabled=p.get("enabled", True),
            )
        )
    return out


class ZovoCardSupplier:
    capabilities = frozenset(
        {Capability.CDK, Capability.VCC, Capability.DIRECT, Capability.VOID, Capability.WEBHOOK}
    )

    def __init__(self, code: str, client: ZovoClient) -> None:
        self.code = code
        self.client = client
        self.env = client.config.env
        self._plans: dict[str, tuple[float, PlanCatalog]] = {}

    async def aclose(self) -> None:
        await self.client.aclose()

    # ---- read-only operations (M1)
    async def balance(self) -> Balance:
        resp = await self.client.get("/balance")
        return parse_balance(resp.data)

    async def plans(self, product: str, *, fresh: bool = False) -> PlanCatalog:
        if product not in DIRECT_PRODUCTS:
            raise ValueError("unknown direct product")
        hit = self._plans.get(product)
        now = time.monotonic()
        if hit is not None and not fresh and now - hit[0] < PLANS_TTL_S:
            return hit[1]
        resp = await self.client.get("/gpt-direct/plans", params={"product": product})
        catalog = parse_plans(product, resp.data)
        self._plans[product] = (now, catalog)
        return catalog

    async def card_products(self) -> list[CardProduct]:
        resp = await self.client.get("/products")
        return parse_card_products(resp.data)

    async def availability(self, offer: OfferSpec) -> Availability:
        now = datetime.now(timezone.utc)
        if offer.kind not in ("cdk", "direct"):
            return Availability(False, "kind_not_supported_m1", checked_at=now)
        product = offer.params.get("product", "gpt")
        key = offer.params.get("plan")
        if product not in DIRECT_PRODUCTS or not isinstance(key, str) or not key:
            return Availability(False, "invalid_offer", checked_at=now)
        if offer.kind == "cdk" and product != "gpt":
            # stricter reading of the docs (A.5 "CDK supports GPT plans only")
            return Availability(False, "cdk_gpt_only", checked_at=now)
        try:
            catalog = await self.plans(product)
        except SupplierError as exc:
            return Availability(False, exc.reason, checked_at=now)
        if not is_purchasable(catalog, key):
            return Availability(False, "not_purchasable", checked_at=now)
        return Availability(True, checked_at=now)

    async def probe(self) -> ProbeReport:
        """auth -> IP allowlist -> balance -> direct plans -> card products. GET only."""
        now = datetime.now(timezone.utc)
        errors: list[str] = []
        try:
            bal = await self.balance()
        except SupplierError as exc:
            auth_ok: bool | None = None
            ip_allowed: bool | None = None
            if exc.status == 401:
                auth_ok = False
            elif exc.status == 403 and exc.error_code is None:
                auth_ok, ip_allowed = True, False  # key accepted, caller IP refused
            reason = "ip_not_allowlisted" if ip_allowed is False else exc.reason
            if exc.status == 401:
                reason = "auth_failed"
            return ProbeReport(
                supplier=self.code,
                env=self.env,
                ok=False,
                auth_ok=auth_ok,
                ip_allowed=ip_allowed,
                errors=(reason,),
                checked_at=now,
            )
        permissions: dict[str, bool] = {}
        purchasable = 0
        direct_ok = False
        for product in DIRECT_PRODUCTS:
            try:
                catalog = await self.plans(product, fresh=True)
            except SupplierError as exc:
                errors.append(f"plans_{product}:{exc.reason}")
                continue
            direct_ok = True
            purchasable += len(catalog.purchasable_keys())
        permissions["direct"] = direct_ok
        cards: int | None = None
        try:
            products = await self.card_products()
            cards = sum(1 for p in products if p.enabled is True)
            permissions["cards"] = True
        except SupplierError as exc:
            permissions["cards"] = False
            errors.append(f"products:{exc.reason}")
        return ProbeReport(
            supplier=self.code,
            env=self.env,
            ok=not errors,
            auth_ok=True,
            ip_allowed=True,
            permissions=permissions,
            balance=bal,
            purchasable_plans=purchasable if direct_ok else None,
            card_products=cards,
            errors=tuple(errors),
            checked_at=now,
        )

    # ---- writes and webhooks: M2 / M3
    async def submit(self, job: JobSpec) -> Result:
        raise NotImplementedError("ZovoCard submit arrives in M2")

    async def query(self, job: JobSpec) -> Result:
        raise NotImplementedError("ZovoCard query arrives in M2")

    async def void(self, job: JobSpec) -> Result:
        raise NotImplementedError("ZovoCard void arrives in M2")

    def verify_webhook(self, raw: bytes, headers: Mapping[str, str]) -> list[SupplierEvent]:
        raise NotImplementedError("ZovoCard webhooks arrive in M2")
