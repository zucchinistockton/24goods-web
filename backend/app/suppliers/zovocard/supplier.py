"""ZovoCard supplier adapter: read-only operations (probe, balance, availability, plans, card
products) and, since M2, CDK issue / reconcile / disable and webhook verification.

Parsers are built from the shapes of live responses (review-v12 §11): ``data.plans{}`` is keyed
by plan key and camelCase; ``data.registry[]`` is snake_case. Amounts that arrive as JSON floats
are turned into ``Decimal(str(x))``.
"""

from __future__ import annotations

import logging
import re
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
    LedgerRow,
    Money,
    OfferSpec,
    Outcome,
    ProbeReport,
    Result,
    SupplierError,
    SupplierEvent,
    WebhookRejected,
    dec,
)
from app.suppliers.zovocard.client import ZovoClient
from app.suppliers.zovocard.webhook import mask_pan, parse_webhook

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


def parse_time(value: Any) -> datetime | None:
    """ISO-8601 (``Z`` or offset) or ``YYYY-MM-DD HH:MM:SS`` (UTC) -> aware datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc)


def parse_balance_logs(data: Any) -> tuple[list[LedgerRow], int | None]:
    """``data.list[]`` of ``GET /balance-logs`` -> rows (newest first as sent). Rows without an
    id or a finite amount are skipped; a full ``card_number`` becomes ``mask_pan`` here and the
    remark (free text that carries the full PAN) is dropped."""
    if not isinstance(data, Mapping) or not isinstance(data.get("list"), list):
        raise SupplierError("malformed balance-logs payload", outcome=Outcome.RETRYABLE)
    rows: list[LedgerRow] = []
    for item in data["list"]:
        if not isinstance(item, Mapping):
            continue
        rid = item.get("id")
        if isinstance(rid, bool) or not isinstance(rid, (int, str)) or not str(rid).strip():
            continue
        try:
            amount = dec(item.get("amount"))
        except ValueError:
            continue
        after = _dec_or_none(item.get("after"))
        ref = item.get("ref_id")
        rows.append(
            LedgerRow(
                external_id=str(rid).strip()[:64],
                type=str(item.get("type") or "unknown")[:48],
                amount=amount,
                currency=str(item.get("currency") or "")[:3].upper(),
                occurred_at=parse_time(item.get("created_at")),
                balance_after=after,
                ref_id=str(ref)[:64] if isinstance(ref, (int, str)) and ref != "" else None,
                card_mask=mask_pan(item["card_number"]) if item.get("card_number") else None,
            )
        )
    total = data.get("total")
    return rows, total if isinstance(total, int) and not isinstance(total, bool) else None


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

    def __init__(self, code: str, client: ZovoClient, *, webhook_secret: str = "") -> None:
        self.code = code
        self.webhook_secret = webhook_secret
        self.client = client
        self.env = client.config.env
        self._plans: dict[str, tuple[float, PlanCatalog]] = {}

    async def aclose(self) -> None:
        await self.client.aclose()

    # ---- read-only operations (M1)
    async def balance(self) -> Balance:
        resp = await self.client.get("/balance")
        return parse_balance(resp.data)

    async def balance_logs(self, page: int = 1, page_size: int = 100) -> list[LedgerRow]:
        """``GET /balance-logs`` (read-only), newest first. Backfill for the cost ledger."""
        resp = await self.client.get(
            "/balance-logs", params={"page": str(page), "page_size": str(page_size)}
        )
        rows, _total = parse_balance_logs(resp.data)
        return rows

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

    async def redemption_cost(self, offer: OfferSpec) -> Money | None:
        """Reserve the breaker holds back per issued CDK (review-v12 §6, §12 second pass): the
        plan's ``expectedAmountMinor`` plus the balance buffer (``balanceBufferBps``, at least
        ``minBalanceBufferUsdMinor``) plus ``serviceFeeUsdMinor`` from the cached plans, only when
        the plan currency is USD. Otherwise ``None``: the per-offer configured cost applies."""
        product = offer.params.get("product", "gpt")
        key = offer.params.get("plan")
        if product not in DIRECT_PRODUCTS or not isinstance(key, str):
            return None
        catalog = await self.plans(product)
        entry = catalog.registry.get(key)
        plan = catalog.plans.get(entry.acc_plan_key if entry and entry.acc_plan_key else key)
        if not isinstance(plan, Mapping) or str(plan.get("currency", "")).upper() != "USD":
            return None
        expected = _int_or_none(plan.get("expectedAmountMinor"))
        if expected is None or expected < 0:
            return None
        bps = _int_or_none(plan.get("balanceBufferBps")) or 0
        floor = _int_or_none(plan.get("minBalanceBufferUsdMinor")) or 0
        fee = _int_or_none(plan.get("serviceFeeUsdMinor")) or 0
        buffer = max((Decimal(expected) * bps / 10000).to_integral_value(), Decimal(floor))
        minor = Decimal(expected) + buffer + Decimal(fee)
        return Money((minor / 100).quantize(Decimal("0.01")), "USD")

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

    # ---- writes (M2: upstream_cdk only; vcc/direct arrive in M3)
    async def submit(self, job: JobSpec) -> Result:
        """``POST /gpt-direct/cdks`` with ``count: 1`` under the job's idempotency key."""
        body = cdk_issue_body(job.offer)
        if body is None:
            return Result(Outcome.FATAL, error_code="invalid_offer")
        try:
            resp = await self.client.request(
                "POST",
                "/gpt-direct/cdks",
                json_body=body,
                idempotency_key=job.idempotency_key,
            )
        except SupplierError as exc:
            return Result.from_error(exc)
        return parse_cdk_issue(resp.data)

    async def query(self, job: JobSpec) -> Result:
        """Reconcile an issue whose outcome is unknown: same-key replay of the SAME body.

        Vendor §4 / A.1: a request that already succeeded is answered from the idempotency
        cache (``Idempotent-Replayed: true``, same codes); one that failed or never arrived is
        executed now, exactly once; one still running answers 409 (AMBIGUOUS again). Every path
        ends with at most one issued code per key. [I] The vendor list (``GET /gpt-direct/cdks``)
        is not used: it is not documented to expose the idempotency key, so it cannot correlate
        a code with a job (spec §14.3, to confirm in the sandbox).
        """
        return await self.submit(job)

    async def void(self, job: JobSpec) -> Result:
        """Disable an unused code (``POST /gpt-direct/cdks/{id}/disable``). The issue fee is not
        refunded; ``409 CDK_DISABLE_REJECTED`` = already used or unknown -> FATAL (cannot void)."""
        ref = job.upstream_ref or ""
        if not ref.isdigit() or len(ref) > 20:
            return Result(Outcome.FATAL, error_code="invalid_upstream_ref")
        try:
            resp = await self.client.request(
                "POST",
                f"/gpt-direct/cdks/{ref}/disable",
                json_body={},
                idempotency_key=job.idempotency_key,
            )
        except SupplierError as exc:
            return Result.from_error(exc)
        data = resp.data if isinstance(resp.data, Mapping) else {}
        status = data.get("status")
        if status not in (None, "disabled"):
            return Result(Outcome.AMBIGUOUS, error_code="unexpected_status", upstream_ref=ref)
        return Result(Outcome.OK, upstream_ref=ref)

    def verify_webhook(self, raw: bytes, headers: Mapping[str, str]) -> list[SupplierEvent]:
        if not self.webhook_secret:
            raise WebhookRejected("no webhook secret configured")
        return parse_webhook(raw, headers, self.webhook_secret)


