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
    card_secret,
    clean_detail,
    dec,
    job_idempotency_key,
)
from app.suppliers.direct_credential import CredentialError, mask_email, validate_credential
from app.suppliers.zovocard import direct as dr
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
class HalfCheck:
    """Outcome of :meth:`ZovoCardSupplier.half_price_check` (no secret in it)."""

    status: str  # eligible | ineligible | session_invalid | unavailable
    reason: str = ""
    quote: tuple[int, str, int] | None = None  # (amount minor, currency, exponent)
    account_mask: str | None = None
    current_plan: str | None = None
    promotions: tuple[str, ...] = ()


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
    recharge_fee: Decimal | None = None  # a RATE (vendor 6.1), applied to a top-up


def region_offered(catalog: PlanCatalog, country: object) -> bool:
    """A region the offer names must still be in the vendor's ``payment_regions`` (issue #27: a
    region the vendor stops offering is shown unavailable, never forced). A catalogue that lists
    no region at all cannot confirm a pinned country: it is NOT offered (fail closed); a source
    without a pinned country is unaffected."""
    if country is None:
        return True
    if not catalog.regions:
        return False  # fail closed: the vendor's region list is empty / unreadable, a pinned
        # country cannot be shown to be offered
    return any(c == country for c, _cur in catalog.regions)


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
        card = item.get("card_id")
        card_id = (
            str(card)[:64]
            if isinstance(card, (int, str)) and not isinstance(card, bool) and str(card)
            else None
        )
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
                card_id=card_id,
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
                recharge_fee=_dec_or_none(p.get("recharge_fee")),
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

    async def payment_regions(self, product: str) -> list[tuple[str, str]]:
        """The vendor's live ``payment_regions`` ``(country, currency)`` for a product (cached with
        the plans catalogue). Never hard-coded: the vendor says the list changes."""
        return list((await self.plans(product)).regions)

    async def redemption_cost(self, offer: OfferSpec) -> Money | None:
        """Reserve the breaker holds back per issued CDK (review-v12 §6, §12 second pass): the
        plan's ``expectedAmountMinor`` plus the balance buffer (``balanceBufferBps``, at least
        ``minBalanceBufferUsdMinor``) plus ``serviceFeeUsdMinor`` from the cached plans, only when
        the plan currency is USD. Otherwise ``None``: the per-offer configured cost applies."""
        if offer.kind == "vcc":
            return await self._vcc_cost(offer)
        if offer.kind == "direct":
            return await self._direct_cost(offer)
        product = offer.params.get("product", "gpt")
        key = offer.params.get("plan")
        if product not in DIRECT_PRODUCTS or not isinstance(key, str):
            return None
        return await self._plan_cost(product, key)

    async def _plan_cost(self, product: str, key: str) -> Money | None:
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
        return Money(_vcc_total(spec, prod), "USD")

    async def availability_fresh(self, offer: OfferSpec) -> Availability:
        """The engine's pre-check before a send: never answered from the 60 s plan cache (N3)."""
        return await self.availability(offer, fresh=True)

    async def availability(self, offer: OfferSpec, *, fresh: bool = False) -> Availability:
        now = datetime.now(timezone.utc)
        if offer.kind == "vcc":
            return await self._vcc_availability(offer, now, fresh=fresh)
        if offer.kind == "direct":
            return await self._direct_availability(offer, now, fresh=fresh)
        if offer.kind != "cdk":
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
        if not region_offered(catalog, offer.params.get("payment_country")):
            return Availability(False, "region_not_offered", checked_at=now)
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
        if job.offer.kind == "direct":
            return await self._direct_run(job)
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
        ends with at most one issued code per key. (A ``direct`` job is reconciled by READS only,
        :meth:`_direct_query`: it never re-POSTs here.) [I] The vendor list
        (``GET /gpt-direct/cdks``)
        is not used: it is not documented to expose the idempotency key, so it cannot correlate
        a code with a job (spec §14.3, to confirm in the sandbox).
        """
        if job.offer.kind == "direct":
            return await self._direct_query(job)
        return await self.submit(job)

    async def void(self, job: JobSpec) -> Result:
        """Disable an unused code (``POST /gpt-direct/cdks/{id}/disable``). The issue fee is not
        refunded; ``409 CDK_DISABLE_REJECTED`` = already used or unknown -> FATAL (cannot void)."""
        ref = job.upstream_ref or ""
        if not ref.isdigit() or len(ref) > 20:
            return Result(Outcome.FATAL, error_code="invalid_upstream_ref")
        if job.offer.kind == "vcc":
            return await self._vcc_void(job, ref)
        if job.offer.kind == "direct":
            return await self._direct_void(job, ref)
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
        cost = Money(_vcc_total(spec, prod), "USD")
        return Availability(True, cost_estimate=cost, checked_at=now)

    async def _vcc_run(self, job: JobSpec) -> Result:
        """The whole purchase, idempotent end to end: ``submit`` and ``query`` are the same walk.

        * ``open`` is POSTed ONLY while the job has no card id yet (under the job's key; a replay
          of a 2xx answers from the vendor cache: same card). Once ``job.upstream_ref`` is known the
          walk continues on THAT card and never opens again (a different card from a replay would
          be a second purchase);
        * ``GET /cards/{id}`` is the balance witness: a top-up is skipped when the card already
          holds ``init + recharge``; otherwise the recharge list decides (a row = it ran, never
          POST again; the vendor's "pending" 400 is not cached) and the POST goes under
          ``pj:<job>:recharge``;
        * before sealing the card must be ACTIVE and hold the expected amount (``balance_short``);
        * PAN / expiry / CVV leave this method only inside ``Result.secrets`` (sealed by the caller
          at once); nothing is logged or kept.

        A closed job (``step == "done"``: refund / fail) only learns the card id (so it can be
        deleted) and returns no secret."""
        spec = vcc_spec(job.offer)
        if spec is None:
            return Result(Outcome.FATAL, error_code="invalid_offer")
        known = job.upstream_ref if job.upstream_ref and job.upstream_ref.isdigit() else None
        mask: str | None = None
        cost: Money | None = None
        fee: Decimal | None = None
        if known is None:
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
            if fee is not None:
                total = spec.init_amount + (spec.recharge_amount or Decimal(0)) + fee
                cost = Money(total + await self._recharge_fee(spec), "USD")
        else:
            card_id = known
        base: dict[str, Any] = {"upstream_ref": card_id, "card_mask": mask, "cost": cost}
        if job.step == "done":
            return Result(Outcome.OK, **base)
        want = spec.init_amount + (spec.recharge_amount or Decimal(0))
        detail = await self._card_detail(card_id, base)
        if isinstance(detail, Result):
            return detail
        if detail.available < want and spec.recharge_amount is not None:
            rc = await self._vcc_recharge(job, spec, card_id, base)
            if rc is not None:
                return rc
            detail = await self._card_detail(card_id, base)  # the witness after the top-up
            if isinstance(detail, Result):
                return detail
        base["card_mask"] = detail.mask or mask
        if detail.status != "ACTIVE":
            return Result(Outcome.FATAL, error_code="card_not_active", **base)
        if detail.available < want:
            # a fee the vendor REPORTS (the card's open_fee, each recharge row's fee) may have been
            # taken from the card: tolerate exactly that much, never more (sandbox fees are 0, so
            # where a real fee is booked is still unknown). Floor: init - open_fee.
            fees = await self._reported_fees(card_id)
            if fees is None:
                return Result(Outcome.RETRYABLE, error_code="recharge_list_unreadable", **base)
            open_fee = detail.open_fee if detail.open_fee is not None else (fee or Decimal(0))
            required = max(spec.init_amount - open_fee, want - open_fee - fees)
            if detail.available < required:
                return Result(Outcome.FATAL, error_code="balance_short", **base)
        secret = card_secret(detail.pan, detail.expire, detail.cvv)
        if secret is None:
            return Result(Outcome.AMBIGUOUS, error_code="malformed_card", **base)
        return Result(Outcome.OK, secrets=(secret,), **base)

    async def _reported_fees(self, card_id: str) -> Decimal | None:
        """Sum of ``fee`` over the card's successful recharge rows (``None`` = list unreadable)."""
        try:
            listing = await self.client.get(f"/cards/{card_id}/recharges")
        except SupplierError:
            return None
        total = Decimal(0)
        for r in _recharge_rows(listing.data):
            if str(r.get("status") or "").lower() in _RECHARGE_OK:
                f = _dec_or_none(r.get("fee"))
                if f is not None and f > 0:
                    total += f
        return total

    async def _recharge_fee(self, spec: VccSpec) -> Decimal:
        """The catalogue's top-up fee (``recharge_fee`` is a rate) for the estimate; 0 when the
        catalogue cannot say. The ledger rows stay the truth."""
        if spec.recharge_amount is None:
            return Decimal(0)
        try:
            prod = next(
                (p for p in await self.card_products() if p.product_code == spec.product_code), None
            )
        except SupplierError:
            return Decimal(0)
        rate = prod.recharge_fee if prod is not None and prod.recharge_fee is not None else 0
        return (spec.recharge_amount * Decimal(rate)).quantize(Decimal("0.01"))

    async def _card_detail(self, card_id: str, base: dict[str, Any]) -> CardDetail | Result:
        try:
            resp = await self.client.get(f"/cards/{card_id}")
        except SupplierError as exc:
            # the card exists: whatever the read says, this is never "nothing happened"
            outcome = Outcome.RETRYABLE if exc.outcome is Outcome.UNAVAILABLE else exc.outcome
            return Result(outcome, error_code=exc.reason, retry_after=exc.retry_after, **base)
        d = parse_card_detail(resp.data)
        if d is None:
            return Result(Outcome.AMBIGUOUS, error_code="malformed_card", **base)
        return d

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
        rows = _recharge_rows(listing.data)
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
        separate refund would have to leave USD 1 on the card, so it adds nothing).

        * a recharge still in flight is waited out first (deleting under it could strand money);
        * ``DELETED`` = done; a ``202`` is persisted by the engine as step ``delete_pending`` and
          from then on ONLY the card is read (the DELETE is never re-sent while it is pending)."""
        try:
            detail = await self.client.get(f"/cards/{card_id}")
        except SupplierError as exc:
            return Result.from_error(exc)
        status = (
            str(detail.data.get("status", "")).upper() if isinstance(detail.data, Mapping) else ""
        )
        if status == "DELETED":
            return Result(Outcome.OK, upstream_ref=card_id)
        if job.step == "delete_pending":
            return Result(
                Outcome.ACCEPTED_PENDING, error_code="delete_pending", upstream_ref=card_id
            )
        try:
            listing = await self.client.get(f"/cards/{card_id}/recharges")
        except SupplierError as exc:
            if exc.outcome is Outcome.FATAL and exc.status == 404:
                listing = None
            else:
                return Result.from_error(exc)
        if listing is not None:
            for r in _recharge_rows(listing.data):
                st = str(r.get("status") or "").lower()
                if st not in _RECHARGE_OK and st not in _RECHARGE_FAILED:
                    return Result(
                        Outcome.ACCEPTED_PENDING,
                        error_code="recharge_pending",
                        upstream_ref=card_id,
                    )
        try:
            resp = await self.client.request(
                "DELETE", f"/cards/{card_id}", idempotency_key=job.idempotency_key
            )
        except SupplierError as exc:
            return Result.from_error(exc)
        pending = isinstance(resp.data, Mapping) and resp.data.get("pending") is True
        if resp.status == 202 or pending:
            return Result(
                Outcome.ACCEPTED_PENDING, error_code="delete_pending", upstream_ref=card_id
            )
        return Result(Outcome.OK, upstream_ref=card_id)

    # ---- upstream_direct (M3 part 2): preflight -> POST /gpt-direct/orders (202) -> GET order
    async def _direct_cost(self, offer: OfferSpec) -> Money | None:
        """Wallet cost of one direct order, in USD or ``None`` (the offer's configured cost then
        applies). Claude / Grok are constant USD prices: the registry's ``checkout_amount_minor``
        plus the service fee (plus the plan's balance buffer when it names one). A GPT plan uses
        the plan formula of ``redemption_cost`` (USD plans only)."""
        spec = dr.direct_spec(offer)
        if spec is None:
            return None
        if spec.product == "gpt":
            return await self._plan_cost("gpt", spec.plan)
        catalog = await self.plans(spec.product)
        entry = catalog.registry.get(spec.plan)
        if (
            entry is None
            or entry.checkout_currency != "USD"
            or entry.checkout_amount_minor is None
            or entry.checkout_amount_minor < 0
        ):
            return None
        plan = catalog.plans.get(entry.acc_plan_key or "")
        bps = _int_or_none(plan.get("balanceBufferBps")) if isinstance(plan, Mapping) else None
        base = Decimal(entry.checkout_amount_minor)
        minor = base + (base * (bps or 0) / 10000).to_integral_value()
        minor += Decimal(entry.service_fee_usd_minor or 0)
        return Money((minor / 100).quantize(Decimal("0.01")), "USD")

    async def _direct_availability(
        self, offer: OfferSpec, now: datetime, *, fresh: bool = False
    ) -> Availability:
        spec = dr.direct_spec(offer)
        if spec is None:
            return Availability(False, "invalid_offer", checked_at=now)
        try:
            catalog = await self.plans(spec.product, fresh=fresh)
        except SupplierError as exc:
            return Availability(False, exc.reason, checked_at=now)
        if not is_purchasable(catalog, spec.plan):
            return Availability(False, "not_purchasable", checked_at=now)
        if spec.product == "gpt" and not region_offered(catalog, spec.country):
            return Availability(False, "region_not_offered", checked_at=now)
        try:
            cost = await self._direct_cost(offer)
        except SupplierError:
            cost = None
        if spec.card_mode != "fixed":
            return await self._auto_card_availability(spec, cost, now)
        assert spec.card_id is not None
        problem, mask = await self._funding_card_problem(spec, cost)
        if problem is not None:
            return Availability(
                False, problem, cost_estimate=cost, checked_at=now, funding_mask=mask
            )
        return Availability(True, cost_estimate=cost, checked_at=now, funding_mask=mask)

    async def _auto_card_availability(
        self, spec: dr.DirectSpec, cost: Money | None, now: datetime
    ) -> Availability:
        """Card mode ``auto_existing`` (review-v12 §21.1): ask the vendor which card it would pick
        for this plan (``GET /gpt-direct/card-pool/schedule``, a read; nothing is reserved), then
        run OUR checks on the pick and, when it fails them, on the next candidates that carry no
        ``skip_reason`` (at most :data:`dr.MAX_CARD_TRIES` cards in all). The cards that pass go
        back to the engine, which still enforces "one order in flight per card" before the send.
        No pick, or no usable card, is a WAIT (never a failure): the job parks."""
        try:
            resp = await self.client.get(
                "/gpt-direct/card-pool/schedule",
                params={"product": spec.product, "plan": spec.plan, "card_mode": "auto_existing"},
            )
        except SupplierError as exc:
            if exc.reason in dr.CARD_WAIT_CODES:  # the vendor itself says "no card"
                return Availability(False, dr.NO_CARD_PICK, cost_estimate=cost, checked_at=now)
            return Availability(False, exc.reason, cost_estimate=cost, checked_at=now)
        ids = dr.parse_schedule(resp.data)
        if not ids:
            return Availability(False, dr.NO_CARD_PICK, cost_estimate=cost, checked_at=now)
        usable: list[tuple[str, str | None]] = []
        busy = False
        for cid in ids:
            probe = dr.DirectSpec(
                spec.product, spec.plan, cid, spec.country, spec.currency, spec.card_mode
            )
            problem, mask = await self._funding_card_problem(probe, cost)
            if problem is None:
                usable.append((str(cid), dr.last4_of((mask or "")[-4:])))
            else:
                busy = busy or problem == "funding_card_busy"
        if not usable:
            reason = dr.CARD_POOL_BUSY if busy else dr.NO_CARD_PICK
            return Availability(False, reason, cost_estimate=cost, checked_at=now)
        return Availability(True, cost_estimate=cost, checked_at=now, card_candidates=tuple(usable))

    async def _funding_card_problem(
        self, spec: dr.DirectSpec, cost: Money | None
    ) -> tuple[str | None, str | None]:
        """The funding card must be OURS (``GET /cards/{id}`` answers for our account only: a
        404 means it is not ours), ACTIVE, hold at least the order's cost (USD) and have capacity
        left under the vendor's usage cap (``GET /gpt-direct/cards/{id}/usage``, [I] shape: an
        unreadable answer does not block). The PAN / CVV in the card read are dropped at once."""
        try:
            resp = await self.client.get(f"/cards/{spec.card_id}")
        except SupplierError as exc:
            return ("funding_card_invalid" if exc.status in (400, 404) else exc.reason), None
        detail = parse_card_detail(resp.data)
        if detail is None:
            return "funding_card_invalid", None
        mask = detail.mask
        enough = cost is None or detail.available >= cost.amount
        try:
            usage = await self.client.get(
                f"/gpt-direct/cards/{spec.card_id}/usage", params={"product": spec.product}
            )
            remaining, waiting, usage_status = dr.usage_state(usage.data, spec.plan)
        except SupplierError:
            remaining, waiting, usage_status = None, False, None
        if usage_status is not None and usage_status != "ACTIVE":
            return "funding_card_invalid", mask  # e.g. DELETED
        return dr.funding_card_problem(detail.status, enough, remaining, waiting), mask

    async def _preflight(self, spec: dr.DirectSpec, credential: str) -> dr.Preflight | Result:
        """``POST /gpt-direct/preflight``: validates the credential and quotes (read-only upstream;
        token valid 10 min, single use). A failure here is a READ failure: nothing was bought."""
        body = dr.preflight_body(spec, credential)
        try:
            resp = await self.client.request(
                "POST",
                "/gpt-direct/preflight",
                json_body=body,
                read_only_post=True,
                endpoint="direct.preflight",
            )
        except SupplierError as exc:
            return Result.from_error(exc)
        finally:
            body.clear()
        pf = dr.parse_preflight(resp.data, parse_time)
        if pf is None:
            return Result(Outcome.RETRYABLE, error_code="malformed_preflight")
        return pf

    async def half_price_check(
        self,
        product: str,
        plan: str,
        country: str | None,
        currency: str | None,
        credential: str,
    ) -> HalfCheck:
        """Eligibility of the buyer's own account for a half-price plan, read from the vendor's
        preflight (read-only upstream; obeys the production-write gate like every preflight).
        Never raises for a vendor answer: the outcome is ``eligible`` / ``ineligible`` /
        ``session_invalid`` / ``unavailable``."""
        if product not in dr.DIRECT_PRODUCTS or not dr._PLAN_RE.fullmatch(plan):
            return HalfCheck("unavailable", "invalid_offer")
        spec = dr.DirectSpec(product, plan, 1, country, currency)
        pf = await self._preflight(spec, credential)
        if isinstance(pf, Result):
            code = pf.error_code or "preflight_failed"
            if code in ("GPT_SESSION_INVALID", "CLAUDE_SESSION_INVALID", "SESSION_REQUIRED"):
                return HalfCheck("session_invalid", "session_rejected")
            return HalfCheck("unavailable", clean_detail(code))
        ok, reason = dr.half_eligibility(
            pf,
            plan,
            datetime.now(timezone.utc),
            quotes_optional=dr.real_sandbox(self.client.config.base_url, self.env),
        )
        quote = pf.quotes.get(plan)
        return HalfCheck(
            "eligible" if ok else "ineligible",
            reason,
            quote=quote,
            account_mask=pf.account_mask,
            current_plan=pf.current_plan,
            promotions=pf.promotions,
        )

    async def _direct_run(self, job: JobSpec) -> Result:
        """Preflight, then ONE order POST under the job's key; afterwards only reads.

        * the recorded order id answers everything: a job that knows its order NEVER POSTs again;
        * the buyer's session comes in ``job.credential`` (opened in memory by the engine for this
          one call); it is sent to the vendor and goes nowhere else, not even into an exception;
        * a refused price (``GPT_PRICE_UNCONFIRMED``) re-quotes ONCE inline under the same key: the
          refusal is definite (nothing executed), and the retry gets a fresh single-use token;
        * a ``202`` is ACCEPTED_PENDING, never success (the order is only queued)."""
        spec = dr.direct_spec(job.offer)
        if spec is None:
            return Result(Outcome.FATAL, error_code="invalid_offer")
        ref = job.upstream_ref if job.upstream_ref and job.upstream_ref.isdigit() else None
        rid = dr.client_request_id(job.job_id)
        if ref is not None:
            return await self._direct_status(spec, ref, rid, None, job.account_mask)
        if spec.card_id is None:  # auto_existing: the engine must have picked a card first
            return Result(Outcome.FATAL, error_code="card_not_selected")
        if not job.credential:
            return Result(Outcome.FATAL, error_code="credential_missing")
        try:
            checked = validate_credential(spec.product, job.credential)
        except CredentialError as exc:
            return Result(Outcome.FATAL, error_code=f"credential_{exc.code}")
        mask: str | None = None
        expires: datetime | None = None
        for attempt in (1, 2):
            pf = await self._preflight(spec, checked.canonical)
            if isinstance(pf, Result):
                return pf
            mask, expires = pf.account_mask or mask, pf.expires_at or expires
            seen: dict[str, Any] = {"account_mask": mask, "preflight_expires_at": expires}
            if dr.is_half_plan(spec.plan):
                # second line of defence of issue #27: the vendor refuses a half-price key for an
                # account that is not eligible BEFORE any charge and never falls back to full
                # price. It is a definite refusal here too (staff refund), never a retry.
                ok, _ = dr.half_eligibility(
                    pf,
                    spec.plan,
                    datetime.now(timezone.utc),
                    quotes_optional=dr.real_sandbox(self.client.config.base_url, self.env),
                )
                # EVERY reason the preflight gives (blocked, waiting, subscribed, no quote...)
                if not ok:
                    return Result(Outcome.FATAL, error_code="half_price_ineligible", **seen)
            if pf.quote_error:  # the vendor: do not create an order while quote_error is set
                return Result(Outcome.RETRYABLE, error_code="quote_error", **seen)
            if job.account_mask and pf.account_mask and job.account_mask != pf.account_mask:
                # the session belongs to ANOTHER account than the first one (re-supplied)
                return Result(Outcome.FATAL, error_code="session_other_account", **seen)
            if pf.current_plan == spec.plan:
                return Result(Outcome.FATAL, error_code="plan_already_active", **seen)
            blocked = _account_problem(pf)
            if blocked is not None:  # support can explain these; nothing is sent
                return Result(Outcome.FATAL, error_code=blocked, **seen)
            over = await self._quote_problem(spec, pf, job)
            if over is not None:
                return Result(Outcome.FATAL, error_code=over, **seen)
            try:
                version = (await self.plans(spec.product, fresh=attempt > 1)).version
            except SupplierError:
                version = None
            body = dr.order_body(
                spec,
                credential=checked.canonical,
                preflight_token=None
                if dr.omit_preflight_token(self.client.config.base_url, self.env)
                else pf.token,
                request_id=rid,
                pricing_version=version,
            )
            try:
                resp = await self.client.request(
                    "POST",
                    "/gpt-direct/orders",
                    json_body=body,
                    idempotency_key=job.idempotency_key,
                    endpoint="direct.order",
                )
            except SupplierError as exc:
                if exc.error_code == "GPT_PRICE_UNCONFIRMED" and attempt == 1:
                    continue  # re-quote once: the answer is "not executed", the key stays
                return Result.from_error(exc, account_mask=mask)
            finally:
                body.clear()
            return await self._direct_created(spec, resp.data, rid, seen)
        return Result(Outcome.RETRYABLE, error_code="GPT_PRICE_UNCONFIRMED", account_mask=mask)

    async def _quote_problem(
        self, spec: dr.DirectSpec, pf: dr.Preflight, job: JobSpec
    ) -> str | None:
        """Use the preflight quote (the price the buyer's account is actually charged): refuse
        before any order when it is above what the offer allows. A USD quote (Claude / Grok, and
        USD GPT regions) is compared with ``job.cost_bound`` (offer cost x (1 + tolerance)), or,
        without one, the catalogue's own USD cost x (1 + tolerance). A non-USD quote (a GPT plan in
        PHP...) cannot be compared with a USD budget: it is compared, in its own currency, with the
        plan's ``maxAmountMinor`` (else ``expectedAmountMinor`` x (1 + tolerance))."""
        quote = pf.quotes.get(spec.plan)
        catalog = await self.plans(spec.product)
        entry = catalog.registry.get(spec.plan)
        if quote is None:
            if (
                spec.product in dr.USD_PRODUCTS
                and entry
                and entry.checkout_amount_minor is not None
            ):
                quote = (entry.checkout_amount_minor, entry.checkout_currency or "USD", 2)
            elif self.env == "sandbox":
                # [V] the sandbox never quotes (``quotes: null``): the budget cannot be checked
                # against a price that does not exist; the offer's cost bound stays the ceiling
                return None
            else:
                return "quote_missing"  # production: no price, no bound check, no order
        minor, currency, exp = quote
        amount = Decimal(minor).scaleb(-exp)
        tol = Decimal(1) + job.quote_tolerance
        if currency == "USD":
            bound = job.cost_bound
            if bound is None:
                est = await self._direct_cost(job.offer)
                bound = Money(est.amount * tol, "USD") if est else None
            if bound is None or bound.currency != "USD":
                return "quote_unbounded"
            return "quote_over_bound" if amount > bound.amount else None
        plan = catalog.plans.get(entry.acc_plan_key or "") if entry else None
        if not isinstance(plan, Mapping) or str(plan.get("currency", "")).upper() != currency:
            return "quote_currency_mismatch"
        ceiling = _int_or_none(plan.get("maxAmountMinor"))
        if ceiling is None:
            expected = _int_or_none(plan.get("expectedAmountMinor"))
            ceiling = int(Decimal(expected) * tol) if expected is not None else None
        if ceiling is None:
            return "quote_unbounded"
        return "quote_over_bound" if minor > ceiling else None

    async def _direct_created(
        self, spec: dr.DirectSpec, data: Any, rid: str, seen: dict[str, Any]
    ) -> Result:
        """The 2xx answer of ``POST /orders``: record the reference on EVERY path that has one."""
        ref = dr.order_id_of(data)
        if ref is None:
            # something may exist (the request was accepted) but we cannot read its id: the lookup
            # by client_request_id finds it; never a blind second order
            return Result(Outcome.AMBIGUOUS, error_code="malformed_order", **seen)
        crid = data.get("client_request_id") if isinstance(data, Mapping) else None
        if isinstance(crid, str) and crid != rid:
            return Result(Outcome.AMBIGUOUS, error_code="order_mismatch", **seen)
        status = data.get("status") if isinstance(data, Mapping) else None
        phase = dr.order_phase(status)
        if phase == "failed":
            return Result(
                Outcome.FATAL, error_code=f"order_{clean_status(status)}", upstream_ref=ref, **seen
            )
        if phase == "done":
            return await self._direct_status(spec, ref, rid, seen.get("account_mask"), None)
        card_id, card_last4 = dr.order_card(data)
        return Result(
            Outcome.ACCEPTED_PENDING,
            upstream_ref=ref,
            card_id=card_id,
            card_last4=card_last4,
            **seen,
        )

    async def _direct_status(
        self,
        spec: dr.DirectSpec,
        ref: str,
        rid: str,
        mask_hint: str | None,
        account_mask: str | None = None,
    ) -> Result:
        """``GET /gpt-direct/orders/{id}`` -> a Result. Pure read; the reference rides on EVERY
        answer, so the job never forgets an order the vendor holds."""
        try:
            resp = await self.client.get(f"/gpt-direct/orders/{ref}")
        except SupplierError as exc:
            if exc.status == 404:  # the order we recorded is gone at the vendor
                return Result(Outcome.UNAVAILABLE, error_code="order_not_found", upstream_ref=ref)
            return Result.from_error(exc, upstream_ref=ref, account_mask=mask_hint)
        order = resp.data.get("order") if isinstance(resp.data, Mapping) else None
        if not isinstance(order, Mapping):
            return Result(Outcome.AMBIGUOUS, error_code="malformed_order", upstream_ref=ref)
        oid = dr.order_id_of(order)
        crid = order.get("client_request_id")
        if (oid is not None and oid != ref) or (isinstance(crid, str) and crid != rid):
            return Result(Outcome.AMBIGUOUS, error_code="order_mismatch", upstream_ref=ref)
        mask = mask_email(order.get("account_email")) or mask_hint
        plan = order.get("plan")
        if isinstance(plan, str) and plan != spec.plan:
            return Result(Outcome.FATAL, error_code="plan_mismatch", upstream_ref=ref)
        status = order.get("status")
        phase = dr.order_phase(status)
        detail = _order_detail(order)
        card_id, card_last4 = dr.order_card(order)  # the card that paid (vendor_switch may move it)
        if phase == "done" and not (
            isinstance(crid, str)
            and crid == rid
            and isinstance(plan, str)
            and plan == spec.plan
            # the vendor's real sandbox invents one synthetic account for the preflight
            # (…@example.test) and another for the order (…@sandbox.local): verified 2026-11-16.
            # Everywhere else the completed order must be for the account the preflight saw.
            and (
                dr.real_sandbox(self.client.config.base_url, self.env)
                or not (account_mask and mask and account_mask != mask)
            )
        ):
            # a COMPLETED order is only delivered when it provably is OUR order for this plan
            # and this account: the request id and plan must be present and equal
            return Result(
                Outcome.FATAL, error_code="order_mismatch", upstream_ref=ref, detail=detail
            )
        if phase == "failed":
            return Result(
                Outcome.FATAL,
                error_code=f"order_{clean_status(status)}",
                upstream_ref=ref,
                account_mask=mask,
                detail=detail,
                card_id=card_id,
                card_last4=card_last4,
            )
        if phase == "pending":
            return Result(
                Outcome.ACCEPTED_PENDING,
                upstream_ref=ref,
                account_mask=mask,
                detail=detail,
                card_id=card_id,
                card_last4=card_last4,
            )
        return Result(
            Outcome.OK,
            upstream_ref=ref,
            account_mask=mask,
            detail=detail,
            card_id=card_id,
            card_last4=card_last4,
            secrets=(dr.delivery_text(spec, mask, ref),),  # plan, masked account, order: no secret
            cost=_order_cost(order),
        )

    async def _direct_query(self, job: JobSpec) -> Result:
        """Reconcile by READS only (never a POST): the recorded order, else the vendor's order list
        searched for our ``client_request_id``. Nothing found = UNAVAILABLE: the engine then
        resubmits under the SAME key, which the vendor de-duplicates twice (key and request id)."""
        spec = dr.direct_spec(job.offer)
        if spec is None:
            return Result(Outcome.FATAL, error_code="invalid_offer")
        rid = dr.client_request_id(job.job_id)
        if job.upstream_ref and job.upstream_ref.isdigit():
            return await self._direct_status(spec, job.upstream_ref, rid, None, job.account_mask)
        seen_rows = 0
        for page in range(1, ORDER_LIST_MAX_PAGES + 1):
            try:
                resp = await self.client.get(
                    "/gpt-direct/orders", params={"page": str(page), "page_size": "100"}
                )
            except SupplierError as exc:
                return Result.from_error(exc)
            rows = resp.data.get("list") if isinstance(resp.data, Mapping) else None
            if not isinstance(rows, list):
                return Result(Outcome.AMBIGUOUS, error_code="malformed_order_list")
            seen_rows += len(rows)
            total = resp.data.get("total") if isinstance(resp.data, Mapping) else None
            for item in rows:
                if isinstance(item, Mapping) and item.get("client_request_id") == rid:
                    found = dr.order_id_of(item)
                    if found is None:
                        return Result(Outcome.AMBIGUOUS, error_code="malformed_order")
                    return await self._direct_status(spec, found, rid, None, job.account_mask)
            # page until the vendor says there is no more (``total`` rows seen, or a short page)
            if isinstance(total, int) and not isinstance(total, bool):
                done = not rows or seen_rows >= total
            else:
                done = len(rows) < 100
            if done:
                return Result(Outcome.UNAVAILABLE, error_code="order_not_found")
        # the list is longer than we are willing to read: "not found" would be a guess
        return Result(Outcome.AMBIGUOUS, error_code="order_list_too_long")

    async def _direct_void(self, job: JobSpec, ref: str) -> Result:
        """What "void" means for a direct order. A subscription that COMPLETED cannot be undone by
        the API (FATAL ``order_completed``: the engine ends in ``needs_manual`` ``void_failed`` with
        a critical audit row and a staff alert; a refund of the buyer is then a staff decision).
        Only an order that has not started paying (queued / awaiting_card / funding_pending) can be
        cancelled (``POST .../cancel``, 409 = too late); one already paying is waited out."""
        try:
            resp = await self.client.get(f"/gpt-direct/orders/{ref}")
        except SupplierError as exc:
            return Result.from_error(exc, upstream_ref=ref)
        order = resp.data.get("order") if isinstance(resp.data, Mapping) else None
        if not isinstance(order, Mapping):
            return Result(Outcome.AMBIGUOUS, error_code="malformed_order", upstream_ref=ref)
        status = str(order.get("status") or "").strip().lower()
        detail = _order_detail(order)
        fee = str(order.get("service_fee_status") or "").strip().lower()
        if status in dr.NOT_CHARGED and fee in dr.NOTHING_HELD_FEE:
            return Result(Outcome.OK, upstream_ref=ref, detail=detail)  # nothing to undo
        if status in dr.FAILED:
            # declined / unknown failure, or a cancel whose fee is not confirmed released: money
            # may be held, so it stays visible to staff (status and fee in the job view)
            return Result(
                Outcome.FATAL, error_code="order_failure_unconfirmed", upstream_ref=ref,
                detail=detail,
            )  # fmt: skip
        if status in dr.DONE:
            return Result(
                Outcome.FATAL, error_code="order_completed", upstream_ref=ref, detail=detail
            )
        if status not in dr.CANCELLABLE or job.step == "delete_pending":
            return Result(Outcome.ACCEPTED_PENDING, error_code="void_waiting", upstream_ref=ref)
        try:
            await self.client.request(
                "POST",
                f"/gpt-direct/orders/{ref}/cancel",
                json_body={},
                idempotency_key=job.idempotency_key,
                endpoint="direct.cancel",
            )
        except SupplierError as exc:
            if exc.status == 409:  # payment already started: too late to cancel
                return Result(Outcome.FATAL, error_code="order_not_cancellable", upstream_ref=ref)
            return Result.from_error(exc, upstream_ref=ref)
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


