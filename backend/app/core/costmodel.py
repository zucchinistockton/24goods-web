"""Cost model (issue #28, review-v12 §19): pure ``Decimal`` formulas and the fee schedule.

Framework-free. Everything is USD unless a name says otherwise; the shop currency (CNY by
default) is converted with a staff-entered rate (``fx``). Nothing here touches the database or
changes a price: the router shows estimates and recommendations only.

Row identity is ``(product_id, variant_id, offer_id, region)``; ``region`` is always ``None``
until regions exist (#27) so the dimension can be added without reshaping rows or settings.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

SETTINGS_KEY = "cost_model"
CENT = Decimal("0.01")
MICRO = Decimal("0.000001")
MAX_TARGETS = 500
DIRECT_PRODUCTS = ("gpt", "grok", "claude")
CARD_TIERS = ("standard", "pro20x")
MONEY_RE = re.compile(r"^[0-9]{1,9}(\.[0-9]{1,6})?$")

# Decimal strings everywhere: (low, high) bounds, inclusive. Rates are fractions (0.02 = 2 %).
_MONEY_FIELDS: dict[str, tuple[str, str]] = {
    "card_open_fee": ("1.50", "1000"),
    "topup_fee_fixed": ("0", "1000"),
    "failed_fee": ("0.30", "1000"),
    "chargeback_tier1_surcharge": ("0.30", "1000"),
    "chargeback_tier2_deposit": ("200", "1000000"),
}
_RATE_FIELDS: dict[str, str] = {
    "topup_fee_rate": "0.02",
    "auth_reversal_rate": "0.01",
    "chargeback_tier1_rate": "0.15",
    "chargeback_tier2_rate": "0.30",
    "expected_failure_rate": "0.05",
    "target_margin": "0.30",
    # owner rule 2026-11-18 ("12 % profit on ChatGPT": cost 100 -> price 112): when above 0, the
    # recommendation is the price-independent cost x (1 + target_markup) and ``target_margin`` is
    # not used; the payment channel fee is not added (the buyer pays it on top at checkout)
    "target_markup": "0",
    "fx_max_change": "0.2",
}
_INT_FIELDS: dict[str, tuple[int, int, int]] = {  # default, lo, hi
    "failed_free_count": (3, 0, 1000),
    # vendor_switch: the most extra card attempts the vendor may make after a refused payment
    # (vendor default 2); the cost bound of the switch assumes all of them are used and billed
    "max_auto_switches": (2, 2, 10),  # floor = the vendor default: we send no switch cap
    "chargeback_tier1_min_tx": (100, 0, 1_000_000),
    "round_step_minor": (100, 1, 100_000),  # whole shop-currency units (owner 2026-11-19)
    "fx_max_age_days": (7, 1, 365),
}
DEFAULT_SERVICE_FEE = {"gpt": "0.30", "grok": "0.15", "claude": "0"}
DEFAULT_USES = {"standard": 3, "pro20x": 3}

DEFAULTS: dict[str, Any] = {
    **{k: v[0] for k, v in _MONEY_FIELDS.items()},
    **_RATE_FIELDS,
    **{k: v[0] for k, v in _INT_FIELDS.items()},
    "service_fee_direct": dict(DEFAULT_SERVICE_FEE),
    "uses_per_card": dict(DEFAULT_USES),
    # {"CNY": {"per_usd": "7.20", "updated_at": iso, "updated_by": uuid}}: staff-entered
    "fx": {},
    # plausible currency-units-per-USD band per currency; an entry outside it is refused
    "fx_bands": {"CNY": {"min": "3", "max": "15"}},
    "fx_max_age_days": 7,
    "fx_max_change": "0.2",
    # {"<product_id>" | "<product_id>:<variant_id>": "0.35"}: per-target margin override
    "targets": {},
}
# informational only (never part of a unit estimate): shown next to the schedule
INFO_FIELDS = (
    "failed_free_count",
    "auth_reversal_rate",
    "chargeback_tier1_rate",
    "chargeback_tier1_min_tx",
    "chargeback_tier1_surcharge",
    "chargeback_tier2_rate",
    "chargeback_tier2_deposit",
)


class CostSettingsError(ValueError):
    def __init__(self, msg: str, code: str = "bad_cost_settings") -> None:
        super().__init__(msg)
        self.code = code


def _dec(raw: Any) -> Decimal | None:
    if isinstance(raw, bool) or raw is None:
        return None
    s = str(raw).strip()
    if len(s) > 16 or not MONEY_RE.match(s):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:  # pragma: no cover - the regex already guards it
        return None


def _s(d: Decimal) -> str:
    return format(d.normalize(), "f") if d != 0 else "0"


def normalize(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Complete, type-correct settings; invalid stored values fall back to the default."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    for k, (default, hi) in _MONEY_FIELDS.items():
        v = _dec(raw.get(k))
        out[k] = _s(v) if v is not None and v <= Decimal(hi) else _s(Decimal(default))
    for k, default in _RATE_FIELDS.items():
        v = _dec(raw.get(k))
        out[k] = _s(v) if v is not None and v <= 1 else _s(Decimal(default))
    for k, (default, lo, hi) in _INT_FIELDS.items():
        v = raw.get(k)
        ok = isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi
        out[k] = v if ok else default
    sf = raw.get("service_fee_direct")
    sf = sf if isinstance(sf, dict) else {}
    out["service_fee_direct"] = {}
    for p in DIRECT_PRODUCTS:
        v = _dec(sf.get(p))
        out["service_fee_direct"][p] = (
            _s(v) if v is not None and v <= 1000 else _s(Decimal(DEFAULT_SERVICE_FEE[p]))
        )
    up = raw.get("uses_per_card")
    up = up if isinstance(up, dict) else {}
    out["uses_per_card"] = {}
    for t in CARD_TIERS:
        v = up.get(t)
        ok = isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 1000
        out["uses_per_card"][t] = v if ok else DEFAULT_USES[t]
    out["fx_bands"] = _norm_bands(raw.get("fx_bands"), raw.get("fx_bands") is None)
    out["fx"] = _norm_fx(raw.get("fx"))
    out["targets"] = _norm_targets(raw.get("targets"))
    return out


def _norm_bands(raw: Any, use_default: bool) -> dict[str, dict[str, str]]:
    if use_default or not isinstance(raw, dict):
        return copy.deepcopy(DEFAULTS["fx_bands"])
    out: dict[str, dict[str, str]] = {}
    for cur, e in raw.items():
        if not (isinstance(cur, str) and re.fullmatch(r"[A-Z]{3}", cur)) or not isinstance(e, dict):
            continue
        lo, hi = _dec(e.get("min")), _dec(e.get("max"))
        if lo is not None and hi is not None and 0 < lo < hi:
            out[cur] = {"min": _s(lo), "max": _s(hi)}
    return out


def _norm_fx(raw: Any) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if not isinstance(raw, dict):
        return out
    for cur, e in raw.items():
        if not (isinstance(cur, str) and re.fullmatch(r"[A-Z]{3}", cur)) or not isinstance(e, dict):
            continue
        rate = _dec(e.get("per_usd"))
        if rate is None or rate <= 0:
            continue
        out[cur] = {
            "per_usd": _s(rate),
            "updated_at": str(e.get("updated_at") or "")[:40],
            "updated_by": str(e.get("updated_by") or "")[:40],
        }
    return out


def _norm_targets(raw: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        d = _dec(v)
        if isinstance(k, str) and len(k) <= 73 and d is not None and d < 1:
            out[k] = _s(d)
        if len(out) >= MAX_TARGETS:
            break
    return out


def from_tenant(ts: dict[str, Any] | None) -> dict[str, Any]:
    return normalize((ts or {}).get(SETTINGS_KEY))


def validate_patch(patch: dict[str, Any]) -> None:
    """Strict check of an admin PUT (the stored form silently falls back, the API must say)."""
    for k, (_d, hi) in _MONEY_FIELDS.items():
        if patch.get(k) is not None:
            v = _dec(patch[k])
            if v is None or v > Decimal(hi):
                raise CostSettingsError(f"{k}: a USD amount 0..{hi} with up to 6 decimals")
    for k in _RATE_FIELDS:
        if patch.get(k) is not None:
            v = _dec(patch[k])
            if v is None or v > 1:
                raise CostSettingsError(f"{k}: a fraction between 0 and 1 (0.02 = 2 %)")
    if patch.get("target_margin") is not None and Decimal(str(patch["target_margin"])) >= 1:
        raise CostSettingsError("target_margin must be below 1")
    for k, (_d, lo, hi) in _INT_FIELDS.items():
        v = patch.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi):
            raise CostSettingsError(f"{k}: an integer {lo}..{hi}")
    for name, keys, hi in (("service_fee_direct", DIRECT_PRODUCTS, 1000),):
        sub = patch.get(name)
        if sub is None:
            continue
        if not isinstance(sub, dict) or set(sub) - set(keys):
            raise CostSettingsError(f"{name}: keys {', '.join(keys)}")
        for p, val in sub.items():
            d = _dec(val)
            if d is None or d > hi:
                raise CostSettingsError(f"{name}.{p}: a USD amount 0..{hi}")
    bands = patch.get("fx_bands")
    if bands is not None:
        if not isinstance(bands, dict) or len(bands) > 20:
            raise CostSettingsError("fx_bands: {CUR: {min, max}}")
        for cur, e in bands.items():
            lo = _dec(e.get("min")) if isinstance(e, dict) else None
            hi = _dec(e.get("max")) if isinstance(e, dict) else None
            if (
                not re.fullmatch(r"[A-Z]{3}", str(cur))
                or lo is None
                or hi is None
                or not 0 < lo < hi
            ):
                raise CostSettingsError(f"fx_bands.{cur}: 0 < min < max")
    up = patch.get("uses_per_card")
    if up is not None:
        if not isinstance(up, dict) or set(up) - set(CARD_TIERS):
            raise CostSettingsError("uses_per_card: keys standard, pro20x")
        for t, n in up.items():
            if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 1000:
                raise CostSettingsError(f"uses_per_card.{t}: an integer 1..1000")


def merged(cur: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Settings after a partial patch (nested maps merge; ``fx``, ``targets`` untouched)."""
    new = copy.deepcopy(cur)
    for k, v in patch.items():
        if v is None or k in ("fx", "targets") or k not in DEFAULTS:
            continue
        if k == "fx_bands":
            new[k] = {**new[k], **v}
        elif isinstance(v, dict):
            new[k] = {**new[k], **v}
        elif isinstance(v, (int, str)):
            new[k] = v if not isinstance(v, str) else v.strip()
    return normalize(new)


