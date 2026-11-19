"""Funding-card choice of ZovoCard direct orders (ADR-048, owner request 2026-11-19).

The vendor's Open API pays every direct order from ONE card of our account (``card_id``); the
wallet only funds cards (``POST /cards/recharge``). This module is the pure part of "which card
pays, and how much must move onto it first":

* :func:`card_need` - what the card must hold (USD): the plan's price plus the vendor's balance
  buffer (``max(balanceBufferBps, minBalanceBufferUsdMinor)``) plus its service fee; a non-USD
  price (GPT plans are quoted in PHP) is taken from the offer's configured USD cost plus
  :data:`FX_MARGIN`. No price -> ``None`` and the caller shows the plan sold out (fail closed).
* :func:`parse_fund_cards` - ``GET /cards`` reduced to what the choice needs. The card number is
  only used, while in hand, for the last four and the masked number (first six + last four) that
  the engine checks against buyers' cards; it is never kept.
* :func:`plan_cards` - the cards that can pay, best first, each with the top-up it needs
  (``0`` = pays as is). Order: no money moves > the card that paid last > the owner's BIN
  priority > the lowest minimum top-up > the largest balance.

Nothing here performs I/O.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

from app.suppliers.base import FundingPolicy

# our FX rate and the vendor's can differ: a non-USD price is budgeted 3 % high
FX_MARGIN = Decimal("0.03")
# a card whose minimum top-up the vendor does not state is assumed to need this much
DEFAULT_MIN_RECHARGE = Decimal(10)
# a card product whose recharge fee rate we cannot read is budgeted at this rate (wallet check)
UNKNOWN_FEE_RATE = Decimal("0.05")
MAX_CARDS = 3  # candidates handed to the engine
CENT = Decimal("0.01")

# merchant names in a card's ``restricted_merchants`` that block a product's own checkout. Exact
# names only: "GOOGLE CHATGPT" is ChatGPT bought through Google Play, not the web checkout.
_BLOCKING = {
    "gpt": frozenset({"OPENAI", "CHATGPT", "OPENAI CHATGPT"}),
    "claude": frozenset({"CLAUDE", "ANTHROPIC"}),
    "grok": frozenset({"GROK", "XAI", "X.AI"}),
}
_CODE_RE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class FundCard:
    id: int
    last4: str | None
    status: str
    available: Decimal
    min_recharge: Decimal
    product_code: str
    bin6: str
    restricted: tuple[str, ...] = ()
    mask: str | None = field(default=None, repr=False)  # first six + last four; never stored


@dataclass(frozen=True)
class CardPlan:
    card: FundCard
    topup: Decimal  # USD to move from the wallet onto the card first; 0 = it pays as is


def _dec(v: Any) -> Decimal | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.isdigit() and len(v) <= 12:
        return int(v)
    return None


def card_need(
    base_usd: Decimal | None, plan: Mapping[str, Any] | None, *, usd_price: bool
) -> Decimal | None:
    """What the card must hold for one order, in USD (cents, rounded up). ``base_usd`` is the
    plan's price in USD: the vendor's own amount for a USD plan (``usd_price``), else the offer's
    configured cost (budgeted :data:`FX_MARGIN` high). ``None`` when no price is known."""
    if base_usd is None or base_usd <= 0:
        return None
    base = base_usd if usd_price else base_usd * (1 + FX_MARGIN)
    p = plan if isinstance(plan, Mapping) else {}
    bps = _int(p.get("balanceBufferBps")) or 0
    floor = Decimal(_int(p.get("minBalanceBufferUsdMinor")) or 0) / 100
    fee = Decimal(_int(p.get("serviceFeeUsdMinor")) or 0) / 100
    need = base + max(base * bps / 10000, floor) + fee
    return need.quantize(CENT, rounding=ROUND_CEILING)


def topup_amount(shortfall: Decimal, min_recharge: Decimal) -> Decimal:
    """The top-up that covers ``shortfall``: at least the card's minimum, in whole dollars up."""
    amt = max(shortfall, min_recharge, Decimal(1))
    return amt.to_integral_value(rounding=ROUND_CEILING)


def blocked_for(product: str, restricted: Iterable[str]) -> bool:
    names = _BLOCKING.get(product, frozenset())
    return any(str(r).strip().upper() in names for r in restricted)