# ---------------------------------------------------------------- upstream_vcc helpers
CARD_ROW_TYPES = frozenset({"open_card", "card_recharge", "card_refund", "card_fee", "card_delete"})
_RECHARGE_OK = frozenset({"success", "succeeded", "completed", "done"})
_RECHARGE_FAILED = frozenset({"failed", "fail", "failure", "rejected", "cancelled", "canceled"})
_PRODUCT_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_NAME_RE = re.compile(r"^[A-Za-z]{1,32}$")
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


@dataclass(frozen=True)
class CardDetail:
    status: str
    available: Decimal
    open_fee: Decimal | None = None  # the card object's own open_fee, when the read carries it
    pan: str = field(repr=False, default="")
    expire: str = field(repr=False, default="")
    cvv: str = field(repr=False, default="")

    @property
    def mask(self) -> str | None:
        return mask_pan(self.pan) if self.pan else None


def _vcc_total(spec: VccSpec, prod: CardProduct) -> Decimal:
    """Wallet cost of one card: opening amount + top-up + open fee + the top-up's fee (a rate)."""
    top = spec.recharge_amount or Decimal(0)
    rate = prod.recharge_fee or Decimal(0)
    return (
        spec.init_amount
        + top
        + (prod.open_fee or Decimal(0))
        + (top * rate).quantize(Decimal("0.01"))
    )


