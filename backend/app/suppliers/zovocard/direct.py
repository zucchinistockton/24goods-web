"""ZovoCard ``upstream_direct`` helpers (review-v12 §18; vendor §6.18): pure parsing and mapping.

The supplier performs the subscription on the BUYER's own account with the account session the
buyer supplied. This module holds no I/O and never sees a credential except as an argument of
:func:`direct_body` (which builds the one request body; the caller sends it and drops it).

* ENUM LOCK: ``product`` is one of ``gpt|claude|grok`` (the vendor's closed list), always sent
  explicitly. The vendor silently turns an unknown ``product`` into ``gpt`` and charges it
  (pitfall 1), so anything else is refused HERE, before any call.
* ``client_request_id`` is derived from the job id (80 chars max): the vendor returns the original
  order for a repeated ``client_request_id``, a second protection next to the Idempotency-Key.
* Order status -> outcome (:func:`order_phase`): completed = done; queued / running / ... =
  pending; declined / failed_precharge / cancelled = terminal failure.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from app.suppliers.base import OfferSpec
from app.suppliers.direct_credential import DIRECT_PRODUCTS, mask_email

_PLAN_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
MAX_CARD_ID = 10**12

# Claude / Grok are constant USD prices: the vendor's pitfall 3 says to send US / USD explicitly
USD_PRODUCTS = ("claude", "grok")

# order statuses (vendor §6.18.5 / §6.18.7 / §7.5). Anything not listed here is treated as still
# in progress (never as success): the pending bound in the engine ends an order that never moves.
DONE = frozenset({"completed"})
FAILED = frozenset({"declined", "failed_precharge", "cancelled", "failed", "rejected", "expired"})
CANCELLABLE = frozenset({"queued", "awaiting_card", "funding_pending"})
NOT_CHARGED = frozenset({"cancelled", "failed_precharge"})
# a failed / cancelled order is "nothing to undo" only when its fee status says nothing is held
# (vendor 6.18.4: ``service_fee_status`` is ``held`` while reserved, ``settled`` once taken)
NOTHING_HELD_FEE = frozenset({"released", "refunded", "none"})


@dataclass(frozen=True)
class DirectSpec:
    product: str
    plan: str  # registry[].key (without the product prefix), what the order takes
    card_id: int  # the funding card (a card of OUR account) the supplier pays with
    country: str | None
    currency: str | None


def _card_id(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        n = v
    elif isinstance(v, str) and v.isdigit() and len(v) <= 13:
        n = int(v)
    else:
        return None
    return n if 0 < n < MAX_CARD_ID else None


def direct_spec(offer: OfferSpec) -> DirectSpec | None:
    """Validate a ``direct`` offer's parameters BEFORE any call (None = invalid).

    ``product`` is REQUIRED (no default: a missing one would be a silent ``gpt`` order) and must be
    in the closed enum; ``plan`` is the registry key, never a ``*_renew`` (card binding) or
    ``credit*`` (add-on) key; ``card_id`` is the funding card. Claude / Grok are pinned to
    ``US`` / ``USD``; a GPT offer may name a ``payment_country`` + ``payment_currency`` pair."""
    if offer.kind != "direct":
        return None
    p = offer.params
    product, plan = p.get("product"), p.get("plan")
    if not isinstance(product, str) or product not in DIRECT_PRODUCTS:
        return None
    if not isinstance(plan, str) or not _PLAN_RE.fullmatch(plan):
        return None
    if plan.endswith("_renew") or plan.startswith("credit"):
        return None
    card = _card_id(p.get("card_id"))
    if card is None:
        return None
    country, currency = p.get("payment_country"), p.get("payment_currency")
    if product in USD_PRODUCTS:
        if country not in (None, "US") or currency not in (None, "USD"):
            return None
        return DirectSpec(product, plan, card, "US", "USD")
    if country is None and currency is None:
        return DirectSpec(product, plan, card, None, None)  # the vendor's own GPT default
    if (
        not isinstance(country, str)
        or not _COUNTRY_RE.fullmatch(country)
        or not isinstance(currency, str)
        or not _CURRENCY_RE.fullmatch(currency)
    ):
        return None
    return DirectSpec(product, plan, card, country, currency)


def client_request_id(job_id: str) -> str:
    """The merchant-side order number: deterministic per job, ``[A-Za-z0-9]``, well under 80."""
    return f"pj{job_id}"[:80]


def preflight_body(spec: DirectSpec, credential: str) -> dict[str, Any]:
    body: dict[str, Any] = {"credential": {"mode": "session", "session": credential}}
    if spec.country:
        body["payment_country"] = spec.country
        body["payment_currency"] = spec.currency
    return body


def omit_preflight_token(base_url: str, env: str) -> bool:
    """The vendor's REAL sandbox refuses an order that carries a preflight token (HTTP 400
    ``GPT_DIRECT_ORDER_REJECTED``, "preflight token invalid") and accepts the same order without
    one (verified 2026-11-16). Production keeps the token: it pins the quote."""
    return real_sandbox(base_url, env)


def real_sandbox(base_url: str, env: str) -> bool:
    """The vendor's own sandbox host (not our simulator, not production). It has quirks that
    production does not: see :func:`omit_preflight_token` and the synthetic accounts below."""
    host = (urlsplit(base_url).hostname or "").lower()
    return env == "sandbox" and host == "sandbox.zovocard.com"


def order_body(
    spec: DirectSpec,
    *,
    credential: str,
    preflight_token: str | None,
    request_id: str,
    pricing_version: int | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "product": spec.product,
        "plan": spec.plan,
        "card_id": spec.card_id,
        "credential": {"mode": "session", "session": credential},
        "client_request_id": request_id,
        # a refused payment must stay refused (money control), not hop to another card
        "no_auto_card_switch": True,
    }
    if spec.country:
        body["payment_country"] = spec.country
        body["payment_currency"] = spec.currency
    if preflight_token:
        body["preflight_token"] = preflight_token
    if pricing_version is not None:
        body["pricing_version"] = pricing_version
    return body


@dataclass(frozen=True)
class Preflight:
    token: str | None = field(repr=False)
    account_mask: str | None
    current_plan: str | None
    expires_at: datetime | None
    quote_error: bool
    quotes: Mapping[str, tuple[int, str, int]] = field(default_factory=dict)
    # [V] real sandbox preflight: the account's state, read before any order
    blocking_reason: str = ""
    can_purchase_at: datetime | None = None
    has_active: bool = False
    recovery_required: bool = False


def parse_preflight(data: Any, parse_time: Any) -> Preflight | None:
    """``POST /gpt-direct/preflight`` answer -> what the adapter needs. ``None`` = unreadable."""
    if not isinstance(data, Mapping):
        return None
    token = data.get("preflight_token")
    if token is not None and (not isinstance(token, str) or not (8 <= len(token) <= 512)):
        return None
    plan = data.get("currentPlan")
    quotes: dict[str, tuple[int, str, int]] = {}
    raw_quotes = data.get("quotes")
    if isinstance(raw_quotes, Mapping):
        for key, q in raw_quotes.items():
            if not isinstance(q, Mapping) or not isinstance(key, str):
                continue
            minor, cur, exp = q.get("amountMinor"), q.get("currency"), q.get("minorUnitExponent", 2)
            if (
                isinstance(minor, int)
                and not isinstance(minor, bool)
                and minor >= 0
                and isinstance(cur, str)
                and isinstance(exp, int)
                and not isinstance(exp, bool)
            ):
                quotes[key] = (minor, cur.upper()[:3], exp)
    return Preflight(
        token=token or None,
        account_mask=mask_email(data.get("email")),
        current_plan=plan.strip().lower()[:40] if isinstance(plan, str) else None,
        expires_at=parse_time(data.get("preflight_expires_at")),
        quote_error=bool(str(data.get("quote_error") or "").strip()),
        quotes=quotes,
        blocking_reason=clean_reason(data.get("blocking_reason")),
        can_purchase_at=parse_time(data.get("can_purchase_at")),
        has_active=data.get("subscription_has_active") is True,
        recovery_required=data.get("subscription_recovery_required") is True,
    )


def funding_card_problem(
    status: str, available_ok: bool, remaining: int | None, waiting: bool = False
) -> str | None:
    """Why the funding card cannot pay (None = it can): not ACTIVE, or its usage cap is used up."""
    if status != "ACTIVE":
        return "funding_card_invalid"
    if remaining is not None and remaining == 0:
        return "funding_card_exhausted"
    if waiting:
        return "funding_card_busy"  # busy / cooling down: park, never send now
    if not available_ok:
        return "funding_card_low"
    return None


def usage_remaining(data: Any, plan: str) -> int | None:
    """``remaining`` of ``GET /gpt-direct/cards/{id}/usage`` for the plan's bucket (``pro20`` for
    Pro 20x, else ``light``); ``-1`` = unlimited -> ``None``. Unreadable -> ``None`` [I]."""
    if not isinstance(data, Mapping):
        return None
    bucket = data.get("pro20" if plan.startswith("pro_20x") else "light")
    rem = bucket.get("remaining") if isinstance(bucket, Mapping) else None
    if isinstance(rem, bool) or not isinstance(rem, int) or rem < 0:
        return None
    return rem


def clean_reason(v: Any) -> str:
    """``blocking_reason`` is vendor text: only its presence matters; keep a machine-safe stub."""
    s = str(v or "").strip()
    return re.sub(r"[^a-z0-9_]", "_", s.lower())[:24] if s else ""


def usage_state(data: Any, plan: str) -> tuple[int | None, bool, str | None]:
    """``(remaining, busy_or_cooling, card_status)`` of ``GET /gpt-direct/cards/{id}/usage`` [V].
    ``remaining`` -1 / unreadable -> ``None``; ``busy`` / ``cooling`` = not usable right now."""
    if not isinstance(data, Mapping):
        return None, False, None
    waiting = data.get("busy") is True or data.get("cooling") is True
    st = data.get("card_status")
    return usage_remaining(data, plan), waiting, st.upper() if isinstance(st, str) else None


def order_id_of(data: Any) -> str | None:
    """The vendor order id from an order object (``id`` on create, ``order_id`` in details)."""
    if not isinstance(data, Mapping):
        return None
    for key in ("id", "order_id"):
        v = data.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, str)):
            continue
        s = str(v)
        if s.isdigit() and len(s) <= 20:
            return s
    return None


def order_phase(status: Any) -> str:
    """``done`` | ``failed`` | ``pending`` (unknown = pending, never success)."""
    s = str(status or "").strip().lower()
    if s in DONE:
        return "done"
    if s in FAILED:
        return "failed"
    return "pending"


def delivery_text(spec: DirectSpec, account_mask: str | None, order_ref: str) -> str:
    """What the buyer sees: plan, masked account, supplier order. Nothing secret."""
    return "----".join((f"{spec.product}/{spec.plan}", account_mask or "-", order_ref))


__all__ = [
    "CANCELLABLE",
    "DIRECT_PRODUCTS",
    "DONE",
    "FAILED",
    "NOTHING_HELD_FEE",
    "NOT_CHARGED",
    "USD_PRODUCTS",
    "DirectSpec",
    "Preflight",
    "client_request_id",
    "delivery_text",
    "direct_spec",
    "order_body",
    "order_id_of",
    "order_phase",
    "parse_preflight",
    "preflight_body",
]