_PLAN_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")


def cdk_issue_body(offer: OfferSpec) -> dict[str, Any] | None:
    """The one request body for a CDK issue, validated BEFORE any call (deterministic, so a
    replay sends byte-identical JSON and ``request_hash`` can pin it)."""
    if offer.kind != "cdk" or offer.params.get("product", "gpt") != "gpt":
        return None
    plan = offer.params.get("plan")
    if not isinstance(plan, str) or not _PLAN_RE.fullmatch(plan):
        return None
    body: dict[str, Any] = {"plan": plan, "count": 1, "funding_confirmed": True}
    country = offer.params.get("payment_country")
    if country is not None:
        if not isinstance(country, str) or not _COUNTRY_RE.fullmatch(country):
            return None
        body["payment_country"] = country
    return body


def parse_cdk_issue(data: Any) -> Result:
    """``data.issued[]`` with exactly one ``{id, code, fee_amount_minor}``. Anything else after
    a 2xx means "something happened that we cannot read": AMBIGUOUS (reconcile by replay)."""
    issued = data.get("issued") if isinstance(data, Mapping) else None
    if not isinstance(issued, list) or len(issued) != 1 or not isinstance(issued[0], Mapping):
        return Result(Outcome.AMBIGUOUS, error_code="malformed_issue")
    item = issued[0]
    code, ref = item.get("code"), item.get("id")
    if not isinstance(code, str) or not (4 <= len(code) <= 256) or not code.isprintable():
        return Result(Outcome.AMBIGUOUS, error_code="malformed_issue")
    if isinstance(ref, bool) or not isinstance(ref, (int, str)) or not str(ref).isdigit():
        return Result(Outcome.AMBIGUOUS, error_code="malformed_issue")
    fee = item.get("fee_amount_minor")
    cost = None
    if isinstance(fee, int) and not isinstance(fee, bool) and fee >= 0:
        cost = Money((Decimal(fee) / 100).quantize(Decimal("0.01")), "USD")
    return Result(Outcome.OK, upstream_ref=str(ref), secrets=(code.strip(),), cost=cost)
