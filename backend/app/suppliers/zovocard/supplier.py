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
from decimal import Decimal, InvalidOperation
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
    job_idempotency_key,
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
        self._cards: tuple[float, list[CardProduct]] | None = None

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
        if offer.kind == "vcc":
            return await self._vcc_cost(offer)
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

    async def card_products(self, *, fresh: bool = False) -> list[CardProduct]:
        hit = self._cards
        now = time.monotonic()
        if hit is not None and not fresh and now - hit[0] < PLANS_TTL_S:
            return hit[1]
        resp = await self.client.get("/products")
        products = parse_card_products(resp.data)
        self._cards = (now, products)
        return products

    async def _vcc_cost(self, offer: OfferSpec) -> Money | None:
        """What one card costs the wallet (opening amount + top-up + the catalogue's open fee)."""
        spec = vcc_spec(offer)
        if spec is None:
            return None
        prod = next(
            (p for p in await self.card_products() if p.product_code == spec.product_code), None
        )
        if prod is None or prod.open_fee is None:
            return None
        return Money(spec.init_amount + (spec.recharge_amount or Decimal(0)) + prod.open_fee, "USD")

    async def availability_fresh(self, offer: OfferSpec) -> Availability:
        """The engine's pre-check before a send: never answered from the 60 s plan cache (N3)."""
        return await self.availability(offer, fresh=True)

    async def availability(self, offer: OfferSpec, *, fresh: bool = False) -> Availability:
        now = datetime.now(timezone.utc)
        if offer.kind == "vcc":
            return await self._vcc_availability(offer, now, fresh=fresh)
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
            catalog = await self.plans(product, fresh=fresh)
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
        """``POST /gpt-direct/cdks`` with ``count: 1`` under the job's idempotency key (``vcc``:
        open -> optional recharge -> reveal, every step under its own fixed key)."""
        if job.offer.kind == "vcc":
            return await self._vcc_run(job)
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
        if job.offer.kind == "vcc":
            return await self._vcc_void(job, ref)
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

    # ---- upstream_vcc (M3): open -> optional recharge -> reveal
    async def _vcc_availability(
        self, offer: OfferSpec, now: datetime, *, fresh: bool = False
    ) -> Availability:
        spec = vcc_spec(offer)
        if spec is None:
            return Availability(False, "invalid_offer", checked_at=now)
        try:
            products = await self.card_products(fresh=fresh)
        except SupplierError as exc:
            return Availability(False, exc.reason, checked_at=now)
        prod = next((p for p in products if p.product_code == spec.product_code), None)
        if prod is None or prod.enabled is not True:
            return Availability(False, "not_purchasable", checked_at=now)
        # the catalogue bounds apply to the opening amount and to the total ever put on the card
        lo, hi = prod.min_amount, prod.max_amount
        if (lo is not None and spec.init_amount < lo) or (
            hi is not None and spec.init_amount + (spec.recharge_amount or Decimal(0)) > hi
        ):
            return Availability(False, "amount_out_of_range", checked_at=now)
        fee = prod.open_fee if prod.open_fee is not None else Decimal(0)
        cost = Money(spec.init_amount + (spec.recharge_amount or Decimal(0)) + fee, "USD")
        return Availability(True, cost_estimate=cost, checked_at=now)

    async def _vcc_run(self, job: JobSpec) -> Result:
        """The whole purchase, idempotent end to end: ``submit`` and ``query`` are the same walk.

        * ``open`` under the job's key (a replay of a 2xx answers from the vendor cache: same card);
        * the optional recharge under ``pj:<job>:recharge``. A recharge is never POSTed again once
          the card's recharge list shows a row for it (the vendor's "pending" 400 is not cached, so
          a blind replay could charge twice), nor while the engine waits on a pending one;
        * reveal through ``GET /cards/{id}``. PAN / expiry / CVV leave this method only inside
          ``Result.secrets`` (sealed by the caller at once); nothing is logged or kept.

        A closed job (``step == "done"``: refund / fail) only learns the card id (so it can be
        deleted) and returns no secret."""
        spec = vcc_spec(job.offer)
        if spec is None:
            return Result(Outcome.FATAL, error_code="invalid_offer")
        try:
            opened = await self.client.request(
                "POST",
                "/cards/open",
                json_body=spec.open_body(),
                idempotency_key=job.idempotency_key,
            )
        except SupplierError as exc:
            return Result.from_error(exc)
        card = parse_card_open(opened.data)
        if card is None:
            return Result(Outcome.AMBIGUOUS, error_code="malformed_open")
        card_id, mask, fee = card
        cost = None
        if fee is not None:
            cost = Money(spec.init_amount + (spec.recharge_amount or Decimal(0)) + fee, "USD")
        base: dict[str, Any] = {"upstream_ref": card_id, "card_mask": mask, "cost": cost}
        if job.step == "done":
            return Result(Outcome.OK, **base)
        if spec.recharge_amount is not None:
            rc = await self._vcc_recharge(job, spec, card_id, base)
            if rc is not None:
                return rc
        try:
            detail = await self.client.get(f"/cards/{card_id}")
        except SupplierError as exc:
            # the card exists: whatever the read says, this is never "nothing happened"
            outcome = Outcome.RETRYABLE if exc.outcome is Outcome.UNAVAILABLE else exc.outcome
            return Result(outcome, error_code=exc.reason, retry_after=exc.retry_after, **base)
        secret = parse_card_secret(detail.data)
        if secret is None:
            return Result(Outcome.AMBIGUOUS, error_code="malformed_card", **base)
        return Result(Outcome.OK, secrets=(secret,), **base)

    async def _vcc_recharge(
        self, job: JobSpec, spec: VccSpec, card_id: str, base: dict[str, Any]
    ) -> Result | None:
        """``None`` = recharged (continue); otherwise the Result that ends this walk."""
        try:
            listing = await self.client.get(f"/cards/{card_id}/recharges")
        except SupplierError as exc:
            return Result(
                Outcome.RETRYABLE if exc.outcome is not Outcome.FATAL else Outcome.AMBIGUOUS,
                error_code=exc.reason,
                **base,
            )
        rows = [
            r
            for r in (listing.data if isinstance(listing.data, list) else [])
            if isinstance(r, Mapping)
        ]
        if rows:
            statuses = {str(r.get("status") or "").lower() for r in rows}
            if statuses & _RECHARGE_OK:
                return None
            if statuses & _RECHARGE_FAILED and not statuses - _RECHARGE_FAILED:
                return Result(Outcome.FATAL, error_code="recharge_failed", **base)
            return Result(Outcome.ACCEPTED_PENDING, error_code="recharge_pending", **base)
        if job.step == "pending":
            # the engine already saw "result pending": never POST it again, wait for the list
            return Result(Outcome.ACCEPTED_PENDING, error_code="recharge_pending", **base)
        try:
            resp = await self.client.request(
                "POST",
                "/cards/recharge",
                json_body={"card_id": int(card_id), "amount": wire_amount(spec.recharge_amount)},
                idempotency_key=job_idempotency_key(job.job_id, "recharge"),
                endpoint="cards.recharge",
            )
        except SupplierError as exc:
            return Result(
                exc.outcome,
                error_code=exc.reason,
                retry_after=exc.retry_after,
                supplier_stop=exc.supplier_stop,
                **base,
            )
        data = resp.data if isinstance(resp.data, Mapping) else {}
        if data.get("pending") is True:
            return Result(Outcome.ACCEPTED_PENDING, error_code="recharge_pending", **base)
        return None

    async def _vcc_void(self, job: JobSpec, card_id: str) -> Result:
        """Delete the card (the vendor returns the whole remaining balance to the wallet; a
        separate refund would have to leave USD 1 on the card, so it adds nothing). Already
        ``DELETED`` = done. A ``202`` is not done: the engine asks again, and ``GET`` answers."""
        try:
            detail = await self.client.get(f"/cards/{card_id}")
        except SupplierError as exc:
            return Result.from_error(exc)
        if (
            isinstance(detail.data, Mapping)
            and str(detail.data.get("status", "")).upper() == "DELETED"
        ):
            return Result(Outcome.OK, upstream_ref=card_id)
        try:
            resp = await self.client.request(
                "DELETE", f"/cards/{card_id}", idempotency_key=job.idempotency_key
            )
        except SupplierError as exc:
            return Result.from_error(exc)
        if resp.status == 202:
            return Result(
                Outcome.ACCEPTED_PENDING, error_code="delete_pending", upstream_ref=card_id
            )
        return Result(Outcome.OK, upstream_ref=card_id)

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


