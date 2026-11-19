"""Supplier protocol and result types (review-v12 §3, ADR-038).

The procurement engine only understands :class:`Outcome`; every supplier quirk (status codes,
error codes, envelopes) stays inside its adapter. Nothing in ``app.suppliers`` imports from
``app.modules`` (the API) or touches the database.

Money is always :class:`decimal.Decimal` built from ``str(x)``; floats are never used for
arithmetic.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol, runtime_checkable

SupplierEnv = Literal["sandbox", "production"]
SUPPLIER_ENVS: tuple[str, ...] = ("sandbox", "production")

# Vendor idempotency keys (ZovoCard appendix A): letters, digits and ``._:-``, 1-80 chars.
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
# Supplier codes double as the prefix of their secrets in the procurement env file.
SUPPLIER_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
# adapters whose credentials come from the procurement env file (``<CODE>_[SANDBOX_]APP_SECRET``)
CREDENTIAL_ADAPTERS: tuple[str, ...] = ("zovocard",)


_DETAIL_BAD = re.compile(r"[^A-Za-z0-9_.:-]")


def clean_detail(value: object, limit: int = 64) -> str:
    """Machine string safe for health_detail / audit rows: ``[A-Za-z0-9_.:-]``, length-capped."""
    return _DETAIL_BAD.sub("_", str(value))[:limit] or "unknown"


def supplier_code_problem(code: str, adapter: str) -> str | None:
    """Why ``code`` is not acceptable for ``adapter`` (None = fine). Mirrors the DB checks.

    * ``..._sandbox`` is refused: ``ZOVOCARD_SANDBOX_SANDBOX_...`` style names would let one code
      read another code's sandbox credentials (``x_sandbox`` + production = ``X_SANDBOX_APP_*``).
    * a credential adapter's code is the adapter itself or ``<adapter>_<suffix>``, so a row can
      only ever select credentials of its own vendor.
    """
    if not SUPPLIER_CODE_RE.fullmatch(code):
        return "invalid_code"
    if code.endswith("_sandbox"):
        return "reserved_suffix"
    if adapter in CREDENTIAL_ADAPTERS and code != adapter and not code.startswith(adapter + "_"):
        return "code_adapter_mismatch"
    return None


class Outcome(enum.Enum):
    """What the engine does next (review-v12 §3 table)."""

    OK = "ok"  # seal secrets, emit procure.fulfilled
    ACCEPTED_PENDING = "accepted_pending"  # wait for webhook / poll query(); never resubmit
    RETRYABLE = "retryable"  # same idempotency key, backoff
    UNAVAILABLE = "unavailable"  # definite "no charge": the next offer may be tried
    FATAL = "fatal"  # deterministic business error: needs_manual + staff alert
    AMBIGUOUS = "ambiguous"  # query() first; resubmit only with the SAME key


class Capability(enum.Enum):
    CDK = "cdk"
    VCC = "vcc"
    DIRECT = "direct"
    VOID = "void"
    WEBHOOK = "webhook"


class SupplierError(Exception):
    """A supplier call failed. ``outcome`` tells the engine what to do; never carries bodies."""

    def __init__(
        self,
        message: str,
        *,
        outcome: Outcome,
        status: int | None = None,
        error_code: str | None = None,
        supplier_stop: bool = False,
        retry_after: timedelta | None = None,
    ) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.status = status
        self.error_code = error_code
        self.supplier_stop = supplier_stop
        self.retry_after = retry_after

    @property
    def reason(self) -> str:
        """Short machine string for health/probe reports: the error code or ``http_<status>``."""
        if self.error_code:
            return self.error_code[:64]
        if self.status is not None:
            return f"http_{self.status}"
        return str(self)[:64]


class WebhookRejected(Exception):
    """Bad signature or malformed webhook. Callers answer 401 without detail."""


def dec(value: Any) -> Decimal:
    """``Decimal(str(x))`` for vendor numbers (JSON floats included). Rejects NaN/inf/bools."""
    if isinstance(value, bool) or value is None:
        raise ValueError("not a number")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("not a number") from None
    if not d.is_finite():
        raise ValueError("not a finite number")
    return d


CARD_FIELD_SEP = "----"  # the sealed card secret is ``<pan>----<MM/YY>----<cvv>``
_PAN_RE = re.compile(r"^[0-9]{12,19}$")
_EXP_RE = re.compile(r"^(0[1-9]|1[0-2])/([0-9]{2}|[0-9]{4})$")
_CVV_RE = re.compile(r"^[0-9]{3,4}$")


def card_secret(pan: str, expiry: str, cvv: str) -> str | None:
    """The ONE sealed secret of a virtual card, or ``None`` for malformed fields. Used for what a
    supplier returns and for a hand-pasted card, so both end up byte-identical in the vault."""
    pan = re.sub(r"[ -]", "", str(pan or ""))
    expiry, cvv = str(expiry or "").strip(), str(cvv or "").strip()
    if not (_PAN_RE.fullmatch(pan) and _EXP_RE.fullmatch(expiry) and _CVV_RE.fullmatch(cvv)):
        return None
    return CARD_FIELD_SEP.join((pan, expiry, cvv))


def canonical_card_secret(text: str) -> str | None:
    """A pasted ``pan----MM/YY----cvv`` normalised (spaces in the number removed), or ``None``."""
    parts = str(text or "").strip().split(CARD_FIELD_SEP)
    return card_secret(*parts) if len(parts) == 3 else None


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError("Money.amount must be a Decimal")


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    error_code: str | None = None
    upstream_ref: str | None = None
    # plaintext secrets (CDK code, PAN ...): the caller seals them immediately and drops them
    secrets: tuple[str, ...] = field(default=(), repr=False)
    cost: Money | None = None
    retry_after: timedelta | None = None
    supplier_stop: bool = False  # account-level refusal: open the supplier breaker
    card_mask: str | None = None  # vcc: masked card number (never the full one)
    # direct: masked account e-mail the supplier's preflight reported, and the token's expiry
    account_mask: str | None = None
    preflight_expires_at: datetime | None = None
    funding_mask: str | None = None  # availability only; never recorded on a job
    # direct: the card the vendor order paid with (id + last four only, never a number), and, from
    # a precheck, the cards that passed our checks (``auto_existing``), best first
    card_id: str | None = None
    card_last4: str | None = None
    card_candidates: tuple[tuple[str, str | None], ...] = ()
    candidate_masks: tuple[tuple[str, str], ...] = field(default=(), repr=False)  # as Availability
    card_topups: tuple[tuple[str, Decimal], ...] = ()  # as Availability
    # direct submit: the USD the adapter moved from the wallet onto the funding card (ADR-048)
    funded: Decimal | None = None
    # direct submit: the vendor refused BEFORE an order existed AND the lookup by client request id
    # verified that none exists (the only case in which a new attempt key is allowed)
    no_order_verified: bool = False
    # direct: short machine summary of the vendor order (``status:fee_status``), shown to staff
    detail: str | None = None
    # direct: whitelist of the vendor order read (state fields + sanitised timeline events), see
    # ``suppliers/direct_progress.py``; never a raw body
    vendor: dict[str, Any] | None = None

    @classmethod
    def from_error(
        cls,
        exc: SupplierError,
        *,
        upstream_ref: str | None = None,
        card_mask: str | None = None,
        account_mask: str | None = None,
    ) -> Result:
        return cls(
            outcome=exc.outcome,
            error_code=exc.reason,
            retry_after=exc.retry_after,
            supplier_stop=exc.supplier_stop,
            upstream_ref=upstream_ref,
            card_mask=card_mask,
            account_mask=account_mask,
        )


@dataclass(frozen=True)
class Balance:
    spendable: Decimal
    currency: str
    total: Decimal | None = None


@dataclass(frozen=True)
class LedgerRow:
    """One vendor balance-log row, normalised (review-v12 §6). ``card_mask`` is already masked:
    a full card number never leaves the adapter. ``currency`` empty = the supplier's currency."""

    external_id: str
    type: str
    amount: Decimal  # signed: positive = credit, negative = debit
    currency: str
    occurred_at: datetime | None
    balance_after: Decimal | None = None
    ref_id: str | None = None
    card_mask: str | None = None
    card_id: str | None = None  # card rows: the vendor card id (links the row to its job)