def parse_card_detail(data: Any) -> CardDetail | None:
    """``GET /cards/{id}``; ``None`` when the amount or status cannot be read (never guessed)."""
    if not isinstance(data, Mapping):
        return None
    try:
        available = dec(data.get("available_amount"))
    except ValueError:
        return None
    status = str(data.get("status") or "").upper()
    if not status:
        return None
    fee = _dec_or_none(data.get("open_fee"))
    return CardDetail(
        status,
        available,
        fee if fee is not None and fee >= 0 else None,
        str(data.get("card_number") or ""),
        str(data.get("expire") or ""),
        str(data.get("cvv") or ""),
    )


def _recharge_rows(data: Any) -> list[Mapping[str, Any]]:
    return [r for r in (data if isinstance(data, list) else []) if isinstance(r, Mapping)]


# ---------------------------------------------------------------- upstream_direct helpers
def clean_status(value: object) -> str:
    """A vendor order status reduced to ``[a-z0-9_]{1,24}`` for an error code (never free text)."""
    return re.sub(r"[^a-z0-9_]", "_", str(value or "unknown").strip().lower())[:24] or "unknown"


def _order_cost(order: Mapping[str, Any]) -> Money | None:
    """Estimate of what the order cost, in USD only (the ledger rows stay the truth)."""
    if str(order.get("currency") or "").upper() != "USD":
        return None
    for key in ("final_amount_minor", "quoted_amount_minor"):
        v = order.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            return Money((Decimal(v) / 100).quantize(Decimal("0.01")), "USD")
    return None


ORDER_LIST_MAX_PAGES = 50  # 5000 orders: beyond that a "not found" would be a guess


def _order_detail(order: Mapping[str, Any]) -> str:
    """``status:fee_status[:funding]`` of a vendor order for staff (machine alphabet, 32 chars)."""
    parts = [
        clean_status(order.get("status")),
        clean_status(order.get("service_fee_status") or "-"),
    ]
    if order.get("funding_hold_status"):
        parts.append(clean_status(order.get("funding_hold_status")))
    return ":".join(parts)[:32]


def _account_problem(pf: dr.Preflight) -> str | None:
    """The preflight says this account cannot take the order now [V]: refused before any send."""
    if pf.blocking_reason:
        return "preflight_blocked"
    if pf.can_purchase_at is not None and pf.can_purchase_at > datetime.now(timezone.utc):
        return "purchase_not_yet"
    if pf.recovery_required:
        return "subscription_recovery_required"
    if pf.has_active:
        return "subscription_active"
    return None
