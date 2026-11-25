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
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from app.suppliers.base import IDEMPOTENCY_KEY_RE, OfferSpec
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


def failed_precharge_clean(order: Mapping[str, Any]) -> bool:
    """ADR-054: the order failed DEFINITELY before any charge. Only ``failed_precharge`` whose
    service fee is released / refunded / none and whose funding hold (when the vendor states one)
    is too. Anything else (declined, cancelled, a held or settled fee, a missing fee status) may
    have charged or holds money: never a reason to try another source."""
    if str(order.get("status") or "").strip().lower() != "failed_precharge":
        return False
    fee = str(order.get("service_fee_status") or "").strip().lower()
    hold = str(order.get("funding_hold_status") or "").strip().lower()
    return fee in NOTHING_HELD_FEE and hold in ("", *NOTHING_HELD_FEE)


# issue #29 / review-v12 §21: how the funding card of a direct order is chosen
CARD_MODES = ("fixed", "auto_existing")
CARD_MODE_RESERVED = "auto_issue"  # issuing a NEW card per order: reserved, refused (§21.4)
# params key the ENGINE injects for the card it picked for a job (never an admin parameter)
PICKED_CARD_KEY = "picked_card_id"
# ... and the top-up (USD cents) the engine planned for that card before the order (ADR-048)
PICKED_TOPUP_KEY = "picked_topup_cents"
PICKED_TOPUP_IDEM = "picked_topup_key"  # the key the top-up is sent under (its own record)
MAX_TOPUP_CENTS = 1_000_000  # USD 10,000: anything above is not a top-up we planned
INVALID_CARD = "invalid"  # ``order_card`` marker: the vendor reported an unusable card id
MAX_CARD_TRIES = 3


@dataclass(frozen=True)
class DirectSpec:
    product: str
    plan: str  # registry[].key (without the product prefix), what the order takes
    # the funding card (a card of OUR account) the supplier pays with. ``fixed``: the offer's
    # ``card_id``. ``auto_existing``: ``None`` until the engine has picked a card for the job
    card_id: int | None
    country: str | None
    currency: str | None
    card_mode: str = "fixed"
    vendor_switch: bool = False  # True: let the vendor switch card after a refused payment


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
    mode = p.get("card_mode", "fixed")
    if mode not in CARD_MODES:  # ``auto_issue`` and anything else: refused
        return None
    switch = p.get("vendor_switch", False)
    if not isinstance(switch, bool):
        return None
    if mode == "fixed":
        card = _card_id(p.get("card_id"))
        if card is None:
            return None
    else:  # auto_existing: the offer's own card_id is never used; only the engine's pick
        card = _card_id(p.get(PICKED_CARD_KEY))
    country, currency = p.get("payment_country"), p.get("payment_currency")
    if product in USD_PRODUCTS:
        if country not in (None, "US") or currency not in (None, "USD"):
            return None
        return DirectSpec(product, plan, card, "US", "USD", mode, switch)
    if country is None and currency is None:
        return DirectSpec(product, plan, card, None, None, mode, switch)  # vendor default
    if (
        not isinstance(country, str)
        or not _COUNTRY_RE.fullmatch(country)
        or not isinstance(currency, str)
        or not _CURRENCY_RE.fullmatch(currency)
    ):
        return None
    return DirectSpec(product, plan, card, country, currency, mode, switch)


def is_half_plan(plan: object) -> bool:
    """``plus_half`` and the Grok ``*_half`` keys (half price needs an eligible account)."""
    return isinstance(plan, str) and (plan == "plus_half" or plan.endswith("_half"))


