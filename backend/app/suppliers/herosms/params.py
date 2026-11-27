"""SMS product parameters (ADR-057, spec §4): defaults and a tolerant normaliser.

Framework-free: the API validates admin input with it (and refuses what it had to change), and
procurement reads stored params through it (so a hand-edited row can never produce a route
without a price cap or a limit outside its range).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

MAX_ROUTES = 6
CAP_MIN = Decimal("0.01")
CAP_MAX = Decimal("5")
SERVICE_RE_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_")

DEFAULT_ROUTES: tuple[tuple[int, str], ...] = ((182, "0.60"), (52, "0.30"), (187, "0.85"))

# key -> (default, min, max)
INT_LIMITS: dict[str, tuple[int, int, int]] = {
    "max_attempts": (5, 1, 10),
    "tries_per_route": (2, 1, 10),
    "window_minutes": (30, 5, 120),
    "number_wait_s": (480, 120, 1200),
    "min_swap_s": (120, 120, 600),
    "start_hours": (72, 1, 720),
    "daily_cap": (30, 0, 999),
    "per_buyer_daily": (3, 1, 50),
    "cool_no_numbers_min": (15, 1, 1440),
    "cool_no_code_streak": (3, 1, 20),
    "cool_no_code_min": (30, 1, 1440),
}


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    if isinstance(v, bool):
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, n))


def _cap(v: Any) -> Decimal | None:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d < CAP_MIN or d > CAP_MAX:
        return None
    return d.quantize(Decimal("0.0001"))


def normalize(raw: Any) -> dict[str, Any]:
    """Every key present, every value in range. Routes: 1..6 distinct countries with a cap."""
    src = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    service = str(src.get("service") or "dr").strip().lower()
    out["service"] = (
        service if 1 <= len(service) <= 16 and set(service) <= SERVICE_RE_CHARS else "dr"
    )
    routes: list[dict[str, Any]] = []
    seen: set[int] = set()
    for r in src.get("routes") if isinstance(src.get("routes"), list) else []:
        if not isinstance(r, dict):
            continue
        c = r.get("country")
        if isinstance(c, bool) or not isinstance(c, int) or not 0 <= c <= 9999 or c in seen:
            continue
        cap = _cap(r.get("max_price"))
        if cap is None:
            continue
        seen.add(c)
        routes.append({"country": c, "max_price": str(cap)})
        if len(routes) >= MAX_ROUTES:
            break
    if not routes:
        routes = [{"country": c, "max_price": p} for c, p in DEFAULT_ROUTES]
    out["routes"] = routes
    for key, (default, lo, hi) in INT_LIMITS.items():
        out[key] = _int(src.get(key, default), default, lo, hi)
    return out


def defaults() -> dict[str, Any]:
    return normalize({})


def max_cap(params: dict[str, Any], countries: set[int] | None = None) -> Decimal:
    caps = [
        Decimal(r["max_price"])
        for r in params["routes"]
        if countries is None or r["country"] in countries
    ]
    return max(caps) if caps else Decimal(0)
