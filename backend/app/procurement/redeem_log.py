"""Redemption attempt log, site settings and stats maths (issue #31, review-v12 §23).

Pure helpers shared by the redemption proxy (writes) and the staff console (reads); no I/O here.

* the log never holds a code, a token, a session or an address: :func:`mask_code` keeps at most
  4 + 4 characters and never more than a third of a code; :func:`ip_class_hash` is a keyed hash of
  the /24 (IPv4) or /48 (IPv6) network, so two clients of one network match and nobody can read or
  brute-force an address back from the table;
* the site settings (``tenants.redeem_settings``) are normalised on read AND on write: a corrupt
  document yields the defaults, a limit is clamped to the vendor-safe bounds, text is plain text
  with control and bidi characters removed and a length cap.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
from collections import Counter
from collections.abc import Iterable
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

RETENTION_DAYS = 90
STEPS = ("preview", "preflight", "redeem", "result")
OUTCOMES = ("ok", "completed", "failed", "error", "rate_limited", "invalid", "maintenance")

# per-IP limits per minute the console may set: never above what the vendor allows per egress IP
# (preview 30, redeem 20, result 60; preflight is throttled separately by the vendor)
LIMIT_BOUNDS: dict[str, tuple[int, int]] = {
    "preview": (1, 30),
    "preflight": (1, 20),
    "redeem": (1, 20),
    "result": (1, 60),
}
ANNOUNCE_MAX = 300
MAINTENANCE_MAX = 200
DEFAULT_MAINTENANCE = {
    "en": "Redemption is temporarily unavailable. Please try again later.",
    "zh": "兑换服务暂时不可用，请稍后再试。",
}

_UNSAFE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff"
    r"\u061c\u180e\u2060-\u2064\ufff9-\ufffb\U000e0000-\U000e007f]"
)
_LINK = re.compile(r"://|www\.", re.IGNORECASE)


def has_link(*texts: str | None) -> bool:
    """True when a display text carries something link-like (we never show clickable or copyable
    URLs from an announcement: that is a phishing channel on a page buyers trust)."""
    return any(_LINK.search(t or "") for t in texts)


_SPACES = re.compile(r"\s+")
_CODE_BAD = re.compile(r"[^A-Za-z0-9._:-]")


def mask_code(code: str) -> tuple[str, str]:
    """``(head, tail)`` of a code: ``k = min(4, len // 6)`` characters from each end, so a 25-char
    code shows 4 + 4 and an 8-char one 1 + 1. Never the whole code, never more than a third."""
    code = _CODE_BAD.sub("", code)
    k = min(4, len(code) // 6)
    if k < 1:
        return "", ""
    return code[:k], code[-k:]


def display_mask(head: str | None, tail: str | None) -> str:
    return f"{head}…{tail}" if head and tail else "…"


def ip_class_hash(key: bytes, ip: str | None) -> str | None:
    """Keyed hash of the client's /24 (v4) or /48 (v6) network. ``None`` for anything unparsable."""
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    bits = 24 if addr.version == 4 else 48
    net = ipaddress.ip_network(f"{addr}/{bits}", strict=False)
    return hmac.new(key, f"ipclass:{net}".encode(), hashlib.sha256).hexdigest()[:32]


def clean_text(value: Any, cap: int) -> str:
    """Plain text: control and bidi characters removed, whitespace collapsed, capped."""
    if not isinstance(value, str):
        return ""
    return _SPACES.sub(" ", _UNSAFE.sub(" ", value)).strip()[:cap]


def default_settings() -> dict[str, Any]:
    return {
        "maintenance": False,
        "maintenance_en": "",
        "maintenance_zh": "",
        "announcement_en": "",
        "announcement_zh": "",
        "ip_limits": {},
    }


def normalize_settings(raw: Any) -> dict[str, Any]:
    """Defensive read of ``tenants.redeem_settings`` (also the writer's final pass)."""
    out = default_settings()
    if not isinstance(raw, dict):
        return out
    out["maintenance"] = raw.get("maintenance") is True
    for key, cap in (
        ("maintenance_en", MAINTENANCE_MAX),
        ("maintenance_zh", MAINTENANCE_MAX),
        ("announcement_en", ANNOUNCE_MAX),
        ("announcement_zh", ANNOUNCE_MAX),
    ):
        out[key] = clean_text(raw.get(key), cap)
    limits = raw.get("ip_limits")
    if isinstance(limits, dict):
        for step, (lo, hi) in LIMIT_BOUNDS.items():
            v = limits.get(step)
            if isinstance(v, int) and not isinstance(v, bool):
                out["ip_limits"][step] = max(lo, min(hi, v))
    return out


def maintenance_message(settings: dict[str, Any], lang: str) -> str:
    key = "zh" if lang == "zh" else "en"
    return settings.get(f"maintenance_{key}") or DEFAULT_MAINTENANCE[key]


def public_config(settings: dict[str, Any]) -> dict[str, Any]:
    """What the unauthenticated ``GET /redeem/config`` shows: nothing but display text."""
    return {
        "maintenance": settings["maintenance"],
        "maintenance_message": {
            "en": maintenance_message(settings, "en"),
            "zh": maintenance_message(settings, "zh"),
        },
        "announcement": {"en": settings["announcement_en"], "zh": settings["announcement_zh"]},
    }


# ----------------------------------------------------------------------------------- stats
def success_rate(completed: int, failed: int) -> str | None:
    """``completed / (completed + failed)`` as a 4-decimal string; ``None`` if nothing finished."""
    total = completed + failed
    if total <= 0:
        return None
    rate = (Decimal(completed) / Decimal(total)).quantize(Decimal("0.0001"), ROUND_HALF_UP)
    return str(rate)


def build_stats(
    day_rows: Iterable[tuple[date, int, int, int]],
    reason_rows: Iterable[tuple[str, int]],
    days: int,
    today: date,
    top: int = 8,
) -> dict[str, Any]:
    """``day_rows``: (day, attempts, completed, failed); ``reason_rows``: (reason, count).

    One entry for every one of the last ``days`` days (empty days are zeros), oldest first."""
    by_day = {d: (a, c, f) for d, a, c, f in day_rows}
    series = []
    tot_a = tot_c = tot_f = 0
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        a, c, f = by_day.get(d, (0, 0, 0))
        series.append(
            {"day": d.isoformat(), "attempts": a, "completed": c, "failed": f,
             "success_rate": success_rate(c, f)}
        )  # fmt: skip
        tot_a, tot_c, tot_f = tot_a + a, tot_c + c, tot_f + f
    merged: Counter[str] = Counter()
    for reason, n in reason_rows:
        merged[reason] += n
    ranked = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    return {
        "days": series,
        "totals": {
            "attempts": tot_a,
            "completed": tot_c,
            "failed": tot_f,
            "success_rate": success_rate(tot_c, tot_f),
        },
        "top_reasons": [{"reason": r, "count": n} for r, n in ranked],
    }
