"""``tenants.funding_settings``: automatic top-ups of direct orders' funding cards (ADR-048).

Shared by the console (validated write behind a fresh 2FA code) and the procurement engine
(read). Unknown keys are dropped; a stored value that no longer validates falls back to its
default (automatic top-ups OFF), so a bad row can never switch spending on.
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
}


def defaults() -> dict[str, Any]:
    return {
        "auto_recharge": False,
        "daily_cap_usd": str(LIMITS["daily_cap_usd"][0]),
        "per_topup_max_usd": str(LIMITS["per_topup_max_usd"][0]),
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
        if key == "auto_recharge":
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
    if isinstance(raw.get("auto_recharge"), bool):
        out["auto_recharge"] = raw["auto_recharge"]
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
