"""Pre-orders (owner request 2026-11-19, plan U10): pay now, delivered within a stated window.

A manual-delivery product may carry ``products.preorder`` = ``{"min_days", "max_days",
"ends_at", "note"}``. While it is active (not past ``ends_at``) the buyer sees the window before
paying and must confirm it (``preorder_ack``); the order snapshots the window into
``orders.terms["preorder"]``, so a later edit of the product never changes a sold order's promise.
The window drives the delivery deadline, the staff "overdue" filter and the buyer's escalation
time. The stock limit stays the product's ``manual_stock`` (units that may be sold ahead).
Card-key backorders (deliver on import) are a later step.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.errors import Validation

MAX_DAYS = 60
NOTE_MAX = 200


def _parse_ends(v: Any) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError as exc:
            raise Validation("Invalid pre-order end date", code="preorder_invalid") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def normalize(data: dict[str, Any] | None, delivery_mode: str) -> dict[str, Any] | None:
    """Validated config to store, or ``None`` (not a pre-order). Manual delivery only."""
    if data is None:
        return None
    if delivery_mode != "manual":
        raise Validation("Pre-orders are for manual-delivery products", code="preorder_manual_only")
    try:
        lo = int(data.get("min_days", 1))
        hi = int(data.get("max_days", 3))
    except (TypeError, ValueError) as exc:
        raise Validation("Invalid pre-order window", code="preorder_invalid") from exc
    if not 0 <= lo <= hi <= MAX_DAYS or hi < 1:
        raise Validation("Invalid pre-order window", code="preorder_invalid")
    ends = _parse_ends(data.get("ends_at"))
    note_in = data.get("note") or {}
    if not isinstance(note_in, dict):
        raise Validation("Invalid pre-order note", code="preorder_invalid")
    note = {k: str(note_in.get(k) or "").strip()[:NOTE_MAX] for k in ("en", "zh-CN")}
    return {
        "min_days": lo,
        "max_days": hi,
        "ends_at": ends.isoformat() if ends else None,
        "note": note,
    }


def active(product: Any, now: datetime | None = None) -> dict[str, Any] | None:
    """The product's pre-order config while it applies, else ``None``."""
    cfg = getattr(product, "preorder", None)
    if not cfg or getattr(product, "delivery_mode", None) != "manual":
        return None
    ends = _parse_ends(cfg.get("ends_at"))
    if ends is not None and (now or datetime.now(UTC)) >= ends:
        return None
    return cfg


def public_view(product: Any, lang: str, now: datetime | None = None) -> dict[str, Any] | None:
    """Buyer-facing window (no staff data)."""
    cfg = active(product, now)
    if cfg is None:
        return None
    note = cfg.get("note") or {}
    return {
        "min_days": cfg["min_days"],
        "max_days": cfg["max_days"],
        "ends_at": cfg.get("ends_at"),
        "note": (note.get(lang) or note.get("en") or "") or None,
    }


def snapshot(cfg: dict[str, Any]) -> dict[str, int]:
    """What an order keeps (``orders.terms["preorder"]``)."""
    return {"min_days": int(cfg["min_days"]), "max_days": int(cfg["max_days"])}


def _z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def of_order(order: Any) -> dict[str, int] | None:
    t = getattr(order, "terms", None)
    p = t.get("preorder") if isinstance(t, dict) else None
    return p if isinstance(p, dict) and "max_days" in p else None


def eta_hours(order: Any) -> int | None:
    """Delivery promise of a pre-order in hours (the window's end), else ``None``."""
    p = of_order(order)
    return int(p["max_days"]) * 24 if p else None


def order_view(order: Any) -> dict[str, Any] | None:
    """``{min_days, max_days, expected_from, expected_to}``; the dates once the order is paid."""
    p = of_order(order)
    if p is None:
        return None
    paid = getattr(order, "paid_at", None)
    return {
        "min_days": p["min_days"],
        "max_days": p["max_days"],
        "expected_from": (paid + timedelta(days=p["min_days"])).isoformat().replace("+00:00", "Z")
        if paid
        else None,
        "expected_to": (paid + timedelta(days=p["max_days"])).isoformat().replace("+00:00", "Z")
        if paid
        else None,
    }
