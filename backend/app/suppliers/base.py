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


@dataclass(frozen=True)
class Balance:
    spendable: Decimal
    currency: str
    total: Decimal | None = None


@dataclass(frozen=True)
class OfferSpec:
    """What a ``supplier_offers`` row asks for, independent of the database model."""

    kind: Literal["cdk", "vcc", "direct"]
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Availability:
    available: bool
    reason: str | None = None  # machine string when unavailable
    cost_estimate: Money | None = None
    checked_at: datetime | None = None


@dataclass(frozen=True)
class JobSpec:
    """One procurement job step (M2). ``idempotency_key`` is committed before the HTTP call."""

    job_id: str
    step: str
    idempotency_key: str
    offer: OfferSpec
    upstream_ref: str | None = None

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
