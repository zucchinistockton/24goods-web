"""Checkout regions and half-price plan keys (issue #27, review-v12 §20). Pure helpers.

The authoritative region list is the vendor's ``GET /gpt-direct/plans`` -> ``payment_regions``
(read by the procurement process, never hard-coded as a source of truth). The table below is only
the display fallback for the currency label of a region the buyer already sees on a variant.
"""

from __future__ import annotations

from typing import Any

# owner decision 2026-11-17: the five ChatGPT regions of the vendor; Grok / Claude are USD only
DEFAULT_ALLOWLIST: tuple[str, ...] = ("PH", "EG", "CL", "JP", "US")

REGION_CURRENCY: dict[str, str] = {
    "PH": "PHP",
    "EG": "EGP",
    "CL": "CLP",
    "JP": "JPY",
    "US": "USD",
    "HK": "USD",
    "NG": "USD",
    "MX": "USD",
    "BR": "USD",
    "IL": "USD",
}

HALF_METHODS = ("vendor_preflight", "chatgpt_direct", "manual")
SETTINGS_KEY = "procurement_options"


def is_half_plan(plan: object) -> bool:
    """``plus_half`` and the Grok ``*_half`` keys (half price needs an eligible account)."""
    return isinstance(plan, str) and (plan == "plus_half" or plan.endswith("_half"))


def region_currency(region: str | None) -> str | None:
    return REGION_CURRENCY.get(region) if region else None


def options_of(raw: Any) -> dict[str, str] | None:
    if (
        isinstance(raw, dict)
        and isinstance(raw.get("plan"), str)
        and isinstance(raw.get("region"), str)
    ):
        return {"plan": raw["plan"], "region": raw["region"]}
    return None


def settings_of(ts: dict[str, Any] | None) -> dict[str, Any]:
    """Owner settings (``tenants.settings['procurement_options']``), always complete and typed."""
    raw = (ts or {}).get(SETTINGS_KEY)
    raw = raw if isinstance(raw, dict) else {}
    allow = raw.get("region_allowlist")
    regions = (
        [r for r in allow if isinstance(r, str) and len(r) == 2 and r.isalpha() and r.isupper()]
        if isinstance(allow, list)
        else list(DEFAULT_ALLOWLIST)
    )
    method = raw.get("half_price_check")
    return {
        "region_allowlist": list(dict.fromkeys(regions))[:30],
        "half_price_check": method if method in HALF_METHODS else "vendor_preflight",
    }