# ---------------------------------------------------------------- upstream_vcc helpers
_RECHARGE_OK = frozenset({"success", "succeeded", "completed", "done"})
_RECHARGE_FAILED = frozenset({"failed", "fail", "failure", "rejected", "cancelled", "canceled"})
_PRODUCT_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_NAME_RE = re.compile(r"^[A-Za-z]{1,32}$")
CARD_FIELD_SEP = "----"  # the buyer-facing format of a sealed card (product delivery_separator)
MAX_CARD_AMOUNT = Decimal("100000")


@dataclass(frozen=True)
class VccSpec:
    product_code: str
    init_amount: Decimal
    recharge_amount: Decimal | None
    first_name: str
    last_name: str

    def open_body(self) -> dict[str, Any]:
        """Deterministic, so a replay is byte-identical and ``request_hash`` can pin it."""
        return {
            "product_code": self.product_code,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "init_amount": wire_amount(self.init_amount),
        }


def wire_amount(value: Decimal | None) -> int | float:
    """JSON number for the vendor (it takes plain numbers): an int when whole, else the exact
    two-decimal value. Never used for arithmetic."""
    if value is None:
        raise ValueError("no amount")
    return int(value) if value == value.to_integral_value() else float(value)


def _money_param(v: Any) -> Decimal | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d <= 0 or d > MAX_CARD_AMOUNT or d != d.quantize(Decimal("0.01")):
        return None
    return d