def parse_fund_cards(data: Any) -> list[FundCard]:
    """``GET /cards`` [V] -> the ACTIVE / FROZEN rows (DELETED dropped). Unreadable balance or
    status -> the row is dropped (never guessed)."""
    from app.suppliers.zovocard.webhook import mask_pan  # local: no import cycle

    if not isinstance(data, Mapping):
        return []
    out: list[FundCard] = []
    for item in data.get("list") if isinstance(data.get("list"), list) else []:
        if not isinstance(item, Mapping):
            continue
        cid = _int(item.get("id"))
        status = str(item.get("status") or "").upper()[:16]
        avail = _dec(item.get("available_amount"))
        if cid is None or not 0 < cid < 10**12 or not status or status == "DELETED":
            continue
        if avail is None:
            continue
        digits = re.sub(r"\D", "", str(item.get("card_number") or ""))
        has_pan = len(digits) >= 12
        minr = _dec(item.get("min_recharge"))
        restricted = item.get("restricted_merchants")
        out.append(
            FundCard(
                id=cid,
                last4=digits[-4:] if has_pan else None,
                status=status,
                available=avail,
                min_recharge=minr if minr is not None and minr > 0 else DEFAULT_MIN_RECHARGE,
                product_code=_CODE_RE.sub("", str(item.get("product_code") or ""))[:32],
                bin6=digits[:6] if has_pan else "",
                restricted=tuple(
                    str(r)[:40] for r in (restricted if isinstance(restricted, list) else [])
                ),
                mask=mask_pan(item.get("card_number")) if has_pan else None,
            )
        )
    return out


def _bin_rank(bin6: str, priority: Sequence[str]) -> int:
    for i, prefix in enumerate(priority):
        # a priority entry may be longer than six digits (537872713): its first six must match
        if prefix and bin6 and (bin6.startswith(prefix) or prefix.startswith(bin6)):
            return i
    return len(priority)


def plan_cards(
    cards: Iterable[FundCard],
    need: Decimal,
    product: str,
    policy: FundingPolicy | None,
    wallet: Decimal | None,
    fee_rates: Mapping[str, Decimal] | None = None,
) -> list[CardPlan]:
    """The cards that can pay ``need`` now, best first (all of them; the caller still checks
    usage caps and in-flight orders). A card short of ``need`` qualifies only when the policy
    allows automatic top-ups, the top-up fits the policy's allowance and the wallet covers it
    with the product's recharge fee."""
    pol = policy or FundingPolicy()
    rates = fee_rates or {}
    plans: list[CardPlan] = []
    for c in cards:
        if c.status != "ACTIVE" or blocked_for(product, c.restricted):
            continue
        short = need - c.available
        if short <= 0:
            plans.append(CardPlan(c, Decimal(0)))
            continue
        if not pol.auto_recharge or wallet is None:
            continue
        amt = topup_amount(short, c.min_recharge)
        rate = rates.get(c.product_code, UNKNOWN_FEE_RATE)
        if amt > pol.allowance or amt * (1 + rate) > wallet:
            continue
        plans.append(CardPlan(c, amt))
    preferred = {cid: i for i, cid in enumerate(pol.preferred)}

    def key(p: CardPlan) -> tuple[Any, ...]:
        return (
            p.topup > 0,
            preferred.get(str(p.card.id), len(preferred)),
            _bin_rank(p.card.bin6, pol.bin_priority),
            p.card.min_recharge,
            -p.card.available,
            p.card.id,
        )

    return sorted(plans, key=key)


def shortfall_reason(cards: Iterable[FundCard], product: str) -> str:
    """Why no card qualifies, for staff (never shown to buyers)."""
    usable = [c for c in cards if c.status == "ACTIVE" and not blocked_for(product, c.restricted)]
    return "funds_insufficient" if usable else "no_funding_card"


_TEXT_RE = re.compile(r"[^A-Za-z0-9 ._*:/#&-]")
CHARGE_MERCHANTS = ("OPENAI", "CHATGPT", "CLAUDE", "ANTHROPIC", "GROK", "X.AI")


def parse_charges(data: Any, limit: int = 20) -> list[dict[str, Any]]:
    """``GET /cards/{id}/transactions`` [V] -> a whitelist per row for staff (ADR-048 charge
    check): time as the vendor writes it (zone not documented), amount, currency, status, type,
    merchant (sanitised, 40 chars) and ``ai`` = the merchant is an AI subscription checkout.
    No card number exists in these rows; anything unreadable is dropped."""
    rows = data if isinstance(data, list) else []
    out: list[dict[str, Any]] = []
    for r in rows[:limit]:
        if not isinstance(r, Mapping):
            continue
        amt = _dec(r.get("settle_amount")) or _dec(r.get("auth_amount"))
        merchant = _TEXT_RE.sub("", str(r.get("merchant_name") or ""))[:40]
        out.append(
            {
                "time": _TEXT_RE.sub("", str(r.get("auth_time") or ""))[:20] or None,
                "amount": str(amt) if amt is not None else None,
                "currency": _CODE_RE.sub("", str(r.get("settle_currency") or ""))[:3] or None,
                "status": _CODE_RE.sub("", str(r.get("status") or "")).upper()[:16] or None,
                "type": _CODE_RE.sub("", str(r.get("type") or ""))[:16] or None,
                "merchant": merchant or None,
                "ai": any(m in merchant.upper() for m in CHARGE_MERCHANTS),
            }
        )
    return out
