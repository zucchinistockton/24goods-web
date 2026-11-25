"""``tenants.funding_settings``: automatic top-ups of direct orders' funding cards (ADR-048).

Shared by the console (validated write behind a fresh 2FA code) and the procurement engine
(read). Unknown keys are dropped; a stored value that no longer validates falls back to its
default, so a bad row can never switch spending on: ``auto_recharge`` is the master switch and
stays OFF by default. ADR-049 (owner 2026-11-21): ``auto_open_cards`` (open a new card when no
card has a use left) and ``sweep_leftover`` (move a used-up card's money back to the wallet) are
ON by default but only act while ``auto_recharge`` is on; ``wallet_alert_usd`` is the vendor
wallet level below which staff get a Telegram alert. 2026-11-25: ``upgrade_pay_difference`` and
``cancel_renewal_before_upgrade`` (both OFF by default) steer plan upgrades.
ADR-055 (owner 2026-11-25): ``zzshu_card_issuers`` (ordered supplier codes that may open the
dedicated card of a zzshu order; ZovoCard first, vmcardio only when ZovoCard opened nothing),
``vmcardio_bin_priority`` and ``vmcardio_reserve_slots`` (skip a vmcardio product whose open slots
are at or below this).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

# the owner's recommended card ranges (2026-11-19): success rate >= 90 %
DEFAULT_BINS = ("532604", "537882", "428852", "446619", "474613", "537872")
MAX_BINS = 20
# ADR-055
DEFAULT_ISSUERS = ("zovocard", "vmcardio")
MAX_ISSUERS = 4
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
DEFAULT_VM_BINS = ("537872", "537882", "532604", "555659")
VM_RESERVE = (1, 0, 100)  # default, minimum, maximum
_BIN_RE = re.compile(r"^\d{6,9}$")
# (default, minimum, maximum) in USD
LIMITS: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    "daily_cap_usd": (Decimal(300), Decimal(0), Decimal(10000)),
    "per_topup_max_usd": (Decimal(200), Decimal(0), Decimal(5000)),
    "wallet_alert_usd": (Decimal(60), Decimal(0), Decimal(100000)),
    # ADR-054: a zzshu order's dedicated card is loaded with the plan cost x (1 + this / 100)
    "zzshu_card_margin_pct": (Decimal(20), Decimal(0), Decimal(100)),
}
# switch -> default (ADR-049: the two card switches act only under ``auto_recharge``)
BOOLS: dict[str, bool] = {
    "auto_recharge": False,
    "auto_open_cards": True,
    "sweep_leftover": True,
    # owner 2026-11-25: an upgrade of a running plan is sold at the FULL list price by default
    # (the vendor still only charges us the difference); on = the buyer pays the difference +
    # markup with the credit shown (ADR-052)
    "upgrade_pay_difference": False,
    # owner 2026-11-25: call the vendor's cancel-renewal (session only) before each upgrade order;
    # off by default, results are logged either way and never block the order
    "cancel_renewal_before_upgrade": False,
}


# owner 2026-11-25: staff get a Telegram alert when card.zzshu.pro points drop to this (0 = off)
POINTS_ALERT = (3, 0, 1000)


def defaults() -> dict[str, Any]:
    return {
        **BOOLS,
        "zzshu_points_alert": POINTS_ALERT[0],
        **{key: str(lim[0]) for key, lim in LIMITS.items()},
        "bin_priority": list(DEFAULT_BINS),
        "zzshu_card_issuers": list(DEFAULT_ISSUERS),
        "vmcardio_bin_priority": list(DEFAULT_VM_BINS),
        "vmcardio_reserve_slots": VM_RESERVE[0],
    }


def _money(v: Any, key: str) -> Decimal:
    if isinstance(v, bool) or v is None:
        raise ValueError(key)
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(key) from exc
    _dflt, lo, hi = LIMITS[key]
    if not d.is_finite() or d < lo or d > hi:
        raise ValueError(key)
    return d.quantize(Decimal("0.01"))


def _points_alert(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int | str | Decimal):
        raise ValueError("zzshu_points_alert")
    try:
        d = Decimal(str(v))
    except InvalidOperation as exc:
        raise ValueError("zzshu_points_alert") from exc
    if (
        not d.is_finite()
        or d != d.to_integral_value()
        or not POINTS_ALERT[1] <= d <= POINTS_ALERT[2]
    ):
        raise ValueError("zzshu_points_alert")
    return int(d)


def _issuers(v: Any) -> list[str]:
    """Ordered, distinct supplier codes (at least one). Whether each one is a configured supplier
    is checked by the console against the tenant's supplier rows."""
    if not isinstance(v, list) or not 1 <= len(v) <= MAX_ISSUERS:
        raise ValueError("zzshu_card_issuers")
    out: list[str] = []
    for c in v:
        s = c.strip() if isinstance(c, str) else ""
        if not _CODE_RE.fullmatch(s) or s.endswith("_sandbox"):
            raise ValueError("zzshu_card_issuers")
        if s not in out:
            out.append(s)
    return out


def _reserve_slots(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int | str):
        raise ValueError("vmcardio_reserve_slots")
    try:
        n = int(str(v))
    except ValueError:
        raise ValueError("vmcardio_reserve_slots") from None
    if not VM_RESERVE[1] <= n <= VM_RESERVE[2]:
        raise ValueError("vmcardio_reserve_slots")
    return n


def _bins(v: Any, key: str = "bin_priority") -> list[str]:
    if not isinstance(v, list) or len(v) > MAX_BINS:
        raise ValueError(key)
    out: list[str] = []
    for b in v:
        s = str(b).strip()
        if not _BIN_RE.fullmatch(s):
            raise ValueError(key)
        if s not in out:
            out.append(s)
    return out


def validate(patch: Mapping[str, Any], current: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``current`` (normalised) with ``patch`` applied; raises ``ValueError(<field>)``."""
    out = dict(normalize(current))
    for key, v in patch.items():
        if key in BOOLS:
            if not isinstance(v, bool):
                raise ValueError(key)
            out[key] = v
        elif key in LIMITS:
            out[key] = str(_money(v, key))
        elif key == "zzshu_points_alert":
            out[key] = _points_alert(v)
        elif key == "bin_priority":
            out[key] = _bins(v)
        elif key == "vmcardio_bin_priority":
            out[key] = _bins(v, key)
        elif key == "zzshu_card_issuers":
            out[key] = _issuers(v)
        elif key == "vmcardio_reserve_slots":
            out[key] = _reserve_slots(v)
        else:
            raise ValueError(key)
    return out


def normalize(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """A stored value -> a complete, valid settings dict (each bad field -> its default)."""
    out = defaults()
    if not isinstance(raw, Mapping):
        return out
    for key in BOOLS:
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    for key in LIMITS:
        try:
            out[key] = str(_money(raw.get(key), key))
        except ValueError:
            pass
    try:
        if "zzshu_points_alert" in raw:
            out["zzshu_points_alert"] = _points_alert(raw["zzshu_points_alert"])
    except ValueError:
        pass
    try:
        if "bin_priority" in raw:
            out["bin_priority"] = _bins(raw.get("bin_priority"))
    except ValueError:
        pass
    for key, fn in (
        ("zzshu_card_issuers", _issuers),
        ("vmcardio_reserve_slots", _reserve_slots),
        ("vmcardio_bin_priority", lambda v: _bins(v, "vmcardio_bin_priority")),
    ):
        try:
            if key in raw:
                out[key] = fn(raw[key])
        except ValueError:
            pass
    return out