def client_request_id(job_id: str, attempt: int = 0) -> str:
    """The merchant-side order number: deterministic per job (and per re-keyed attempt after a
    verified card refusal, review-v12 §21.3), ``[A-Za-z0-9]``, well under 80."""
    return (f"pj{job_id}" if attempt <= 0 else f"pj{job_id}a{attempt}")[:80]


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
    allow_switch: bool = True,
    payment_currency: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "product": spec.product,
        "plan": spec.plan,
        "card_id": spec.card_id,
        "credential": {"mode": "session", "session": credential},
        "client_request_id": request_id,
        # a refused payment stays refused (money control), not hop to another card, unless the
        # source opted in to ``vendor_switch`` (extra failed attempts cost ``failed_fee``, §21.2)
        # (never switched on a production supplier: the vendor cannot restrict the switch pool yet)
        "no_auto_card_switch": not (spec.vendor_switch and allow_switch),
    }
    if spec.country:
        body["payment_country"] = spec.country
        body["payment_currency"] = spec.currency
    if payment_currency:  # an upgrade pays in the account's own bill currency (vendor)
        body["payment_currency"] = payment_currency
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
    # vendor ``promotions`` (informational: names only, never decisive on their own) [I]
    promotions: tuple[str, ...] = ()


# ---- upgrades of an existing subscription (owner 2026-11-22; vendor "upgrade-quotes")
# plan order for "is the target higher than what the account holds"
PLAN_RANK: dict[str, int] = {"free": 0, "go": 1, "plus": 2, "pro_5x": 3, "pro_20x": 4, "pro_50x": 5}
UPGRADE_TARGETS = frozenset({"pro_5x", "pro_20x", "pro_50x"})
UPGRADE_FUNDING_BUFFER = Decimal("1.10")  # vendor: funding_usd_minor = difference x rate + 10 %


@dataclass(frozen=True)
class UpgradeQuote:
    """One ``available`` entry of ``POST /gpt-direct/upgrade-quotes``: what THIS account pays to
    move to ``plan`` (its own bill currency), valid about 5 minutes, bound to its token."""

    plan: str
    current_plan: str | None
    currency: str
    amount_minor: int
    exponent: int
    funding_usd: Decimal  # the vendor's card funding estimate (difference + 10 %)
    token: str = field(repr=False)

    @property
    def usd(self) -> Decimal:
        """The difference in USD (the vendor's estimate without its 10 % margin)."""
        return (self.funding_usd / UPGRADE_FUNDING_BUFFER).quantize(Decimal("0.01"))

    def view(self) -> dict[str, Any]:
        return {
            "plan": self.plan,
            "current_plan": self.current_plan,
            "currency": self.currency,
            "amount": str(Decimal(self.amount_minor).scaleb(-self.exponent)),
            "usd": str(self.usd),
        }


def upgradable(pf: Preflight, plan: str) -> bool:
    """The account holds a paid plan below ``plan`` and ``plan`` is an upgrade target."""
    cur = pf.current_plan or "free"
    return (
        plan in UPGRADE_TARGETS
        and PLAN_RANK.get(cur, 0) >= 1
        and PLAN_RANK.get(plan, 0) > PLAN_RANK.get(cur, 0)
    )


def parse_upgrade_quote(data: Any, plan: str) -> UpgradeQuote | None:
    """The ``available`` quote for ``plan`` in an ``upgrade-quotes`` answer, else ``None``."""
    rows = data.get("quotes") if isinstance(data, Mapping) else None
    for q in rows if isinstance(rows, list) else []:
        if not isinstance(q, Mapping) or q.get("plan") != plan or q.get("available") is not True:
            continue
        minor, fund = q.get("amount_minor"), q.get("funding_usd_minor")
        exp = q.get("minor_unit_exponent", 2)
        cur, tok = q.get("currency"), q.get("preflight_token")
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in (minor, exp, fund)):
            continue
        if not isinstance(cur, str) or not isinstance(tok, str):
            continue
        if not 8 <= len(tok) <= PREFLIGHT_TOKEN_MAX:
            continue
        if minor <= 0 or fund <= 0 or not 0 <= exp <= 4:
            continue
        cp = q.get("current_plan")
        return UpgradeQuote(
            plan=plan,
            current_plan=cp.strip().lower()[:40] if isinstance(cp, str) else None,
            currency=cur.upper()[:3],
            amount_minor=minor,
            exponent=exp,
            funding_usd=Decimal(fund) / 100,
            token=tok,
        )
    return None


