"""Card pool reads and the card-rules writes of the ZovoCard direct API (issue #29 part 2).

Vendor docs (``docs/research/zovocard/zovocard-openapi.md`` §6.18.5a): ``GET /cards``,
``GET /gpt-direct/cards/{id}/usage``, ``GET /gpt-direct/card-pool/schedule``,
``GET`` / ``PUT /gpt-direct/card-rules``, ``POST /gpt-direct/cards/reset-usage``. Request shapes are
[V] (documented); response shapes marked [I] are inferred and parsed defensively: whatever is not
recognised is dropped, never guessed.

**No card number leaves this module.** ``GET /cards`` carries the full PAN: only the last four
digits are kept (``last4``), the number itself is dropped at once. Everything returned is plain
JSON-safe data for the console.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any

from app.suppliers.zovocard.client import ZovoClient

PRODUCTS = ("gpt", "claude", "grok")
SELECT_MODES = ("default", "lowest_usage", "newest")  # [V] card-rules table
MAX_PAGES = 5  # 500 cards at most per read
PAGE_SIZE = 100
MAX_USAGE_READS = 40  # one usage read per card: bounded (the client rate-limits itself too)
_SKIP_RE = re.compile(r"[^A-Za-z0-9_.:-]")


def _int(v: Any) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def _bool(v: Any) -> bool | None:
    return v if isinstance(v, bool) else None


def _skip_text(v: Any) -> str:
    """A ``skip_reason`` is vendor text: keep only a machine-safe stub."""
    return _SKIP_RE.sub("", str(v or ""))[:48]


def _card_id(v: Any) -> int | None:
    n = _int(v)
    if n is None and isinstance(v, str) and v.isdigit() and len(v) <= 12:
        n = int(v)
    return n if n is not None and 0 < n < 10**12 else None


def last4_of_pan(pan: Any) -> str | None:
    """The last four digits of a card number; ``None`` when it is not a card number."""
    digits = re.sub(r"\D", "", str(pan or ""))
    return digits[-4:] if len(digits) >= 12 else None


def parse_card_rows(
    data: Any, exclude_masks: frozenset[str] = frozenset()
) -> tuple[list[dict[str, Any]], int | None]:
    """``GET /cards`` [V]: ``{total, list: [{id, card_number, product_code, available_amount,
    status, ...}]}`` -> ``(rows, total)``. A row keeps id, last four, product code, balance (a
    decimal string), status, and ``buyer_mask``: True when the masked number (first six + last
    four, computed here while the PAN is still in hand) is in ``exclude_masks``, i.e. a card
    bought for a buyer. The PAN and everything else are dropped."""
    from app.suppliers.zovocard.supplier import _dec_or_none  # local: no import cycle
    from app.suppliers.zovocard.webhook import mask_pan

    if not isinstance(data, Mapping):
        return [], None
    total = _int(data.get("total"))
    rows: list[dict[str, Any]] = []
    for item in data.get("list") if isinstance(data.get("list"), list) else []:
        if not isinstance(item, Mapping):
            continue
        cid = _card_id(item.get("id"))
        status = str(item.get("status") or "").upper()[:16]
        if cid is None or not status or status == "DELETED":
            continue
        bal = _dec_or_none(item.get("available_amount"))
        code = str(item.get("product_code") or "")[:32]
        rows.append(
            {
                "id": cid,
                "last4": last4_of_pan(item.get("card_number")),
                "product_code": _SKIP_RE.sub("", code),
                "status": status,
                "balance": str(bal) if bal is not None else None,
                "buyer_mask": bool(exclude_masks)
                and last4_of_pan(item.get("card_number")) is not None
                and mask_pan(item.get("card_number")) in exclude_masks,
            }
        )
    return rows, total


def _bucket(v: Any) -> dict[str, Any]:
    """One tier of the usage read [V field names ``used`` / ``remaining`` / ``cap``; ``completed``
    / ``failed`` / ``reserved`` named by the docs, spelling inside the bucket [I]]. ``remaining``
    -1 = unlimited."""
    if not isinstance(v, Mapping):
        return {"used": None, "remaining": None, "cap": None, "failed": None, "unlimited": False}
    rem = _int(v.get("remaining"))
    return {
        "used": _int(v.get("used")),
        "remaining": rem if rem is not None and rem >= 0 else None,
        "cap": _int(v.get("cap")),
        "failed": _int(v.get("failed")),
        "unlimited": rem == -1,
    }


def parse_usage(data: Any) -> dict[str, Any]:
    """``GET /gpt-direct/cards/{id}/usage``: ``light`` / ``pro20`` buckets [V]; ``cooling`` and
    ``busy`` [V in the engine's ``usage_state``]; the failure streak (``decline_count`` or
    ``consecutive_failures``) and ``locked`` [I]."""
    if not isinstance(data, Mapping):
        return {"readable": False}
    streak = _int(data.get("decline_count"))
    if streak is None:
        streak = _int(data.get("consecutive_failures"))
    return {
        "readable": True,
        "light": _bucket(data.get("light")),
        "pro20": _bucket(data.get("pro20")),
        "cooling": data.get("cooling") is True,
        "busy": data.get("busy") is True,
        "locked": data.get("locked") is True,
        "failure_streak": streak,
    }


def parse_schedule_view(data: Any) -> dict[str, Any]:
    """The schedule preview for the console [V: ``data.pick`` + ``data.candidates`` with
    ``skip_reason``; the id key ``card_id`` | ``id`` and ``pick`` as object or bare id are [I]]."""
    if not isinstance(data, Mapping):
        return {"pick": None, "candidates": []}
    raw = data.get("pick")
    pick = _card_id(raw.get("card_id", raw.get("id")) if isinstance(raw, Mapping) else raw)
    out: list[dict[str, Any]] = []
    cands = data.get("candidates")
    for item in cands if isinstance(cands, list) else []:
        if not isinstance(item, Mapping):
            continue
        cid = _card_id(item.get("card_id", item.get("id")))
        if cid is None or cid == pick:
            continue
        reason = _skip_text(item.get("skip_reason"))
        out.append({"card_id": cid, "skip_reason": reason or None})
    return {"pick": pick, "candidates": out[:50]}


def parse_rule(data: Any, fallback_product: str | None = None) -> dict[str, Any] | None:
    """One ``card-rules`` row [V field names and defaults]; unknown values fall back to ``None``
    (a field the vendor did not send is never invented). ``select_priority`` is kept as a list of
    plain strings dicts so a write can send it back unchanged (the console never edits it)."""
    if not isinstance(data, Mapping):
        return None
    product = data.get("product") if data.get("product") in PRODUCTS else fallback_product
    if product not in PRODUCTS:
        return None
    mode = data.get("select_mode")
    prio: list[dict[str, str]] = []
    raw = data.get("select_priority")
    for p in raw if isinstance(raw, list) else []:
        if isinstance(p, Mapping):
            prio.append(
                {
                    k: _SKIP_RE.sub("", str(p.get(k) or ""))[:40]
                    for k in ("issuer", "segment_type", "segment_key")
                }
            )
    return {
        "product": product,
        "count_failures": _bool(data.get("count_failures")),
        "light_max_uses": _int(data.get("light_max_uses")),
        "pro20_max_uses": _int(data.get("pro20_max_uses")),
        "select_mode": mode if mode in SELECT_MODES else None,
        "auto_switch_on_fail": _bool(data.get("auto_switch_on_fail")),
        "max_auto_switches": _int(data.get("max_auto_switches")),
        "strict_select": _bool(data.get("strict_select")),
        "select_priority": prio,
        "is_default": data.get("is_default") is True,
    }


def parse_rules(data: Any) -> dict[str, dict[str, Any]]:
    """``GET /gpt-direct/card-rules`` without ``product`` returns three rows [V]; the envelope
    (``{list: [...]}``, a bare list, or one object) is [I]."""
    rows: list[Any]
    if isinstance(data, list):
        rows = data
    elif isinstance(data, Mapping) and isinstance(data.get("list"), list):
        rows = data["list"]
    elif isinstance(data, Mapping):
        rows = [data]
    else:
        rows = []
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        rule = parse_rule(r)
        if rule is not None:
            out[rule["product"]] = rule
    return out


# --------------------------------------------------------------------------------------- calls
async def read_cards(
    client: ZovoClient, exclude_masks: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    """Every card of our vendor account (not deleted), at most :data:`MAX_PAGES` pages. Rows whose
    masked number is in ``exclude_masks`` carry ``buyer_mask=True``."""
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for page in range(1, MAX_PAGES + 1):
        resp = await client.get("/cards", params={"page": str(page), "page_size": str(PAGE_SIZE)})
        got, total = parse_card_rows(resp.data, exclude_masks)
        fresh = [r for r in got if r["id"] not in seen]
        seen.update(r["id"] for r in fresh)
        rows.extend(fresh)
        if not got or (total is not None and len(seen) >= total) or len(got) < PAGE_SIZE:
            break
    return rows


async def read_usage(client: ZovoClient, card_id: int, product: str) -> dict[str, Any]:
    resp = await client.get(f"/gpt-direct/cards/{card_id}/usage", params={"product": product})
    return parse_usage(resp.data)


async def read_schedule(client: ZovoClient, product: str, plan: str) -> dict[str, Any]:
    resp = await client.get(
        "/gpt-direct/card-pool/schedule",
        params={"product": product, "plan": plan, "card_mode": "auto_existing"},
    )
    return parse_schedule_view(resp.data)


async def read_rules(client: ZovoClient) -> dict[str, dict[str, Any]]:
    resp = await client.get("/gpt-direct/card-rules")
    return parse_rules(resp.data)


def rules_body(product: str, rule: Mapping[str, Any]) -> dict[str, Any]:
    """The full ``PUT /gpt-direct/card-rules`` body [V]: every editable field plus the existing
    ``select_priority`` passed through, so a partial edit cannot wipe what the console does not
    show (whether an omitted field resets to its default is unknown [I])."""
    body: dict[str, Any] = {"product": product}
    for k in (
        "count_failures",
        "light_max_uses",
        "pro20_max_uses",
        "select_mode",
        "auto_switch_on_fail",
        "max_auto_switches",
        "strict_select",
    ):
        if rule.get(k) is not None:
            body[k] = rule[k]
    if rule.get("select_priority"):
        body["select_priority"] = list(rule["select_priority"])
    return body


async def write_rule(client: ZovoClient, product: str, rule: Mapping[str, Any]) -> None:
    """A write: the client refuses it for a production supplier without the opt-in flag."""
    await client.request(
        "PUT",
        "/gpt-direct/card-rules",
        json_body=rules_body(product, rule),
        idempotency_key=f"cr:{product}:{uuid.uuid4().hex}",
    )


async def reset_failed(client: ZovoClient, card_ids: list[int]) -> int | None:
    """``POST /gpt-direct/cards/reset-usage`` with ``scope=failed`` [V]; NEVER ``scope=all`` and
    never an empty id list (empty = every card). Returns the vendor's count when it states one
    ([I] key ``reset``)."""
    if not card_ids or any(_card_id(c) is None for c in card_ids):
        raise ValueError("explicit card ids are required")
    resp = await client.request(
        "POST",
        "/gpt-direct/cards/reset-usage",
        json_body={"card_ids": list(card_ids), "scope": "failed"},
        idempotency_key=f"ru:{uuid.uuid4().hex}",
    )
    d = resp.data
    return _int(d.get("reset")) if isinstance(d, Mapping) else None


# ------------------------------------------------------------------------------- validation
RULE_RANGES: dict[str, tuple[int, int]] = {
    "light_max_uses": (0, 1000),  # 0 = unlimited [V]
    "pro20_max_uses": (0, 1000),
    "max_auto_switches": (0, 10),  # 0 = no switch [V]; 10 = the cost model's own ceiling
}
RULE_BOOLS = ("count_failures", "auto_switch_on_fail", "strict_select")
MAX_RESET_CARDS = 50


def check_rule_patch(patch: Mapping[str, Any]) -> dict[str, Any]:
    """Strict range check of a card-rules edit; an unknown key or a bad value is a ValueError."""
    out: dict[str, Any] = {}
    for k, v in patch.items():
        if k in RULE_RANGES:
            lo, hi = RULE_RANGES[k]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ValueError(f"{k} must be an integer {lo}..{hi}")
        elif k in RULE_BOOLS:
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be true or false")
        elif k == "select_mode":
            if v not in SELECT_MODES:
                raise ValueError("select_mode is not one of " + ", ".join(SELECT_MODES))
        else:
            raise ValueError(f"unknown field {k}")
        out[k] = v
    if not out:
        raise ValueError("nothing to change")
    return out
