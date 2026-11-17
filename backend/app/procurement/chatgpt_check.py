"""``chatgpt_direct`` half-price check: the buyer's OWN eligibility from ChatGPT's account check.

The fallback method the owner asked for (issue #27, review-v12 §20.4). It exists to be small and
honest:

* ONE read-only ``GET`` of the documented account-check endpoint, with the access token the buyer
  just pasted for THIS order, over plain HTTPS (httpx defaults) and ordinary headers: a bearer
  token, ``Accept``, an honest ``User-Agent``. No browser-fingerprint imitation, no TLS
  impersonation, no sentinel / attestation / risk-control workaround, no proxy, no retry loop.
* The session is used in memory for that one request and never stored, logged or echoed. The
  outcome is logged WITHOUT it (check id, method, outcome, reason).
* When ChatGPT refuses (403, 429, Cloudflare page, any non-JSON answer) the result is
  ``unavailable`` with a machine reason, so staff can switch the method; nothing is bypassed.

Ported in spirit only from the owner's reference parser (``promo_eligibility`` /
``parse_account_check``); none of its risk preparation or fingerprinting is used.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

ACCOUNT_CHECK_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
USER_AGENT = "24goods-eligibility-check/1.0"
TIMEOUT_S = 10.0
MAX_BODY = 512 * 1024


class CheckUnavailable(Exception):
    """ChatGPT could not (or would not) answer; ``reason`` is a machine string."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def session_fields(canonical: str) -> tuple[str, str]:
    """``(access_token, account_id)`` of a validated session document (never logged)."""
    data = json.loads(canonical)
    token = data.get("accessToken") if isinstance(data, Mapping) else None
    account = (data.get("account") or {}) if isinstance(data, Mapping) else {}
    acct = account.get("id") if isinstance(account, Mapping) else None
    if not isinstance(token, str) or not token:
        raise CheckUnavailable("no_access_token")
    return token, acct if isinstance(acct, str) else ""


def parse_account_check(payload: Any, account_id: str = "") -> dict[str, Any]:
    """The account's plan and eligible promotion campaigns from the account-check document."""
    accounts = payload.get("accounts") if isinstance(payload, Mapping) else None
    if not isinstance(accounts, Mapping):
        raise CheckUnavailable("unreadable_answer")
    item = accounts.get(account_id) if account_id else None
    if not isinstance(item, Mapping):
        item = accounts.get("default")
    if not isinstance(item, Mapping):
        item = next(
            (v for k, v in accounts.items() if k != "default" and isinstance(v, Mapping)), None
        )
    if not isinstance(item, Mapping):
        raise CheckUnavailable("unreadable_answer")
    account = item.get("account") if isinstance(item.get("account"), Mapping) else {}
    entitlement = item.get("entitlement") if isinstance(item.get("entitlement"), Mapping) else {}
    campaigns_raw = item.get("eligible_promo_campaigns")
    campaigns_raw = campaigns_raw if isinstance(campaigns_raw, Mapping) else {}
    plan_type = str(account.get("plan_type") or "").strip().lower()
    subscription_plan = str(entitlement.get("subscription_plan") or "").strip().lower()
    is_free = plan_type == "free" or subscription_plan == "chatgptfreeplan"
    campaigns: list[dict[str, Any]] = []
    for key, raw in campaigns_raw.items():
        if not isinstance(raw, Mapping):
            continue
        meta = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
        discount = meta.get("discount") if isinstance(meta.get("discount"), Mapping) else {}
        pct = discount.get("percentage")
        campaigns.append(
            {"key": str(key)[:40], "discount": pct if isinstance(pct, (int, float)) else None}
        )
    return {
        "plan_type": plan_type or None,
        "is_free": is_free,
        "campaigns": campaigns if is_free else [],
    }


def half_eligible(parsed: Mapping[str, Any]) -> tuple[bool, str]:
    """Half price is for a FREE account that ChatGPT itself lists a promotion for: a 50 % campaign,
    or the ``plus`` campaign [I: the half-price campaign's id is not documented; the offer is also
    enforced by the vendor at execution]."""
    if not parsed["is_free"]:
        return False, "not_free_account"
    for c in parsed["campaigns"]:
        if c["key"] == "plus" or c["discount"] == 50:
            return True, ""
    return False, "no_promotion"


async def read_eligibility(
    client: httpx.AsyncClient | None, access_token: str, account_id: str
) -> dict[str, Any]:
    """One GET. Raises :class:`CheckUnavailable` for anything but a readable 200."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    own = client is None
    http = client or httpx.AsyncClient(timeout=TIMEOUT_S, trust_env=False, follow_redirects=False)
    try:
        resp = await http.get(
            ACCOUNT_CHECK_URL, params={"timezone_offset_min": "0"}, headers=headers
        )
    except httpx.HTTPError:
        raise CheckUnavailable("chatgpt_unreachable") from None
    finally:
        headers.clear()
        if own:
            await http.aclose()
    if resp.status_code == 401:
        raise CheckUnavailable("session_expired")
    if resp.status_code != 200:
        raise CheckUnavailable("chatgpt_refused")  # 403 / 429 / challenge page: nothing bypassed
    if len(resp.content) > MAX_BODY:
        raise CheckUnavailable("unreadable_answer")
    try:
        payload = resp.json()
    except ValueError:
        raise CheckUnavailable("unreadable_answer") from None
    return parse_account_check(payload, account_id)