@dataclass(frozen=True)
class FundingPolicy:
    """How a direct order's funding card may be topped up from the supplier wallet (ADR-048).
    Built by the procurement process from the tenant's ``funding_settings`` and today's top-ups;
    the adapter only reads it. ``allowance`` is the most ONE automatic top-up may move now (the
    per-order cap, bounded by what is left of the daily cap)."""

    auto_recharge: bool = False
    allowance: Decimal = Decimal(0)
    bin_priority: tuple[str, ...] = ()
    preferred: tuple[str, ...] = ()  # card ids, the one that paid last first


@dataclass(frozen=True)
class OfferSpec:
    """What a ``supplier_offers`` row asks for, independent of the database model."""

    kind: Literal["cdk", "vcc", "direct"]
    params: Mapping[str, Any] = field(default_factory=dict)
    # the offer's configured cost in USD (``supplier_offers.cost_estimate`` when its currency is
    # USD): the price a direct plan quoted in another currency is budgeted from
    cost_usd: Decimal | None = None
    funding: FundingPolicy | None = None


@dataclass(frozen=True)
class Availability:
    available: bool
    reason: str | None = None  # machine string when unavailable
    cost_estimate: Money | None = None
    checked_at: datetime | None = None
    # direct: masked number of the funding card (first 6 + last 4), so the caller can refuse a card
    # that was bought for a buyer
    funding_mask: str | None = None
    # direct, ``auto_existing``: cards that passed our checks, best first (id, last four or None)
    card_candidates: tuple[tuple[str, str | None], ...] = ()
    # the candidates' masked numbers (id, mask): checked against buyers' cards by the engine in the
    # same process, never stored, logged or audited
    candidate_masks: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    # direct: the USD top-up a candidate needs before it can pay (absent = none), ADR-048
    card_topups: tuple[tuple[str, Decimal], ...] = ()