# the vendor refused the buyer's session itself (nothing was charged): the buyer must paste a new
# one. ``*_SESSION_EXPIRED`` is undocumented; first seen on production 2026-11-20 ("Session has
# expired or has no access, get the full Session again; not charged").
SESSION_REJECTED_CODES = frozenset(
    {
        "SESSION_REQUIRED",
        "GPT_SESSION_INVALID",
        "CLAUDE_SESSION_INVALID",
        "GROK_SESSION_INVALID",
        "GPT_SESSION_EXPIRED",
        "CLAUDE_SESSION_EXPIRED",
        "GROK_SESSION_EXPIRED",
    }
)

# the vendor's preflight token is a compact JWE (five base64url parts) of ~9.8 KB, measured
# 2026-11-20 on production; it was a short opaque string before. Kept in memory only.
PREFLIGHT_TOKEN_MAX = 32768


def parse_preflight(data: Any, parse_time: Any) -> Preflight | None:
    """``POST /gpt-direct/preflight`` answer -> what the adapter needs. ``None`` = unreadable."""
    if not isinstance(data, Mapping):
        return None
    token = data.get("preflight_token")
    if token is not None and (
        not isinstance(token, str) or not (8 <= len(token) <= PREFLIGHT_TOKEN_MAX)
    ):
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
        promotions=_promotions(data.get("promotions")),
    )


def _promotions(raw: Any) -> tuple[str, ...]:
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        name = item if isinstance(item, str) else None
        if isinstance(item, Mapping):
            name = next(
                (item[k] for k in ("key", "id", "plan", "name") if isinstance(item.get(k), str)),
                None,
            )
        if name:
            out.append(clean_reason(name))
    return tuple(out[:8])


def half_eligibility(
    pf: Preflight, plan: str, now: datetime, *, quotes_optional: bool = False
) -> tuple[bool, str]:
    """Is the account eligible for a half-price plan, from the vendor's preflight (the same fields
    the engine's own gate reads, so the check and the execution agree): not blocked, not waiting
    for ``can_purchase_at``, no active / recovering subscription, no ``quote_error``, and a quote
    for the half-price key itself (the vendor quotes it only to an eligible account) [I].
    ``quotes_optional``: the vendor's REAL sandbox never quotes (``quotes: null``) [V]."""
    if pf.blocking_reason:
        return False, "preflight_blocked"
    if pf.can_purchase_at is not None and pf.can_purchase_at > now:
        return False, "purchase_not_yet"
    if pf.recovery_required:
        return False, "subscription_recovery_required"
    if pf.has_active:
        return False, "subscription_active"
    if pf.quote_error:
        return False, "quote_error"
    if plan not in pf.quotes and not (quotes_optional and not pf.quotes):
        return False, "plan_not_quoted"
    return True, ""


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


# Vendor refusals that mean "no card can pay this order right now": the order is not created, no
# money moves, the job waits (review-v12 §21.3). [V] = in the vendor's list of owner-card refusals
# (§6.18.8a, written for the CDK owner path, applied here by analogy [I]). ONLY these may park a
# job and re-key its next attempt, and only after a lookup verified that no order exists.
CARD_REFUSAL_CODES = frozenset(
    {
        "CARD_FUNDING_BUSY",  # [V]
        "CARD_IN_USE",  # [V]
        "CARD_CAPACITY_REACHED",  # [V]
        "CARD_CAPACITY_PRO20",  # [V]
        "CARD_COOLING_DOWN",  # [V]
    }
)
# our own guesses at the wording of "no card / pool empty / supplier busy": NOT in the docs. They
# are not mapped in ``errors.TABLE`` and never park or re-key: the job goes to ``needs_manual``.
CARD_GUESSED_CODES = frozenset({"NO_CARD_AVAILABLE", "CARD_POOL_EMPTY", "SUPPLIER_BUSY"})
CARD_WAIT_CODES = CARD_REFUSAL_CODES  # (kept name) the codes that mean a wait
# reasons of our own availability answer that park a job (never a failover, never a failure)
NO_CARD_PICK = "no_card_pick"  # the vendor picked none, or every candidate failed
CARD_POOL_BUSY = "card_pool_busy"  # candidates exist but are busy / cooling / in flight
WAIT_REASONS = frozenset({NO_CARD_PICK, CARD_POOL_BUSY, *CARD_REFUSAL_CODES})
# ADR-048: no card, no money, no price. The plan shows sold out; a PAID order that meets one of
# these goes to staff at once (owner 2026-11-19: never wait out the card wait for missing money)
FUNDS_INSUFFICIENT = "funds_insufficient"  # neither a card nor an allowed top-up covers it
NO_FUNDING_CARD = "no_funding_card"  # no ACTIVE card of ours may pay this product
COST_UNKNOWN = "cost_unknown"  # the order's price cannot be worked out: never sent
FUNDING_CAP_REACHED = "funding_cap_reached"  # the daily top-up cap is used up
MANUAL_NOW = frozenset(
    {FUNDS_INSUFFICIENT, NO_FUNDING_CARD, COST_UNKNOWN, FUNDING_CAP_REACHED, "topup_below_min"}
)


