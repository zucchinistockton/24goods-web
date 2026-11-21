"""``tenants.funding_settings``: automatic top-ups of direct orders' funding cards (ADR-048).

Shared by the console (validated write behind a fresh 2FA code) and the procurement engine
(read). Unknown keys are dropped; a stored value that no longer validates falls back to its
default, so a bad row can never switch spending on: ``auto_recharge`` is the master switch and
stays OFF by default. ADR-049 (owner 2026-11-21): ``auto_open_cards`` (open a new card when no
card has a use left) and ``sweep_leftover`` (move a used-up card's money back to the wallet) are
ON by default but only act while ``auto_recharge`` is on; ``wallet_alert_usd`` is the vendor
wallet level below which staff get a Telegram alert.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

# the owner's recommended card ranges (2026-11-19): success rate >= 90 %
DEFAULT_BINS = ("532604", "537882", "428852", "446619", "474613", "537872")
MAX_BINS = 20
_BIN_RE = re.compile(r"^\d{6,9}$")
# (default, minimum, maximum) in USD
LIMITS: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    "daily_cap_usd": (Decimal(300), Decimal(0), Decimal(10000)),
    "per_topup_max_usd": (Decimal(200), Decimal(0), Decimal(5000)),
    "wallet_alert_usd": (Decimal(60), Decimal(0), Decimal(100000)),
}
# switch -> default (ADR-049: the two card switches act only under ``auto_recharge``)
BOOLS: dict[str, bool] = {"auto_recharge": False, "auto_open_cards": True, "sweep_leftover": True}


def defaults() -> dict[str, Any]:
    return {
        **BOOLS,
        **{key: str(lim[0]) for key, lim in LIMITS.items()},
        "bin_priority": list(DEFAULT_BINS),
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


def _bins(v: Any) -> list[str]:
    if not isinstance(v, list) or len(v) > MAX_BINS:
        raise ValueError("bin_priority")
    out: list[str] = []
    for b in v:
        s = str(b).strip()
        if not _BIN_RE.fullmatch(s):
            raise ValueError("bin_priority")
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
        elif key == "bin_priority":
            out[key] = _bins(v)
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
        if "bin_priority" in raw:
            out["bin_priority"] = _bins(raw.get("bin_priority"))
    except ValueError:
        pass
    return out
