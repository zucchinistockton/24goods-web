"""vmcardio adapter (ADR-055): the SECOND issuer of the dedicated card of a card.zzshu.pro order.

It is used for nothing else: no offers, no CDK / VCC / direct jobs (``capabilities`` is empty and
the admin refuses a source on it). ZovoCard stays the first issuer; this one opens the card only
when ZovoCard could not (see ``app/suppliers/issuers.py``).

Lifecycle of one card, same as the ZovoCard dedicated card (ADR-054):

* open: ``/v2/getProductCode`` -> the owner's BIN priority (``vmcardio_bin_priority``) picks the
  product, skipping one whose ``remaining_open_card_num`` is at or below ``vmcardio_reserve_slots``
  -> ``/v2/createCard`` loaded with ``need`` in whole dollars up. The request id is deterministic
  (``zz-<job uuid hex>-open``) and the card ``label`` is the same reference. A repeat of a request
  id answers ``Duplicate Client Request Id`` (not the first result): the card is then looked up in
  ``/v2/getCardList``. The vendor's list shows no label, so the lookup takes the ONE active card
  created in the last 45 minutes that holds exactly the loaded amount; none yet = RETRYABLE (the
  engine retries under the same id), several = AMBIGUOUS (staff). Never a second create.
* read: ``/v2/cardDetail`` for the number / expiry / CVV, held in memory for the one zzshu POST.
* finish: ``/v2/refundCard`` of the balance above the vendor's minimum, then ``/v2/deleteCard``
  (which returns the rest to the account).

The job's card id is ``vm:<vm_card_id>`` so the engine and the composite issuer can tell the vendors
apart. No card number / CVV is ever logged, stored, audited or put into an exception.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from app.suppliers.base import (
    Availability,
    Balance,
    Capability,
    FundingPolicy,
    JobSpec,
    OfferSpec,
    Outcome,
    ProbeReport,
    Result,
    SupplierError,
    SupplierEvent,
    WebhookRejected,
    dec,
)
from app.suppliers.vmcardio.client import VmcardioClient
from app.suppliers.zovocard.supplier import CardSecret, DedicatedCard, DedicatedRead

log = logging.getLogger("app.suppliers.vmcardio")

PREFIX = "vm:"
DEFAULT_BINS = ("537872", "537882", "532604", "555659")
DEFAULT_RESERVE_SLOTS = 1
PRODUCTS_TTL_S = 30.0
RECOVER_WINDOW = timedelta(minutes=45)
RECOVER_PAGES = 3
PAGE_SIZE = 50
REFUND_KEEP = Decimal("0.10")  # the highest minimum the vendor keeps on a card (FAQ 14)
FUNDS_INSUFFICIENT = "funds_insufficient"
CAP_REACHED = "funding_cap_reached"
_VM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,40}$")
_REF_BAD = re.compile(r"[^A-Za-z0-9]")
_ADDR_BAD = re.compile(r"[^A-Za-z0-9 -]")


@dataclass(frozen=True)
class Holder:
    """Cardholder data from the procurement env (never in git, never logged)."""

    first_name: str = "Digital"
    last_name: str = "Goods"
    address_line_one: str = ""
    city: str = ""
    state: str = ""
    post_code: str = ""
    country: str = "US"

    def address(self) -> dict[str, str] | None:
        """``card_address`` of createCard: all or nothing (the vendor requires every field but
        line two once it is given; it takes letters, digits, spaces and hyphens only)."""
        parts = {
            "address_line_one": self.address_line_one,
            "city": self.city,
            "state": self.state,
            "country": self.country,
            "post_code": self.post_code,
        }
        clean = {k: _ADDR_BAD.sub("", v).strip() for k, v in parts.items()}
        return clean if all(clean.values()) else None


@dataclass(frozen=True)
class Product:
    bin: str
    code: str
    remaining: int


def is_vm_card(card_id: str | None) -> bool:
    return bool(card_id) and str(card_id).startswith(PREFIX)


def vm_id_of(card_id: str) -> str | None:
    """The vendor card id inside ``vm:<id>`` (None = not a vmcardio card id)."""
    if not card_id.startswith(PREFIX):
        return None
    vid = card_id[len(PREFIX) :]
    return vid if _VM_ID_RE.fullmatch(vid) else None


def reference_of(key: str) -> str:
    """``zz-<job uuid hex>`` from the zzshu open key ``zzopen:<job uuid>``."""
    hexed = _REF_BAD.sub("", key.split(":", 1)[-1])[:40]
    return f"zz-{hexed}"


def pick_product(products: list[Product], bins: tuple[str, ...], reserve: int) -> Product | None:
    """The first BIN of the owner's priority that has a product with more than ``reserve`` slots
    left. Never a BIN outside the list."""
    for b in bins:
        for p in products:
            if p.bin[: len(b)] == b and p.remaining > max(0, reserve):
                return p
    return None


def _expiry(text: str) -> tuple[int, int] | None:
    t = text.strip()
    m = re.fullmatch(r"(\d{4})\s*[-/]\s*(\d{2})", t)
    if m:
        return int(m.group(2)), int(m.group(1))
    m = re.fullmatch(r"(\d{2})\s*[-/]?\s*(\d{4}|\d{2})", t)
    if not m:
        return None
    year = int(m.group(2))
    return int(m.group(1)), year + 2000 if year < 100 else year


def parse_secret(data: Mapping[str, Any]) -> CardSecret | None:
    pan = re.sub(r"[ -]", "", str(data.get("card_number") or ""))
    cvv = str(data.get("cvv") or "").strip()
    exp = _expiry(str(data.get("expire") or ""))
    if not (re.fullmatch(r"[0-9]{12,19}", pan) and re.fullmatch(r"[0-9]{3,4}", cvv) and exp):
        return None
    month, year = exp
    if not 1 <= month <= 12:
        return None
    return CardSecret(pan, month, year, cvv)


def _last4(data: Mapping[str, Any]) -> str | None:
    digits = re.sub(r"\D", "", str(data.get("card_number") or ""))
    return digits[-4:] if len(digits) >= 12 else None


def _created(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    t = text.strip().replace("T", " ").rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(t[:26], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _recent(created: datetime | None, now: datetime) -> bool:
    """The list gives no zone: a time that is recent as UTC or as UTC+8 (the vendor's console zone)
    counts."""
    if created is None:
        return False
    return any(abs(now - (created - timedelta(hours=h))) <= RECOVER_WINDOW for h in (0, 8))


class VmcardioSupplier:
    capabilities: frozenset[Capability] = frozenset()  # an issuer of cards only
    adapter_name = "vmcardio"

    def __init__(self, code: str, client: VmcardioClient, holder: Holder | None = None) -> None:
        self.code = code
        self.client = client
        self.env = client.config.env
        self.holder = holder or Holder()
        self._products_hit: tuple[float, list[Product]] | None = None
        self._wallet_hit: tuple[float, Decimal] | None = None

    async def aclose(self) -> None:
        await self.client.aclose()

    # ---- reads
    async def balance(self) -> Balance:
        resp = await self.client.call("/v2/getAccountBalance")
        data = resp.data if isinstance(resp.data, Mapping) else {}
        try:
            bal = dec(data.get("balance"))
        except ValueError:
            raise SupplierError(
                "unreadable balance", outcome=Outcome.RETRYABLE, error_code="balance_unreadable"
            ) from None
        return Balance(spendable=bal, currency="USD")

    async def _wallet(self, fresh: bool) -> Decimal | None:
        hit = self._wallet_hit
        if hit is not None and not fresh and time.monotonic() - hit[0] < PRODUCTS_TTL_S:
            return hit[1]
        try:
            spendable = (await self.balance()).spendable
        except SupplierError:
            return None
        self._wallet_hit = (time.monotonic(), spendable)
        return spendable

    async def products(self, *, fresh: bool = False) -> list[Product]:
        hit = self._products_hit
        if hit is not None and not fresh and time.monotonic() - hit[0] < PRODUCTS_TTL_S:
            return hit[1]
        resp = await self.client.call("/v2/getProductCode")
        data = resp.data if isinstance(resp.data, Mapping) else {}
        rows = data.get("list")
        out: list[Product] = []
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, Mapping) or r.get("type") != "save":
                continue  # only stored-value cards: share (limit) cards are not offered
            code, bin_, left = r.get("product_code"), r.get("bin"), r.get("remaining_open_card_num")
            if not (isinstance(code, str) and code and isinstance(bin_, str) and bin_):
                continue
            try:
                n = int(dec(left))
            except ValueError:
                continue
            out.append(Product(re.sub(r"\D", "", bin_), code, n))
        self._products_hit = (time.monotonic(), out)
        return out

    async def probe(self) -> ProbeReport:
        now = datetime.now(timezone.utc)
        errors: list[str] = []
        bal: Balance | None = None
        auth_ok: bool | None = None
        try:
            bal = await self.balance()
            auth_ok = True
        except SupplierError as exc:
            auth_ok = False if exc.supplier_stop else None
            errors.append(exc.reason)
            return ProbeReport(
                self.code, self.env, ok=False, auth_ok=auth_ok, errors=tuple(errors), checked_at=now
            )
        n: int | None = None
        try:
            prods = await self.products(fresh=True)
            n = len(prods)
            if pick_product(prods, DEFAULT_BINS, 0) is None:
                errors.append("no_priority_bin_product")
        except SupplierError as exc:
            errors.append(exc.reason)
        return ProbeReport(
            self.code,
            self.env,
            ok=not errors,
            auth_ok=auth_ok,
            balance=bal,
            card_products=n,
            errors=tuple(errors),
            checked_at=now,
        )

    # ---- the dedicated card (same names as ZovoCardSupplier, ADR-054)
    async def open_dedicated_card(
        self, need: Decimal, pol: FundingPolicy, key: str
    ) -> DedicatedCard:
        """Open ONE card loaded with ``need`` (whole dollars up, at least 1). Raises
        ``SupplierError``: UNAVAILABLE = nothing opened (wallet / cap / no product with a free
        slot / vendor refusal); RETRYABLE / AMBIGUOUS = it may exist, call again with the SAME key
        (the request id repeats, the card is then looked up, never created twice)."""
        amount = max(Decimal(1), need.to_integral_value(rounding=ROUND_CEILING))
        wallet = await self._wallet(True)
        if wallet is None:
            raise SupplierError(
                "wallet unreadable", outcome=Outcome.RETRYABLE, error_code="wallet_unreadable"
            )
        if pol.reserved > 0:
            wallet = max(Decimal(0), wallet - pol.reserved)
        if amount > pol.allowance:
            raise SupplierError(
                "over the funding cap", outcome=Outcome.UNAVAILABLE, error_code=CAP_REACHED
            )
        if wallet < amount:
            raise SupplierError(
                "wallet too low", outcome=Outcome.UNAVAILABLE, error_code=FUNDS_INSUFFICIENT
            )
        bins = pol.vmcardio_bins or DEFAULT_BINS
        plan = pick_product(await self.products(fresh=True), bins, pol.vmcardio_reserve_slots)
        if plan is None:
            raise SupplierError(
                "no card product with a free slot",
                outcome=Outcome.UNAVAILABLE,
                error_code="no_card_product",
            )
        ref = reference_of(key)
        body: dict[str, Any] = {
            "product_code": plan.code,
            "first_name": self.holder.first_name,
            "last_name": self.holder.last_name,
            "amount": int(amount),
            "label": ref,
        }
        address = self.holder.address()
        if address is not None:
            body["card_address"] = address
        try:
            resp = await self.client.call(
                "/v2/createCard", body, request_id=f"{ref}-open", creates=True
            )
        except SupplierError as exc:
            if exc.error_code == "duplicate_request":
                return await self._recover(amount)
            if exc.outcome is Outcome.AMBIGUOUS:
                # a card may exist. Retrying under the SAME request id is safe (a repeat answers
                # "duplicate" and the card is then looked up), so the engine retries, it does not
                # park the job: RETRYABLE
                raise SupplierError(
                    "card creation unconfirmed",
                    outcome=Outcome.RETRYABLE,
                    status=exc.status,
                    error_code=exc.error_code,
                ) from None
            raise
        data = resp.data if isinstance(resp.data, Mapping) else {}
        vid = data.get("vm_card_id")
        if not isinstance(vid, str) or not _VM_ID_RE.fullmatch(vid):
            # something may exist but we cannot name it: the same-id retry looks it up
            raise SupplierError(
                "unreadable card", outcome=Outcome.RETRYABLE, error_code="card_open_unreadable"
            )
        self._wallet_hit = None
        return DedicatedCard(PREFIX + vid, None, amount, amount)

    async def _recover(self, amount: Decimal) -> DedicatedCard:
        """The request id was used before (a lost answer or a concurrent call): find the card. The
        ONE active card created recently that holds exactly ``amount``; anything else is
        AMBIGUOUS."""
        now = datetime.now(timezone.utc)
        found: list[tuple[str, str | None]] = []
        for page in range(1, RECOVER_PAGES + 1):
            resp = await self.client.call(
                "/v2/getCardList",
                {"card_type": "save", "status": "ACTIVE", "page": page, "page_size": PAGE_SIZE},
            )
            data = resp.data if isinstance(resp.data, Mapping) else {}
            rows = data.get("list")
            rows = rows if isinstance(rows, list) else []
            for r in rows:
                if not isinstance(r, Mapping):
                    continue
                vid = r.get("vm_card_id")
                try:
                    held = dec(r.get("available_amount"))
                except ValueError:
                    continue
                if (
                    isinstance(vid, str)
                    and _VM_ID_RE.fullmatch(vid)
                    and held == amount
                    and _recent(_created(r.get("create_time")), now)
                ):
                    found.append((vid, _last4(r)))
            last_page = len(rows) < PAGE_SIZE
            del resp, data, rows  # the list carried full card numbers
            if last_page or len(found) > 1:
                break
        if len(found) != 1:
            # none yet: the list may lag, retry; several: which one is ours is for staff to say
            raise SupplierError(
                "card not confirmed",
                outcome=Outcome.RETRYABLE if not found else Outcome.AMBIGUOUS,
                error_code="open_unconfirmed",
            )
        self._wallet_hit = None
        return DedicatedCard(PREFIX + found[0][0], found[0][1], amount, amount)

    async def read_dedicated_card(self, card_id: str) -> DedicatedRead | None:
        """``/v2/cardDetail``: state, balance and the number / expiry / CVV for the ONE zzshu POST.
        The caller holds the secret in memory only. ``None`` = unreadable / not ours."""
        vid = vm_id_of(card_id)
        if vid is None:
            return None
        try:
            resp = await self.client.call("/v2/cardDetail", {"vm_card_id": vid})
        except SupplierError:
            return None
        data = resp.data if isinstance(resp.data, Mapping) else None
        if data is None:
            return None
        status = data.get("status")
        try:
            available = dec(data.get("available_amount"))
        except ValueError:
            return None
        if not isinstance(status, str):
            return None
        return DedicatedRead(status, available, _last4(data), parse_secret(data))

    async def finish_dedicated_card(self, card_id: str) -> Decimal | None:
        """The order is over (or never reached zzshu): move what is left back to the account
        (``refundCard``) and delete the card (which returns the rest). Returns the leftover that
        went back (USD) or ``None`` when nothing could be confirmed (the card stays; the caller
        audits it for staff)."""
        vid = vm_id_of(card_id)
        if vid is None:
            return None
        try:
            resp = await self.client.call("/v2/cardDetail", {"vm_card_id": vid})
        except SupplierError:
            return None
        data = resp.data if isinstance(resp.data, Mapping) else None
        if data is None:
            return None
        try:
            available = dec(data.get("available_amount"))
        except ValueError:
            return None
        status = data.get("status")
        del resp, data
        if status == "DELETED":
            return Decimal(0)
        # request ids are unique forever: a retry minutes later needs a new one
        bucket = int(time.time() // 60)
        amt = (available - REFUND_KEEP).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        if status == "ACTIVE" and amt >= Decimal("0.01"):
            try:
                await self.client.call(
                    "/v2/refundCard",
                    {"vm_card_id": vid, "amount": float(amt)},
                    request_id=f"vr-{vid}-{int(amt * 100)}-{bucket}"[:64],
                )
            except SupplierError as exc:
                log.warning("dedicated card refund refused reason=%s", exc.reason)
        try:
            await self.client.call(
                "/v2/deleteCard", {"vm_card_id": vid}, request_id=f"vd-{vid}-{bucket}"[:64]
            )
        except SupplierError as exc:
            log.warning("dedicated card delete refused reason=%s", exc.reason)
            return None
        self._wallet_hit = None
        return available

    # ---- the Supplier protocol: nothing is sold through this adapter
    async def availability(self, offer: OfferSpec) -> Availability:
        return Availability(False, "unsupported", checked_at=datetime.now(timezone.utc))

    async def submit(self, job: JobSpec) -> Result:
        return Result(Outcome.FATAL, error_code="unsupported")

    async def query(self, job: JobSpec) -> Result:
        return Result(Outcome.FATAL, error_code="unsupported")

    async def void(self, job: JobSpec) -> Result:
        return Result(Outcome.FATAL, error_code="unsupported")

    def verify_webhook(self, raw: bytes, headers: Mapping[str, str]) -> list[SupplierEvent]:
        raise WebhookRejected("vmcardio webhooks are handled by the API process")