@dataclass(frozen=True)
class JobSpec:
    """One procurement job step (M2). ``idempotency_key`` is committed before the HTTP call."""

    job_id: str
    step: str
    idempotency_key: str
    offer: OfferSpec
    upstream_ref: str | None = None
    # upstream_direct ONLY: the buyer's account session, opened in memory by the engine for this one
    # call. A live credential: hidden from ``repr``, never logged, never copied anywhere.
    credential: str | None = field(default=None, repr=False)
    # direct: the masked account the job's FIRST preflight reported (a later one must match),
    # the most the order may cost (USD, offer cost x (1 + tolerance)) and that tolerance
    account_mask: str | None = None
    cost_bound: Money | None = None
    quote_tolerance: Decimal = Decimal("0.10")
    # direct: re-keyed attempt of the job (0 = the first key); the client request id derives from it
    attempt: int = 0

    def __post_init__(self) -> None:
        if not IDEMPOTENCY_KEY_RE.fullmatch(self.idempotency_key):
            raise ValueError("idempotency key must match ^[A-Za-z0-9._:-]{1,80}$")


def job_idempotency_key(job_hex: str, step: str) -> str:
    """``pj:<job uuid hex>:<step>`` (review-v12 §4); validated against the vendor format."""
    key = f"pj:{job_hex}:{step}"
    if not IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise ValueError("invalid idempotency key")
    return key


@dataclass(frozen=True)
class ProbeReport:
    """Read-only connection test. Contains no secrets and no raw response bodies."""

    supplier: str
    env: str
    ok: bool
    auth_ok: bool | None = None
    ip_allowed: bool | None = None
    permissions: Mapping[str, bool] = field(default_factory=dict)
    balance: Balance | None = None
    purchasable_plans: int | None = None
    card_products: int | None = None
    errors: tuple[str, ...] = ()
    checked_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "supplier": self.supplier,
            "env": self.env,
            "ok": self.ok,
            "auth_ok": self.auth_ok,
            "ip_allowed": self.ip_allowed,
            "permissions": dict(self.permissions),
            "spendable_balance": str(self.balance.spendable) if self.balance else None,
            "currency": self.balance.currency if self.balance else None,
            "purchasable_plans": self.purchasable_plans,
            "card_products": self.card_products,
            "errors": list(self.errors),
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
        }


@dataclass(frozen=True)
class SupplierEvent:
    """A verified webhook event (M2). ``payload`` is already masked (no PAN, no OTP)."""

    kind: str
    dedup_key: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    upstream_ref: str | None = None


@runtime_checkable
class Supplier(Protocol):
    code: str
    env: str
    capabilities: frozenset[Capability]

    async def probe(self) -> ProbeReport: ...

    async def balance(self) -> Balance: ...

    async def availability(self, offer: OfferSpec) -> Availability: ...

    async def submit(self, job: JobSpec) -> Result: ...

    async def query(self, job: JobSpec) -> Result: ...

    async def void(self, job: JobSpec) -> Result: ...

    def verify_webhook(self, raw: bytes, headers: Mapping[str, str]) -> list[SupplierEvent]: ...

    async def aclose(self) -> None: ...