def vcc_spec(offer: OfferSpec) -> VccSpec | None:
    """Validate a ``vcc`` offer's parameters BEFORE any call: product code, opening amount,
    optional top-up and cardholder name. Amounts come from the staff-set offer, never a client."""
    if offer.kind != "vcc":
        return None
    p = offer.params
    code = p.get("product_code")
    init = _money_param(p.get("init_amount"))
    if not isinstance(code, str) or not _PRODUCT_RE.fullmatch(code) or init is None:
        return None
    recharge = None
    if p.get("recharge_amount") not in (None, ""):
        recharge = _money_param(p.get("recharge_amount"))
        if recharge is None:
            return None
    first, last = p.get("first_name", "Digital"), p.get("last_name", "Goods")
    if not (isinstance(first, str) and _NAME_RE.fullmatch(first)):
        return None
    if not (isinstance(last, str) and _NAME_RE.fullmatch(last)):
        return None
    return VccSpec(code, init, recharge, first, last)


def parse_card_open(data: Any) -> tuple[str, str, Decimal | None] | None:
    """``POST /cards/open`` -> ``(card id, masked number, open fee)``. The PAN / CVV / expiry in
    this answer are DROPPED here: the secret is read again from ``GET /cards/{id}`` to seal it."""
    if not isinstance(data, Mapping):
        return None
    cid = data.get("id")
    if isinstance(cid, bool) or not isinstance(cid, (int, str)) or not str(cid).isdigit():
        return None
    if len(str(cid)) > 20:
        return None
    fee = _dec_or_none(data.get("open_fee"))
    return (
        str(cid),
        mask_pan(data.get("card_number")),
        fee if fee is not None and fee >= 0 else None,
    )


_PAN_RE = re.compile(r"^[0-9]{12,19}$")
_EXP_RE = re.compile(r"^(0[1-9]|1[0-2])/([0-9]{2}|[0-9]{4})$")
_CVV_RE = re.compile(r"^[0-9]{3,4}$")


def parse_card_secret(data: Any) -> str | None:
    """``GET /cards/{id}`` -> ``"<pan>----<MM/YY>----<cvv>"`` (one sealed secret per card), or
    ``None`` for anything malformed or not an active card."""
    if not isinstance(data, Mapping):
        return None
    pan = re.sub(r"[ -]", "", str(data.get("card_number") or ""))
    exp, cvv = str(data.get("expire") or "").strip(), str(data.get("cvv") or "").strip()
    status = str(data.get("status") or "ACTIVE").upper()
    if status not in ("ACTIVE", "FROZEN") or not (
        _PAN_RE.fullmatch(pan) and _EXP_RE.fullmatch(exp) and _CVV_RE.fullmatch(cvv)
    ):
        return None
    return CARD_FIELD_SEP.join((pan, exp, cvv))