def set_rate(
    cur: dict[str, Any],
    currency: str,
    per_usd: str,
    actor: str,
    now: datetime | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """``currency`` units per 1 USD; stamps ``updated_at`` (the hook for a future rate service:
    call this with ``actor='rate-service'``). A rate outside the currency's plausible band is
    refused (catches the inverted rate, 0.139 instead of 7.2); a move of more than
    ``fx_max_change`` against the stored rate needs ``confirm``."""
    rate = _dec(per_usd)
    if rate is None or rate <= 0 or rate > 100000 or not re.fullmatch(r"[A-Z]{3}", currency):
        raise CostSettingsError("rate: a positive decimal, currency units per 1 USD")
    if currency == "USD":
        raise CostSettingsError("USD needs no rate")
    band = cur["fx_bands"].get(currency)
    if band and not Decimal(band["min"]) <= rate <= Decimal(band["max"]):
        raise CostSettingsError(
            f"rate: {currency} per USD must be between {band['min']} and {band['max']}",
            "fx_out_of_band",
        )
    old = cur["fx"].get(currency)
    if old and not confirm:
        prev = Decimal(old["per_usd"])
        if abs(rate - prev) / prev > Decimal(cur["fx_max_change"]):
            raise CostSettingsError(
                f"rate: more than {Decimal(cur['fx_max_change']) * 100:.0f} % away from the "
                "saved rate; confirm to change it",
                "fx_change_confirm",
            )
    new = copy.deepcopy(cur)
    new["fx"][currency] = {
        "per_usd": _s(rate),
        "updated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "updated_by": actor,
    }
    return normalize(new)


def fx_stale(settings: dict[str, Any], currency: str, now: datetime | None = None) -> bool:
    """True when the saved rate is older than ``fx_max_age_days`` (or its stamp is unreadable)."""
    if currency == "USD":
        return False
    e = settings["fx"].get(currency)
    if not e:
        return False  # a missing rate is reported as ``fx_rate`` instead
    try:
        at = datetime.fromisoformat(e["updated_at"])
    except ValueError:
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    age = (now or datetime.now(timezone.utc)) - at
    return age.total_seconds() > settings["fx_max_age_days"] * 86400


def rate_for(settings: dict[str, Any], currency: str) -> Decimal | None:
    if currency == "USD":
        return Decimal(1)
    e = settings["fx"].get(currency)
    return Decimal(e["per_usd"]) if e else None


def target_key(product_id: Any, variant_id: Any = None) -> str:
    return f"{product_id}:{variant_id}" if variant_id else str(product_id)


def target_margin(settings: dict[str, Any], product_id: Any, variant_id: Any = None) -> Decimal:
    t = settings["targets"]
    v = t.get(target_key(product_id, variant_id)) or t.get(str(product_id))
    return Decimal(v if v is not None else settings["target_margin"])


# ---------------------------------------------------------------------------------------------
# formulas


@dataclass(frozen=True)
class Source:
    """What the estimate needs from a supplier offer."""

    kind: str  # cdk | direct | vcc | manual (the product's own cost_minor)
    supplier_price_usd: Decimal | None
    direct_product: str | None = None  # gpt | grok | claude
    card_tier: str = "standard"
    vendor_switch: bool = False  # direct only: the vendor may retry a refused payment on a card


@dataclass
class Estimate:
    supplier_price: Decimal | None = None
    service_fee: Decimal = Decimal(0)
    topup_fee: Decimal = Decimal(0)
    card_share: Decimal = Decimal(0)
    failure_cost: Decimal = Decimal(0)
    # vendor_switch only: expected extra failed-attempt fees of the vendor's card switches
    switch_cost: Decimal = Decimal(0)
    channel_fee: Decimal = Decimal(0)
    sale_price_usd: Decimal | None = None
    gross: Decimal | None = None
    net: Decimal | None = None
    gross_margin: Decimal | None = None
    net_margin: Decimal | None = None
    incomplete: list[str] = field(default_factory=list)

    @property
    def fees(self) -> Decimal:
        return (
            self.service_fee
            + self.topup_fee
            + self.card_share
            + self.failure_cost
            + self.switch_cost
        )

    @property
    def total_cost(self) -> Decimal | None:
        if self.supplier_price is None:
            return None
        return self.supplier_price + self.fees + self.channel_fee


def card_kind(kind: str) -> bool:
    """Sources that pay through a vendor card: the top-up fee and the failure fee apply."""
    return kind in ("direct", "vcc")


def fixed_cost(settings: dict[str, Any], src: Source) -> Decimal:
    """Sale-price independent part of the cost (supplier price + the four fee components)."""
    return _components(settings, src)[0]


def switch_cost(settings: dict[str, Any]) -> Decimal:
    """CONSERVATIVE UPPER BOUND of the extra cost per sale of ``vendor_switch`` (review-v12 §21.5):
    ``expected_failure_rate x max_auto_switches x failed_fee``, with NO ``failed_free_count``
    discount (a card's free failures may already be used up). Together with ``failure_cost``
    (``rate x failed_fee``, the first failed attempt) it covers all ``1 + max_auto_switches``
    attempts of an order that keeps failing: the last failed attempt is counted once, in
    ``failure_cost``, and the switches here. An ESTIMATE for the cost rows: it never changes a
    price by itself."""
    rate = Decimal(settings["expected_failure_rate"])
    return rate * int(settings["max_auto_switches"]) * Decimal(settings["failed_fee"])


def _components(
    settings: dict[str, Any], src: Source
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]:
    price = src.supplier_price_usd or Decimal(0)
    service = topup = card = failure = switch = Decimal(0)
    if src.kind == "direct" and src.direct_product:
        service = Decimal(settings["service_fee_direct"].get(src.direct_product, "0"))
    if card_kind(src.kind):
        topup = price * Decimal(settings["topup_fee_rate"]) + Decimal(settings["topup_fee_fixed"])
        failure = Decimal(settings["expected_failure_rate"]) * Decimal(settings["failed_fee"])
    if src.kind == "direct" and src.vendor_switch:
        switch = switch_cost(settings)
    if src.kind == "vcc":  # only a source that opens a card per sale carries a share of its fee
        uses = settings["uses_per_card"].get(src.card_tier) or 1
        card = Decimal(settings["card_open_fee"]) / Decimal(uses)
    return price + service + topup + card + failure + switch, service, topup, card, failure, switch


def estimate(
    settings: dict[str, Any], src: Source, sale_price_usd: Decimal | None, channel_rate: Decimal
) -> Estimate:
    """Unit-cost estimate, every component shown. ``channel_rate`` is a fraction of the sale
    price (our payment gateway's fee). Missing inputs are reported in ``incomplete`` and the
    margin fields stay ``None``: nothing is guessed."""
    est = Estimate(supplier_price=src.supplier_price_usd, sale_price_usd=sale_price_usd)
    if src.supplier_price_usd is None or src.supplier_price_usd <= 0:
        est.incomplete.append("supplier_price")  # a zero or missing price is never "known free"
    if sale_price_usd is None:
        est.incomplete.append("fx_rate")
    if src.kind == "direct" and not src.direct_product:
        est.incomplete.append("direct_product")
    _t, est.service_fee, est.topup_fee, est.card_share, est.failure_cost, est.switch_cost = (
        _components(settings, src)
    )
    if sale_price_usd is not None:
        est.channel_fee = sale_price_usd * channel_rate
    if est.incomplete:
        return est
    assert sale_price_usd is not None and src.supplier_price_usd is not None
    est.gross = sale_price_usd - src.supplier_price_usd
    est.net = est.gross - est.fees - est.channel_fee
    if sale_price_usd > 0:
        est.gross_margin = est.gross / sale_price_usd
        est.net_margin = est.net / sale_price_usd
    return est


def recommend_usd(
    settings: dict[str, Any], src: Source, channel_rate: Decimal, margin: Decimal
) -> Decimal | None:
    """Sale price (USD) whose NET margin is ``margin``: ``P = C / (1 - g - m)`` with ``C`` the
    price-independent cost and ``g`` the channel fee rate; or, with a ``target_markup`` above 0,
    ``P = C x (1 + markup)``. ``None`` when unreachable."""
    if src.supplier_price_usd is None or src.supplier_price_usd <= 0:
        return None
    markup = Decimal(settings.get("target_markup") or 0)
    if markup > 0:
        return _components(settings, src)[0] * (Decimal(1) + markup)
    denom = Decimal(1) - channel_rate - margin
    if denom <= 0:
        return None
    return _components(settings, src)[0] / denom


def recommend_minor(usd: Decimal, per_usd: Decimal, step_minor: int) -> int:
    """Shop-currency minor units, rounded UP to a multiple of ``step_minor`` (never below one
    step: a free product is not a recommendation)."""
    minor = (usd * per_usd * 100).to_integral_value(rounding=ROUND_CEILING)
    steps = (int(minor) + step_minor - 1) // step_minor
    return max(steps, 1) * step_minor


def usd_from_minor(minor: int, per_usd: Decimal | None) -> Decimal | None:
    if per_usd is None or per_usd <= 0:
        return None
    return (Decimal(minor) / 100) / per_usd


def q(d: Decimal | None, places: Decimal = MICRO) -> str | None:
    """Decimal string for the API (6 decimals max, half-up)."""
    if d is None:
        return None
    return format(d.quantize(places, rounding=ROUND_HALF_UP), "f")


def pct(d: Decimal | None) -> str | None:
    """Fraction -> percent string with 2 decimals (0.3 -> "30.00")."""
    return None if d is None else format((d * 100).quantize(CENT, rounding=ROUND_HALF_UP), "f")