def picked_topup(params: Mapping[str, Any]) -> tuple[Decimal, str] | None:
    """The planned top-up of the engine's picked card: ``(USD, idempotency key)``; ``None`` =
    none (or unreadable: then nothing is moved and the pre-send card read decides)."""
    v, key = params.get(PICKED_TOPUP_KEY), params.get(PICKED_TOPUP_IDEM)
    if isinstance(v, bool) or not isinstance(v, int) or not 0 < v <= MAX_TOPUP_CENTS:
        return None
    if not isinstance(key, str) or not IDEMPOTENCY_KEY_RE.fullmatch(key):
        return None
    return Decimal(v) / 100, key


def _id_of(v: Any) -> int | None:
    if isinstance(v, Mapping):
        for key in ("card_id", "id"):
            n = _card_id(v.get(key))
            if n is not None:
                return n
        return None
    return _card_id(v)


def parse_schedule(data: Any) -> list[int]:
    """``GET /gpt-direct/card-pool/schedule`` -> card ids to try, best first: ``data.pick`` then
    the ``data.candidates`` that carry no ``skip_reason`` (vendor §6.18.8, [I] field spelling: the
    id is ``card_id`` or ``id``). At most :data:`MAX_CARD_TRIES`; unreadable -> empty."""
    if not isinstance(data, Mapping):
        return []
    out: list[int] = []
    pick = _id_of(data.get("pick"))
    if pick is not None:
        out.append(pick)
    cands = data.get("candidates")
    for item in cands if isinstance(cands, list) else []:
        if isinstance(item, Mapping) and item.get("skip_reason"):
            continue
        cid = _id_of(item)
        if cid is not None and cid not in out:
            out.append(cid)
    return out[:MAX_CARD_TRIES]


def last4_of(v: Any) -> str | None:
    """Four digits or ``None``: the only part of a card number we ever record."""
    s = str(v or "").strip()
    return s if re.fullmatch(r"[0-9]{4}", s) else None


def order_card(order: Any) -> tuple[str | None, str | None]:
    """The card an order really paid with: ``(card_id, last four)`` from the order object
    (vendor §6.18.8a: ``card_id`` and ``card_last_four``; the last four may be empty)."""
    if not isinstance(order, Mapping):
        return None, None
    raw = order.get("card_id")
    cid = _card_id(raw)
    if cid is None and raw not in (None, "", 0):
        return INVALID_CARD, None  # reported, but not a card id we can use: never guessed at
    return (str(cid) if cid is not None else None), last4_of(order.get("card_last_four"))


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
    "CARD_GUESSED_CODES",
    "CARD_MODES",
    "CARD_REFUSAL_CODES",
    "CARD_WAIT_CODES",
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
    "failed_precharge_clean",
    "order_body",
    "order_id_of",
    "order_phase",
    "parse_preflight",
    "preflight_body",
]
