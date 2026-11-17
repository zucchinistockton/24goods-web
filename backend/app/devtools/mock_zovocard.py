"""ZovoCard OpenAPI simulator (dev/test only): ``python -m app.devtools.mock_zovocard`` on :8005.

Reproduces the vendor envelope and quirks the adapter must survive (review-v12 §1, §5), for the
read endpoints used in M1:

* envelope ``{"code": 0, "msg": "ok", "data": ...}`` (``msg``, not ``message``);
* ``401`` without error_code for a missing/unknown key, ``403`` without error_code when the
  key has no IP allowlist (``sk_mock_noallow``);
* ``429`` without error_code past the per-key limit (default 100/min);
* forced quirks per path: ``503 channel_unavailable``, ``409`` (idempotent request still
  running), ``200`` with body ``code: 404``, ``500 internal_error``, ``401``, ``403``;
* writes: ``Idempotency-Key`` format check (``400`` without error_code), otherwise ``404``
  (M1 implements no write endpoint).

Fixtures are synthetic and only *shaped* like real responses: camelCase ``plans{}``, snake_case
``registry[]``, card amounts as JSON floats, a registry key that differs from its
``acc_plan_key`` (Claude/Grok), a non-purchasable plan and a registry row without
``purchasable``.

Cards (review-v12 §17): ``POST /cards/open`` and ``/cards/recharge``, ``GET /cards/{id}`` and
``/cards/{id}/recharges``, ``DELETE /cards/{id}``, with the vendor envelope.
``recharge_pending_next`` reproduces the documented "pending" 400 (no error_code, not cached),
``delete_pending_next`` the 202 delete; balance-log rows carry the FULL PAN like the sandbox (the
adapter masks it).

Scenario plans (``MockZovo.scenarios`` / ``APP_MOCKZOVO_SCENARIOS=1``, used by the E2E smoke so
no control channel is needed): GPT plans ``scn_lost`` (answer lost after execution: 504, the
same-key replay returns the code), ``scn_409`` (first attempt 409, then executes), ``scn_202``
(executed, first answer ``202`` without an envelope), ``scn_503`` (always ``503
channel_unavailable``, nothing charged).

Public redemption (``/api/v1/cdk/{preview,preflight,redeem,result}``, no API key, review-v12
§8/§12): tokens are bound to the ``X-Redemption-Device`` header of the preview (another device
answers ``400 REDEMPTION_SESSION_INVALID``); ``preflight`` returns a token plus payment
country/currency (Grok/Claude plans: no token); ``redeem`` answers ``succeeded`` with an
``SBXORD-`` order id and, like the real sandbox, leaves the CDK ``unused``; ``result`` is 404
``not_found`` for an unknown token (and for every token when ``MockZovo.result_404`` or
``APP_MOCKZOVO_RESULT_404=1``). An unknown, disabled or non-``unused`` code is
``400 CDK_UNAVAILABLE``. A credential containing ``invalid`` is ``400 GPT_SESSION_INVALID``.
The credential and the code are never stored in ``requests``.

Direct orders (review-v12 §18): ``POST /gpt-direct/preflight`` (no Idempotency-Key; a single-use
token valid ``DIRECT_TOKEN_TTL_S``; the full e-mail is reported, the adapter masks it),
``POST /gpt-direct/orders`` (``202 accepted``, status ``queued``; the vendor's pitfall 1 is
reproduced: an UNKNOWN ``product`` silently becomes ``gpt``; a repeated ``client_request_id``
returns the original order; a missing / used / expired token is ``400 GPT_PRICE_UNCONFIRMED``),
``GET /gpt-direct/orders`` (list) and ``/{id}`` (``data.order`` + ``data.events``; the order moves
queued -> running -> completed over successive reads, the 1st still saying queued, or ends
``declined`` / stays ``running`` forever through ``direct_end``),
``POST /gpt-direct/orders/{id}/cancel`` (200 only while ``queued``, else ``409`` without
error_code). The session is NEVER stored: only a digest.

Card pool (#29 part 2): ``GET /cards`` (list with the full PAN, like the real one),
``usage_failed`` = failed count per funding card.
Card modes (issue #29): ``GET /gpt-direct/card-pool/schedule`` (``data.pick`` + ``data.candidates``
with ``skip_reason``; knobs ``card_pool``, ``schedule_skip``, ``schedule_none`` = "no card
available"), ``GET`` / ``PUT /gpt-direct/card-rules``, ``POST /gpt-direct/cards/reset-usage``;
``direct_no_card_next`` makes the next order POSTs answer the "no card" refusal,
``direct_switch_to`` simulates the vendor switching card (only when ``no_auto_card_switch`` is
false). Orders show ``card_id`` and ``card_last_four``.

Force a quirk from tests with ``MockZovo.force(path, name)`` or, on a running dev server,
``POST /__mock/force {"path": "/balance", "quirk": "429"}``. Refuses to start unless
``APP_ENV`` is dev or test.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

PREFIX = "/openapi/v1"
DIRECT_TOKEN_TTL_S = 600.0  # vendor 6.18.3: the preflight token is valid 10 minutes, single use
MOCK_EMAIL = "buyer.person@example.test"  # the vendor reports it in full; the adapter masks it
OK_KEY = "sk_mock_ok"
NOALLOW_KEY = "sk_mock_noallow"
_IDEM_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")

QUIRKS: dict[str, tuple[int, dict[str, Any]]] = {
    "401": (401, {"code": 401, "msg": "invalid api key"}),
    "403": (403, {"code": 403, "msg": "ip allowlist not configured"}),
    "429": (429, {"code": 429, "msg": "too many requests"}),
    "409": (409, {"code": 409, "msg": "same request still processing"}),
    "500": (500, {"code": 500, "error_code": "internal_error", "msg": "server error"}),
    "503": (
        503,
        {"code": 503, "error_code": "channel_unavailable", "msg": "channel temporarily down"},
    ),
    "200_code_404": (200, {"code": 404, "msg": "card not found"}),
    # the request is EXECUTED (and its 2xx cached) but the answer is lost: timeout after send
    "lost_response": (504, {}),
    "delete_disabled": (400, {"code": 400, "error_code": "DELETE_DISABLED", "msg": "no delete"}),
    "403_access_denied": (
        403,
        {"code": 403, "error_code": "GPT_DIRECT_ACCESS_DENIED", "msg": "direct not enabled"},
    ),
}


def _plan(key: str, label: str, currency: str, expected: int, *, enabled: Any = True) -> dict:
    return {
        "key": key,
        "label": label,
        "upstreamPlan": key,
        "flow": "checkout",
        "currency": currency,
        "enabled": enabled,
        "serviceFeeUsdMinor": 15,
        "expectedAmountMinor": expected,
        "minAmountMinor": int(expected * 0.9),
        "maxAmountMinor": int(expected * 1.1),
        "balanceBufferBps": 500,
        "minBalanceBufferUsdMinor": 100,
    }


def _reg(key: str, product: str, acc: str, currency: str, amount: int, **extra: Any) -> dict:
    row = {
        "key": key,
        "product": product,
        "funding": "bin_snapshot",
        "acc_plan_key": acc,
        "label": key.replace("_", " ").title(),
        "flow": "checkout",
        "tier": 1,
        "sort_order": 10,
        "is_credit": key.startswith("credit"),
        "requires_active_subscription": key.startswith("credit"),
        "checkout_currency": currency,
        "checkout_amount_minor": amount,
        "max_amount_minor": int(amount * 1.1),
        "offshore_countries": None,
        "service_fee_usd_minor": 15,
        "purchasable": True,
    }
    row.update(extra)
    return row


SCENARIO_PLANS = ("scn_lost", "scn_409", "scn_202", "scn_503")


MOCK_GPT_REGIONS = (("PH", "PHP"), ("EG", "EGP"), ("CL", "CLP"), ("JP", "JPY"), ("US", "USD"))


def plans_fixture(product: str, scenarios: bool = False) -> dict[str, Any]:
    """Synthetic plans payload. GPT: ``plus`` and ``go`` purchasable; ``pro_5x_renew`` has
    ``purchasable: false``; ``pro_20x`` has the registry flag but its plan is disabled;
    ``credit250`` lacks ``purchasable`` entirely. Claude/Grok keys differ from acc_plan_key."""
    if product == "claude":
        plans = {
            "claude_pro": _plan("claude_pro", "Claude Pro", "USD", 2000),
            "claude_max_5x": _plan("claude_max_5x", "Claude Max 5x", "USD", 10000),
        }
        registry = [
            _reg("pro", "claude", "claude_pro", "USD", 2000),
            _reg("max_5x", "claude", "claude_max_5x", "USD", 10000),
        ]
        regions = [{"country": "US", "currency": "USD"}]
    elif product == "grok":
        plans = {
            "grok_premium_monthly": _plan("grok_premium_monthly", "Premium", "USD", 3000),
            "grok_super_monthly": _plan("grok_super_monthly", "Super", "USD", 5000, enabled=False),
        }
        registry = [
            _reg("premium_monthly", "grok", "grok_premium_monthly", "USD", 3000),
            _reg("super_monthly", "grok", "grok_super_monthly", "USD", 5000),
        ]
        regions = [{"country": "US", "currency": "USD"}, {"country": "HK", "currency": "USD"}]
    else:
        plans = {
            "go": _plan("go", "Go", "PHP", 30000),
            "plus": _plan("plus", "Plus", "PHP", 110000),
            "pro_20x": _plan("pro_20x", "Pro 20x", "PHP", 1100000, enabled=False),
            "pro_5x_renew": _plan("pro_5x_renew", "Pro 5x renew", "PHP", 0),
            "credit250": _plan("credit250", "Credits 250", "PHP", 56500),
        }
        credit = _reg("credit250", "gpt", "credit250", "PHP", 56500)
        del credit["purchasable"]
        registry = [
            _reg("go", "gpt", "go", "PHP", 30000),
            _reg("plus", "gpt", "plus", "PHP", 110000),
            _reg("pro_20x", "gpt", "pro_20x", "PHP", 1100000),
            _reg("pro_5x_renew", "gpt", "pro_5x_renew", "PHP", 0, purchasable=False),
            credit,
        ]
        regions = [{"country": c, "currency": cur} for c, cur in MOCK_GPT_REGIONS]
        if scenarios:
            for key in SCENARIO_PLANS:
                plans[key] = _plan(key, key, "PHP", 30000)
                registry.append(_reg(key, "gpt", key, "PHP", 30000))
            # half-price promotion plan (issue #27): quoted only to an eligible account
            plans["plus_half"] = _plan("plus_half", "Plus Half", "PHP", 55000)
            registry.append(_reg("plus_half", "gpt", "plus_half", "PHP", 55000))
    return {
        "version": 7,
        "updatedAt": 1700000000000,
        "updatedBy": "mock",
        "reason": "",
        "plans": plans,
        "registry": registry,
        "sandbox_manual_card": False,
        "external_card_fee_minor": 0,
        "cs_live_fee_minor": 0,
        "payment_regions": regions,
        "cdk_creation_promo": {"active": False, "until": 0},
    }


def products_fixture() -> list[dict[str, Any]]:
    def p(code: str, issuer: str, fee: float, enabled: bool = True) -> dict[str, Any]:
        return {
            "id": len(code),
            "product_code": code,
            "issuer": issuer,
            "bin": "000000",
            "network": "VISA",
            "issuing_area": "Mockland",
            "card_type": "save",
            "open_fee": fee,
            "base_open_fee": fee,
            "recharge_fee": 0,
            "rtf_rate": 0.1,
            "min_amount": 10,
            "max_amount": 10000,
            "min_recharge_amount": None,
            "enabled": enabled,
            "restricted_merchants": [],
            "google_chatgpt_blocked": False,
        }

    return [p("MOCK01", "one", 1.5), p("MOCK02", "four", 0.3), p("MOCK03", "two", 2.0, False)]


BALANCE_FIXTURE: dict[str, Any] = {
    "balance": 12.34,
    "spendable_balance": 12.34,
    "account_reserve_amount": 0,
    "account_reserve_enabled": True,
    "minimum_deposit_amount": 10,
    "currency": "USD",
}


@dataclass
class MockZovo:
    keys: dict[str, bool] = field(default_factory=lambda: {OK_KEY: True, NOALLOW_KEY: False})
    rate_per_min: int = 100
    forced: dict[str, deque[str]] = field(default_factory=lambda: defaultdict(deque))
    hits: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))
    requests: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    # writes (M2): idempotency cache of 2xx answers only (vendor §4), issued CDKs by id
    idem: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    cdks: dict[int, dict[str, Any]] = field(default_factory=dict)
    next_cdk: int = 1000
    balance_logs: list[dict[str, Any]] = field(default_factory=list)  # newest first
    scenarios: bool = False  # E2E scenario plans (scn_*), see the module docstring
    scn_seen: set[str] = field(default_factory=set)
    executions: int = 0  # how many issue requests really ran (tests: never twice per key)
    # public redemption: token -> session; (step, device, has_credential) per call, no secrets
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    redeem_calls: list[tuple[str, str, bool]] = field(default_factory=list)
    redeem_orders: dict[str, dict[str, Any]] = field(default_factory=dict)  # client_request_id
    result_404: bool = False  # the real sandbox never returns a result
    # cards (M3): vendor id -> card; recharge rows by card; knobs for the documented quirks
    cards: dict[int, dict[str, Any]] = field(default_factory=dict)
    recharges: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    next_card: int = 500
    next_log: int = 9000
    card_opens: int = 0  # how many opens really ran (tests: never twice per key)
    card_recharges: int = 0  # how many recharges really ran
    recharge_pending_next: int = 0  # the next N recharges answer the "pending" 400 (documented)
    delete_pending_next: int = 0  # the next N deletes answer 202 and settle after two reads
    card_cvv: str | None = None  # tests: a CVV / expiry that cannot collide with ordinary numbers
    card_expire: str | None = None
    short_open: float = 0.0  # the card opens holding this much LESS than init_amount
    fee_from_card: bool = False  # open / recharge fees are taken from the card balance
    recharge_fee: float = 0.0  # reported on every recharge row (0 in the real sandbox)
    malformed_detail: int = 0  # the next N card reads lack ``status`` / ``available_amount``
    # direct orders (M3 part 2): vendor id -> order; session digests only, never the session
    direct_orders: dict[int, dict[str, Any]] = field(default_factory=dict)
    direct_tokens: dict[str, float] = field(default_factory=dict)  # preflight token -> expiry
    next_direct: int = 800
    direct_executions: int = 0  # how many orders really ran (tests: never twice per job)
    preflights: int = 0
    direct_end: str = "completed"  # completed | declined | running (never ends)
    direct_polls: int = 3  # the Nth read ends the order (1st: queued, then running)
    direct_expire_next: int = 0  # the next N preflight tokens are issued already expired
    direct_cancel_409: bool = False  # every cancel answers 409 (payment already started)
    direct_quote_factor: float = 1.0  # the preflight quotes are inflated by this factor
    direct_quotes_null: bool = False  # [V] the real sandbox answers ``quotes: null``
    preflight_over: dict[str, Any] = field(default_factory=dict)  # preflight fields overridden
    usage_busy: set[int] = field(default_factory=set)  # funding cards that are busy / cooling
    direct_fee_status: str = "held"  # ``service_fee_status`` of an order that did not complete
    direct_omit: set[str] = field(default_factory=set)  # order-view fields left out (a quirk)
    direct_order_over: dict[str, Any] = field(default_factory=dict)  # order-view overrides
    usage_remaining: dict[int, int] = field(default_factory=dict)  # funding card -> remaining uses
    list_cap: int = 100  # rows per page the order list really returns (paging tests)
    direct_current_plan: str = "free"  # what the preflight reports as the account's plan
    direct_quote_error: str = ""  # a non-empty quote_error on the preflight
    direct_half_eligible: bool = True  # False: the preflight quotes no ``*_half`` plan
    direct_cancelled: int = 0
    # card modes (issue #29): ``card-pool/schedule`` preview, card rules, usage reset
    card_pool: list[int] = field(default_factory=list)  # funding cards in the vendor's pick order
    schedule_none: bool = False  # "no card available": the schedule has no pick, no candidates
    schedule_skip: dict[int, str] = field(default_factory=dict)  # card -> ``skip_reason``
    schedule_calls: int = 0
    direct_no_card_next: int = 0  # the next N order POSTs answer the "no card" refusal
    direct_no_card_code: str = "NO_CARD_AVAILABLE"
    direct_switch_to: int | None = None  # vendor_switch: the order finally pays with this card
    direct_switch_seen: list[bool] = field(default_factory=list)  # ``no_auto_card_switch`` as sent
    direct_cards_sent: list[int] = field(default_factory=list)  # ``card_id`` of every order POST
    card_rules: dict[str, dict[str, Any]] = field(default_factory=dict)
    usage_resets: list[dict[str, Any]] = field(default_factory=list)
    usage_failed: dict[int, int] = field(default_factory=dict)  # funding card -> failed count
    direct_product_seen: list[str] = field(default_factory=list)  # product exactly as sent

    def __post_init__(self) -> None:
        if (
            self.scenarios
        ):  # the E2E smoke pays direct orders with this card of "our" account (id 1)
            self.add_funding_card(1, available=5000.0)

    def add_funding_card(self, cid: int, available: float = 500.0, status: str = "ACTIVE") -> None:
        """A card of OUR vendor account for ``upstream_direct`` offers (``card_id``)."""
        self.cards[cid] = {
            "id": cid, "card_number": f"5378{cid:012d}", "cvv": "123", "expire": "12/30",
            "status": status, "available_amount": available, "open_fee": 0.0,
            "product_code": "FUND",
        }  # fmt: skip
        self.recharges[cid] = []

    def force(self, path: str, quirk: str, times: int = 1) -> None:
        if quirk not in QUIRKS:
            raise ValueError("unknown quirk")
        for _ in range(times):
            self.forced[path].append(quirk)


def _env(code: int, data: Any = None, msg: str = "ok") -> dict[str, Any]:
    return {"code": code, "msg": msg, "data": data}


def create_mock_zovocard(state: MockZovo | None = None) -> Starlette:
    mock = state or MockZovo()

    def _gate(request: Request, path: str) -> JSONResponse | None:
        key = request.headers.get("x-api-key", "")
        # the API key itself is never recorded, only whether one was sent
        mock.requests.append(
            (
                request.method,
                path,
                {
                    "api_key": "1" if key else "0",
                    "app_id": request.headers.get("x-app-id", ""),
                    "idempotency_key": request.headers.get("idempotency-key", ""),
                },
            )
        )
        if not key:  # the vendor also accepts a bearer token
            auth = request.headers.get("authorization", "")
            key = auth[7:] if auth.lower().startswith("bearer ") else ""
        if key not in mock.keys:
            return JSONResponse(QUIRKS["401"][1], status_code=401)
        if not mock.keys[key]:
            return JSONResponse(QUIRKS["403"][1], status_code=403)
        now = time.monotonic()
        window = mock.hits[key]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= mock.rate_per_min:
            return JSONResponse(QUIRKS["429"][1], status_code=429, headers={"Retry-After": "30"})
        window.append(now)
        queue = mock.forced.get(path)
        if queue and queue[0] != "lost_response":
            status, body = QUIRKS[queue.popleft()]
            return JSONResponse(body, status_code=status)
        return None

    def _issue(request_body: dict[str, Any]) -> JSONResponse:
        plan = request_body.get("plan")
        count = request_body.get("count", 1)
        if request_body.get("funding_confirmed") is not True:
            return JSONResponse({"code": 400, "msg": "funding must be confirmed"}, status_code=400)
        if not isinstance(count, int) or not 1 <= count <= 50:
            return JSONResponse({"code": 400, "msg": "at most 50 per request"}, status_code=400)
        gpt = plans_fixture("gpt", mock.scenarios)
        reg = {r["key"]: r for r in gpt["registry"]}
        row = reg.get(plan) if isinstance(plan, str) else None
        if row is None or row.get("purchasable") is not True:
            return JSONResponse({"code": 400, "msg": "unknown plan"}, status_code=400)
        mock.executions += 1
        issued = []
        for _ in range(count):
            mock.next_cdk += 1
            cid = mock.next_cdk
            code = f"MOCKPLUS-{cid:010X}-{(cid * 7919) & 0xFFFFFFFFFF:010X}"
            mock.cdks[cid] = {"id": cid, "code": code, "plan": plan, "status": "unused"}
            issued.append(
                {
                    "id": cid,
                    "code": code,
                    "code_prefix": code[:12],
                    "plan": plan,
                    "fee_amount_minor": 15,
                }
            )
        return JSONResponse(_env(0, {"requested": count, "issued": issued}))

    # ---- cards (M3). Amounts are JSON floats like the vendor's; PAN/CVV/expiry are synthetic.
    def _log(card: dict[str, Any], kind: str, amount: float) -> None:
        mock.next_log += 1
        mock.balance_logs.insert(
            0,
            {
                "id": mock.next_log,
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "amount": amount,
                "before": 0,
                "after": 0,
                "type": kind,
                "remark": f"card {card['card_number']}",  # the vendor puts the full PAN here
                "ref_id": "",
                "wallet": "main",
                "card_id": card["id"],
                "card_number": card["card_number"],
            },
        )

    def _card_view(card: dict[str, Any]) -> dict[str, Any]:
        return {
            "card_id": f"card{card['id']:020d}",
            "card_number": card["card_number"],
            "cvv": card["cvv"],
            "expire": card["expire"],
            "status": card["status"],
            "available_amount": card["available_amount"],
            "card_type": "save",
            "open_fee": card.get("open_fee", 0),
        }

    def _open_card(body: dict[str, Any]) -> JSONResponse:
        prod = next(
            (p for p in products_fixture() if p["product_code"] == body.get("product_code")), None
        )
        amount = body.get("init_amount")
        if prod is None or not prod["enabled"]:
            return JSONResponse({"code": 400, "msg": "product unavailable"}, status_code=400)
        if (
            isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or amount < prod["min_amount"]
            or amount > prod["max_amount"]
            or not body.get("first_name")
            or not body.get("last_name")
        ):
            return JSONResponse({"code": 400, "msg": "bad parameters"}, status_code=400)
        mock.card_opens += 1
        mock.next_card += 1
        cid = mock.next_card
        pan = f"4111{cid:012d}"
        year = (datetime.now(timezone.utc).year + 4) % 100
        card = {
            "id": cid, "card_number": pan, "cvv": mock.card_cvv or f"{(cid * 37) % 900 + 100}",
            "expire": mock.card_expire or f"{(cid % 12) + 1:02d}/{year:02d}", "status": "ACTIVE",
            "available_amount": float(amount) - mock.short_open
            - (float(prod["open_fee"]) if mock.fee_from_card else 0.0),
            "open_fee": float(prod["open_fee"]), "product_code": prod["product_code"],
        }  # fmt: skip
        mock.cards[cid] = card
        mock.recharges[cid] = []
        _log(card, "card_recharge", -float(amount))  # the opening charge, as the sandbox books it
        _log(card, "open_card", -float(prod["open_fee"]))
        return JSONResponse(
            _env(
                0,
                {
                    "id": cid, "issuer": prod["issuer"], "card_number": pan, "cvv": card["cvv"],
                    "expire": card["expire"], "product_code": prod["product_code"],
                    "available_amount": card["available_amount"], "status": "ACTIVE",
                    "open_fee": prod["open_fee"],
                },
            )
        )  # fmt: skip

    def _recharge_card(body: dict[str, Any]) -> JSONResponse:
        card = mock.cards.get(body.get("card_id")) if isinstance(body.get("card_id"), int) else None
        amount = body.get("amount")
        if card is None or card["status"] != "ACTIVE":
            return JSONResponse({"code": 404, "error_code": "not_found", "msg": "no card"}, 404)
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount < 10:
            return JSONResponse(
                {"code": 400, "error_code": "amount_below_min", "msg": "below minimum"}, 400
            )
        row = {
            "id": len(mock.recharges[card["id"]]) + 1,
            "amount": float(amount),
            "fee": mock.recharge_fee,
        }
        if mock.recharge_pending_next > 0:
            # the documented quirk: "result pending" is a 400 WITHOUT error_code, not cached; the
            # recharge IS in flight and shows up as pending, then settles by itself
            mock.recharge_pending_next -= 1
            mock.card_recharges += 1
            mock.recharges[card["id"]].append({**row, "status": "pending", "polls": 0})
            return JSONResponse({"code": 400, "msg": "recharge result pending"}, status_code=400)
        mock.card_recharges += 1
        mock.recharges[card["id"]].append({**row, "status": "success"})
        card["available_amount"] += float(amount) - (
            mock.recharge_fee if mock.fee_from_card else 0.0
        )
        _log(card, "card_recharge", -float(amount))
        return JSONResponse(_env(0, {"pending": False}, msg="ok"))

    def _delete_card(cid: int) -> JSONResponse:
        card = mock.cards.get(cid)
        if card is None:
            return JSONResponse({"code": 404, "error_code": "not_found", "msg": "no card"}, 404)
        if card["status"] == "DELETED":
            return JSONResponse(_env(0, None))
        if mock.delete_pending_next > 0:
            mock.delete_pending_next -= 1
            card["status"], card["polls"] = "DELETING", 0
            return JSONResponse({"msg": "accepted"}, status_code=202)
        card["status"] = "DELETED"
        _log(card, "card_refund", card["available_amount"])
        card["available_amount"] = 0.0  # the sandbox answers 200 with 0, not 404
        return JSONResponse(_env(0, {"pending": False}))

    # ---- direct orders (M3 part 2)
    def _credential_ok(cred: Any) -> bool:
        return (
            isinstance(cred, dict)
            and cred.get("mode") == "session"
            and isinstance(cred.get("session"), str)
            and 8 <= len(cred["session"])
            and "invalid" not in cred["session"].lower()
        )

    def _preflight(body: dict[str, Any]) -> JSONResponse:
        cred = body.get("credential")
        if not _credential_ok(cred):
            return JSONResponse(
                {"code": 400, "error_code": "GPT_SESSION_INVALID", "msg": "bad session"}, 400
            )
        mock.preflights += 1
        token = "pf-" + secrets.token_hex(12)
        expires = time.monotonic() + DIRECT_TOKEN_TTL_S
        if mock.direct_expire_next > 0:
            mock.direct_expire_next -= 1
            expires = time.monotonic() - 1.0  # a stale token: the order refuses it
        mock.direct_tokens[token] = expires
        country = body.get("payment_country") or "PH"
        currency = body.get("payment_currency") or ("USD" if country == "US" else "PHP")
        quotes: dict[str, Any] = {}
        for prod in ("gpt", "claude", "grok"):
            for row in plans_fixture(prod, mock.scenarios)["registry"]:
                if row["key"].endswith("_half") and not mock.direct_half_eligible:
                    continue
                if row["checkout_amount_minor"]:
                    quotes[row["key"]] = {
                        "country": country, "currency": row["checkout_currency"],
                        "plan": row["key"],
                        "amountMinor": int(row["checkout_amount_minor"] * mock.direct_quote_factor),
                        "minorUnitExponent": 2,
                    }  # fmt: skip
        return JSONResponse(
            _env(
                0,
                {
                    "email": MOCK_EMAIL,
                    "currentPlan": mock.direct_current_plan,
                    "credentialMode": "session",
                    "preflight_token": token,
                    "preflight_expires_at": (
                        datetime.now(timezone.utc) + timedelta(seconds=DIRECT_TOKEN_TTL_S)
                    ).isoformat(),
                    "payment_country": country,
                    "payment_currency": currency,
                    "quotes": None if mock.direct_quotes_null else quotes,
                    "blocking_reason": "",
                    "can_purchase_at": None,
                    "subscription_has_active": False,
                    "subscription_recovery_required": False,
                    **mock.preflight_over,
                    "quote_error": mock.direct_quote_error,
                },
            )
        )

    def _order_view(o: dict[str, Any]) -> dict[str, Any]:
        view = _order_view_raw(o)
        view.update(mock.direct_order_over)
        for k in mock.direct_omit:
            view.pop(k, None)
        return view

    def _order_view_raw(o: dict[str, Any]) -> dict[str, Any]:
        return {
            "order_id": o["id"],
            "client_request_id": o["client_request_id"],
            "plan": o["plan"],
            "source": "api",
            "status": o["status"],
            "stage": o["status"],
            "currency": o["currency"],
            "quoted_amount_minor": o["amount"],
            "final_amount_minor": o["amount"] if o["status"] == "completed" else None,
            "account_email": "b***@example.test",
            "card_id": o["card_id"],
            # the vendor shows the last four only, never a number (§6.18.8a)
            "card_last_four": str(mock.cards.get(o["card_id"], {}).get("card_number", ""))[-4:],
            "service_fee_status": "settled"
            if o["status"] == "completed"
            else mock.direct_fee_status,
        }

    def _create_order(body: dict[str, Any]) -> JSONResponse:
        mock.direct_product_seen.append(str(body.get("product", "")))
        rid = body.get("client_request_id")
        if not isinstance(rid, str) or not 1 <= len(rid) <= 80:
            return JSONResponse(
                {"code": 400, "error_code": "INVALID_REQUEST", "msg": "client_request_id"}, 400
            )
        for o in mock.direct_orders.values():  # a repeated client_request_id: the original order
            if o["client_request_id"] == rid:
                return JSONResponse(
                    _env(0, {"id": o["id"], "plan": o["plan"], "status": o["status"],
                             "client_request_id": rid}, msg="accepted"), status_code=202,
                )  # fmt: skip
        product = body.get("product")
        if product not in ("gpt", "claude", "grok"):
            product = "gpt"  # vendor pitfall 1: an unknown product is silently a GPT order
        plan = body.get("plan")
        reg = {r["key"]: r for r in plans_fixture(str(product), mock.scenarios)["registry"]}
        row = reg.get(plan) if isinstance(plan, str) else None
        if row is None or row.get("purchasable") is not True:
            return JSONResponse(
                {"code": 400, "error_code": "INVALID_REQUEST", "msg": "unknown plan"}, 400
            )
        if not isinstance(body.get("card_id"), int) or isinstance(body.get("card_id"), bool):
            return JSONResponse(
                {"code": 400, "error_code": "INVALID_REQUEST", "msg": "card_id"}, 400
            )
        cred = body.get("credential")
        if not _credential_ok(cred):
            return JSONResponse(
                {"code": 400, "error_code": "GPT_SESSION_INVALID", "msg": "bad session"}, 400
            )
        token = body.get("preflight_token")
        live = mock.direct_tokens.pop(token, None) if isinstance(token, str) else None
        if live is None or live < time.monotonic():
            return JSONResponse(
                {"code": 400, "error_code": "GPT_PRICE_UNCONFIRMED", "msg": "re-preflight"}, 400
            )
        mock.direct_switch_seen.append(body.get("no_auto_card_switch") is True)
        mock.direct_cards_sent.append(body["card_id"])
        if mock.direct_no_card_next > 0:  # refused before any order exists: nothing executed
            mock.direct_no_card_next -= 1
            return JSONResponse(
                {"code": 400, "error_code": mock.direct_no_card_code, "msg": "no card"}, 400
            )
        mock.direct_executions += 1
        mock.next_direct += 1
        oid = mock.next_direct
        mock.direct_orders[oid] = {
            "id": oid,
            "client_request_id": rid,
            "plan": plan,
            "product": product,
            "status": "queued",
            "polls": 0,
            "currency": row["checkout_currency"],
            "amount": row["checkout_amount_minor"],
            "card_id": mock.direct_switch_to
            if body.get("no_auto_card_switch") is False and mock.direct_switch_to
            else body["card_id"],
            "cred": hashlib.sha256(cred["session"].encode()).hexdigest()[:16],
        }
        return JSONResponse(
            _env(0, {"id": oid, "plan": plan, "source": "api", "status": "queued",
                     "client_request_id": rid, "currency": row["checkout_currency"],
                     "quoted_amount_minor": row["checkout_amount_minor"]}, msg="accepted"),
            status_code=202,
        )  # fmt: skip

    def _schedule() -> dict[str, Any]:
        """``GET /gpt-direct/card-pool/schedule``: ``pick`` (the card the rules would choose) and
        ``candidates`` (the other cards, each with a ``skip_reason`` or ``""``). Card ids come
        from ``card_pool`` (else every funding card); ``schedule_none`` empties both."""
        if mock.schedule_none:
            return {"pick": None, "candidates": []}
        ids = mock.card_pool or [
            c for c, v in mock.cards.items() if v.get("product_code") == "FUND"
        ]
        usable = [c for c in ids if c not in mock.schedule_skip]
        pick = usable[0] if usable else None
        rest = [
            {"card_id": c, "skip_reason": mock.schedule_skip.get(c, "")} for c in ids if c != pick
        ]
        return {"pick": {"card_id": pick} if pick is not None else None, "candidates": rest}

    def _cancel_order(oid: int) -> JSONResponse:
        o = mock.direct_orders.get(oid)
        if o is None:
            return JSONResponse({"code": 404, "msg": "no order"}, 404)
        if o["status"] != "queued" or mock.direct_cancel_409:  # too late (no error_code)
            return JSONResponse({"code": 409, "msg": "cannot cancel"}, 409)
        o["status"] = "cancelled"
        mock.direct_cancelled += 1
        return JSONResponse(_env(0, {"id": oid, "status": "cancelled"}))

    def _read_order(oid: int) -> JSONResponse:
        o = mock.direct_orders.get(oid)
        if o is None:
            return JSONResponse({"code": 404, "msg": "no order"}, 404)
        if o["status"] in ("queued", "running"):
            o["polls"] += 1
            if o["polls"] == 1:
                pass  # the first read still sees it queued (a cancel is possible)
            elif mock.direct_end == "running" or o["polls"] < mock.direct_polls:
                o["status"] = "running"
            else:
                o["status"] = mock.direct_end
        return JSONResponse(_env(0, {"order": _order_view(o), "events": []}))

    def _write(request: Request, path: str, body: dict[str, Any]) -> JSONResponse:
        if path == "/gpt-direct/orders":
            return _create_order(body)
        m_cancel = re.fullmatch(r"/gpt-direct/orders/([0-9]{1,12})/cancel", path)
        if m_cancel:
            return _cancel_order(int(m_cancel.group(1)))
        if path == "/gpt-direct/cards/reset-usage":
            ids = body.get("card_ids")
            mock.usage_resets.append(
                {"card_ids": ids if isinstance(ids, list) else [], "scope": body.get("scope")}
            )
            for cid in ids if isinstance(ids, list) else list(mock.usage_remaining):
                if isinstance(cid, int):
                    mock.usage_remaining.pop(cid, None)
                    mock.usage_busy.discard(cid)
                    mock.usage_failed.pop(cid, None)
            return JSONResponse(_env(0, {"reset": len(ids) if isinstance(ids, list) else 0}))
        if path == "/gpt-direct/card-rules" and request.method == "PUT":
            prod = body.get("product")
            if prod not in ("gpt", "claude", "grok"):
                return JSONResponse({"code": 400, "error_code": "INVALID_REQUEST"}, 400)
            mock.card_rules[prod] = {**body, "is_default": False}
            return JSONResponse(_env(0, mock.card_rules[prod]))
        if path == "/cards/open":
            return _open_card(body)
        if path == "/cards/recharge":
            return _recharge_card(body)
        m_del = re.fullmatch(r"/cards/([0-9]{1,12})", path)
        if m_del and request.method == "DELETE":
            return _delete_card(int(m_del.group(1)))
        if path == "/gpt-direct/cdks":
            return _issue(body)
        m = re.fullmatch(r"/gpt-direct/cdks/([0-9]{1,12})/disable", path)
        if m:
            cdk = mock.cdks.get(int(m.group(1)))
            if cdk is None or cdk["status"] != "unused":
                return JSONResponse(
                    {"code": 409, "error_code": "CDK_DISABLE_REJECTED", "msg": "not unused"},
                    status_code=409,
                )
            cdk["status"] = "disabled"
            return JSONResponse(_env(0, {"id": cdk["id"], "status": "disabled"}))
        return JSONResponse({"code": 404, "msg": "not implemented in mock"}, status_code=404)

    async def dispatch(request: Request) -> JSONResponse:
        path = "/" + request.path_params["path"]
        if request.method != "GET":
            idem = request.headers.get("idempotency-key")
            if idem is not None and not _IDEM_RE.fullmatch(idem):
                return JSONResponse(
                    {"code": 400, "msg": "Idempotency-Key format error"}, status_code=400
                )
        blocked = _gate(request, path)
        if blocked is not None:
            return blocked
        if request.method == "POST" and path == "/gpt-direct/preflight":
            try:  # read-only upstream: no Idempotency-Key, nothing cached
                pre = json.loads(await request.body() or b"{}")
            except ValueError:
                pre = None
            if not isinstance(pre, dict):
                return JSONResponse({"code": 400, "error_code": "INVALID_REQUEST"}, 400)
            return _preflight(pre)
        if request.method != "GET":
            idem = request.headers.get("idempotency-key")
            if not idem:
                return JSONResponse({"code": 400, "msg": "Idempotency-Key required"}, 400)
            try:
                body = json.loads(await request.body() or b"{}")
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return JSONResponse({"code": 400, "error_code": "invalid_argument"}, 400)
            fingerprint = json.dumps(body, sort_keys=True)
            cached = mock.idem.get(idem)
            if cached is not None:
                if cached[0] != fingerprint:
                    return JSONResponse(
                        {"code": 409, "error_code": "IDEMPOTENCY_CONFLICT",
                         "msg": "key reused with another body"}, 409,
                    )  # fmt: skip
                return JSONResponse(cached[1], headers={"Idempotent-Replayed": "true"})
            scn = str(body.get("plan", "")) if mock.scenarios and path == "/gpt-direct/cdks" else ""
            first = scn.startswith("scn_") and idem not in mock.scn_seen
            if scn == "scn_503":
                return JSONResponse(QUIRKS["503"][1], status_code=503)
            if first:
                mock.scn_seen.add(idem)
                if scn == "scn_409":  # not executed: the next attempt runs
                    return JSONResponse(QUIRKS["409"][1], status_code=409)
            resp = _write(request, path, body)
            if 200 <= resp.status_code < 300:  # only 2xx is cached (vendor A.1)
                mock.idem[idem] = (fingerprint, json.loads(resp.body))
            if first and scn == "scn_lost":  # executed and cached, the answer never arrives
                return JSONResponse({}, status_code=504)
            if first and scn == "scn_202":  # executed and cached, the first answer is "accepted"
                return JSONResponse({"msg": "accepted"}, status_code=202)
            queue = mock.forced.get(path)
            if queue and queue[0] == "lost_response":
                queue.popleft()
                return JSONResponse({}, status_code=504)
            return resp
        if path == "/cards":  # [V] list: {total, list}; carries the full PAN like the real one
            try:
                page = max(1, int(request.query_params.get("page", "1")))
                size = min(mock.list_cap, max(1, int(request.query_params.get("page_size", "20"))))
            except ValueError:
                return JSONResponse({"code": 400, "msg": "bad paging"}, status_code=400)
            live = [c for _, c in sorted(mock.cards.items()) if c["status"] != "DELETED"]
            rows = [
                {"id": c["id"], "card_number": c["card_number"], "product_code": c["product_code"],
                 "available_amount": c["available_amount"], "status": c["status"]}
                for c in live[(page - 1) * size : page * size]
            ]  # fmt: skip
            return JSONResponse(_env(0, {"total": len(live), "list": rows}))
        m_card = re.fullmatch(r"/cards/([0-9]{1,12})(/recharges)?", path)
        if m_card:
            card = mock.cards.get(int(m_card.group(1)))
            if card is None:
                return JSONResponse({"code": 404, "error_code": "not_found", "msg": "no card"}, 404)
            if m_card.group(2):
                rows = mock.recharges[card["id"]]
                for r in rows:  # a pending recharge settles on the second read
                    if r["status"] == "pending":
                        r["polls"] += 1
                        if r["polls"] >= 2:
                            r["status"] = "success"
                            card["available_amount"] += r["amount"]
                            _log(card, "card_recharge", -r["amount"])
                return JSONResponse(
                    _env(0, [{k: v for k, v in r.items() if k != "polls"} for r in rows])
                )
            if card["status"] == "DELETING":
                card["polls"] = card.get("polls", 0) + 1
                if card["polls"] >= 2:
                    card["status"] = "DELETED"
                    _log(card, "card_refund", card["available_amount"])
                    card["available_amount"] = 0.0
            if mock.malformed_detail > 0:
                mock.malformed_detail -= 1
                view = _card_view(card)
                view.pop("status"), view.pop("available_amount")  # secrets present, shape broken
                return JSONResponse(_env(0, view))
            return JSONResponse(_env(0, _card_view(card)))
        m_usage = re.fullmatch(r"/gpt-direct/cards/([0-9]{1,12})/usage", path)
        if m_usage:
            cid = int(m_usage.group(1))
            if cid not in mock.cards:
                return JSONResponse({"code": 404, "msg": "no card"}, 404)
            left = mock.usage_remaining.get(cid, 5)
            return JSONResponse(
                _env(0, {"card_id": cid, "card_status": mock.cards[cid]["status"],
                         "product": request.query_params.get("product", "gpt"),
                         "light": {"used": 5 - left, "remaining": left, "cap": 5,
                                   "failed": mock.usage_failed.get(cid, 0)},
                         "pro20": {"used": 0, "remaining": 3, "cap": 3, "failed": 0},
                         "count_failures": True, "cooling": False,
                         "decline_count": mock.usage_failed.get(cid, 0),
                         "busy": cid in mock.usage_busy})
            )  # fmt: skip
        if path == "/gpt-direct/card-pool/schedule":
            mock.schedule_calls += 1
            return JSONResponse(_env(0, _schedule()))
        if path == "/gpt-direct/card-rules":
            prod = request.query_params.get("product")
            rules = {
                p: mock.card_rules.get(
                    p, {"product": p, "count_failures": True, "light_max_uses": 5,
                        "pro20_max_uses": 3, "select_mode": "default", "is_default": True,
                     "auto_switch_on_fail": True, "max_auto_switches": 2,
                     "strict_select": False, "select_priority": []}
                )
                for p in ("gpt", "claude", "grok")
            }  # fmt: skip
            if prod in rules:
                return JSONResponse(_env(0, rules[prod]))
            return JSONResponse(_env(0, {"list": list(rules.values())}))
        m_order = re.fullmatch(r"/gpt-direct/orders/([0-9]{1,12})", path)
        if m_order:
            return _read_order(int(m_order.group(1)))
        if path == "/gpt-direct/orders":
            try:
                page = max(1, int(request.query_params.get("page", "1")))
                size = min(mock.list_cap, max(1, int(request.query_params.get("page_size", "20"))))
            except ValueError:
                return JSONResponse({"code": 400, "msg": "bad paging"}, status_code=400)
            ordered = sorted(mock.direct_orders.values(), key=lambda o: -o["id"])
            rows = [
                {k: v for k, v in _order_view(o).items() if k != "account_email"}
                for o in ordered[(page - 1) * size : page * size]
            ]
            return JSONResponse(_env(0, {"total": len(ordered), "list": rows}))
        if path == "/gpt-direct/cdks":
            items = sorted(mock.cdks.values(), key=lambda c: -c["id"])
            return JSONResponse(_env(0, {"total": len(items), "list": items}))
        if path == "/balance":
            if mock.scenarios:  # the E2E smoke buys cards: enough wallet for the balance breaker
                return JSONResponse(
                    _env(0, {**BALANCE_FIXTURE, "balance": 5000, "spendable_balance": 5000})
                )
            return JSONResponse(_env(0, BALANCE_FIXTURE))
        if path == "/products":
            return JSONResponse(_env(0, products_fixture()))
        if path == "/gpt-direct/plans":
            product = request.query_params.get("product", "gpt")
            if product not in ("gpt", "claude", "grok"):
                return JSONResponse(
                    {"code": 400, "error_code": "INVALID_REQUEST", "msg": "unknown product"},
                    status_code=400,
                )
            return JSONResponse(_env(0, plans_fixture(product, mock.scenarios)))
        if path == "/gpt-direct/card-products":
            items = [
                {
                    "product_code": "MOCK01",
                    "issuer": "one",
                    "bin": "000000",
                    "label": "Mock card",
                    "enabled": True,
                    "suspended": False,
                    "channel_open": True,
                    "auto_open_allowed": True,
                    "usable": True,
                }
            ]
            return JSONResponse(_env(0, {"list": items}))
        if path == "/vip":
            return JSONResponse(_env(0, {"tier": "none", "is_svip": False}))
        if path == "/balance-logs":
            try:
                page = max(1, int(request.query_params.get("page", "1")))
                size = min(200, max(1, int(request.query_params.get("page_size", "20"))))
            except ValueError:
                return JSONResponse({"code": 400, "msg": "bad paging"}, status_code=400)
            chunk = mock.balance_logs[(page - 1) * size : page * size]
            return JSONResponse(
                _env(
                    0,
                    {"total": len(mock.balance_logs), "list": chunk, "spendable_balance": 12.34},
                )
            )
        return JSONResponse({"code": 404, "msg": "not found"}, status_code=404)

    # ---- public redemption (no API key): the buyer-facing four steps
    def _rerr(status: int, error_code: str, msg: str) -> JSONResponse:
        return JSONResponse({"code": status, "error_code": error_code, "msg": msg}, status)

    def _device(request: Request) -> str:
        return request.headers.get("x-redemption-device") or request.headers.get("user-agent", "")

    def _session(
        request: Request, body: dict[str, Any], step: str
    ) -> dict[str, Any] | JSONResponse:
        token = body.get("redemption_token")
        sess = mock.sessions.get(token) if isinstance(token, str) else None
        has_cred = isinstance(body.get("credential"), dict)
        mock.redeem_calls.append((step, request.headers.get("x-redemption-device", ""), has_cred))
        if sess is None or sess["expires"] < time.time() or sess["device"] != _device(request):
            return _rerr(400, "REDEMPTION_SESSION_INVALID", "session invalid")
        return sess

    def _good_credential(cred: Any) -> bool:
        return (
            isinstance(cred, dict)
            and cred.get("mode") == "session"
            and isinstance(cred.get("session"), str)
            and bool(cred["session"])
            and "invalid" not in cred["session"]
        )

    async def cdk_step(request: Request) -> JSONResponse:
        step = request.path_params["step"]
        if step == "result":
            token = request.query_params.get("token", "")
            mock.redeem_calls.append(
                ("result", request.headers.get("x-redemption-device", ""), False)
            )
            sess = mock.sessions.get(token)
            if mock.result_404 or sess is None or sess["device"] != _device(request):
                return _rerr(404, "not_found", "not found")
            order = sess.get("order")
            if order is None:
                return _rerr(404, "not_found", "not found")
            return JSONResponse(
                _env(
                    0,
                    {
                        "order": {**order, "stage": "completed", "cdk_status": "unused"},
                        "events": [{"type": "order.succeeded", "created_at": order["created_at"]}],
                    },
                )
            )
        if request.method != "POST":
            return _rerr(405, "method_not_allowed", "POST only")
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return _rerr(400, "INVALID_REQUEST", "bad body")
        if step == "preview":
            code = body.get("code")
            mock.redeem_calls.append(
                ("preview", request.headers.get("x-redemption-device", ""), False)
            )
            cdk = next((c for c in mock.cdks.values() if c["code"] == code), None)
            if cdk is None or cdk["status"] != "unused":
                return _rerr(400, "CDK_UNAVAILABLE", "code unavailable")
            token = "rt_" + secrets.token_hex(16)
            expires = datetime.now(timezone.utc) + timedelta(minutes=15)
            mock.sessions[token] = {
                "cdk": cdk["id"], "device": _device(request), "expires": time.time() + 900,
                "plan": cdk["plan"], "preflight": None,
            }  # fmt: skip
            return JSONResponse(
                _env(
                    0,
                    {
                        "redemption_token": token,
                        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "plan": cdk["plan"],
                        "plan_flow": "checkout",
                        "funding_cap_minor": 0,
                    },
                )
            )
        sess = _session(request, body, step)
        if isinstance(sess, JSONResponse):
            return sess
        tokenless = str(sess["plan"]).startswith(("grok_", "claude"))
        if step == "preflight":
            if not _good_credential(body.get("credential")):
                return _rerr(400, "GPT_SESSION_INVALID", "session rejected")
            sess["credential_ok"] = True
            ptoken = None if tokenless else "pf_" + secrets.token_hex(16)
            sess["preflight"] = ptoken
            return JSONResponse(
                _env(
                    0,
                    {
                        "preflight_token": ptoken,
                        "preflight_expires_at": (
                            datetime.now(timezone.utc) + timedelta(minutes=10)
                        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "payment_country": "PH",
                        "payment_currency": "PHP",
                        "quotes": None,
                    },
                )
            )
        if step == "redeem":
            rid = body.get("client_request_id")
            if not isinstance(rid, str) or not rid:
                return _rerr(400, "INVALID_REQUEST", "client_request_id required")
            fingerprint = json.dumps(
                {k: body.get(k) for k in ("redemption_token", "preflight_token")}, sort_keys=True
            )
            prior = mock.redeem_orders.get(rid)
            if prior is not None:
                if prior["fp"] != fingerprint:
                    return _rerr(409, "IDEMPOTENCY_CONFLICT", "other parameters")
                return JSONResponse(_env(0, {"order": prior["order"]}))
            if tokenless:
                if not _good_credential(body.get("credential")):
                    return _rerr(400, "CREDENTIAL_REQUIRED", "credential required")
            elif body.get("preflight_token") is None:
                return _rerr(400, "PREFLIGHT_REQUIRED", "preflight required")
            elif body.get("preflight_token") != sess["preflight"]:
                return _rerr(400, "REDEMPTION_SESSION_INVALID", "preflight invalid")
            cdk = mock.cdks.get(sess["cdk"])
            if cdk is None or cdk["status"] != "unused":
                return _rerr(400, "CDK_UNAVAILABLE", "code unavailable")
            order = {
                "order_id": f"SBXORD-{secrets.token_hex(6).upper()}",
                "status": "succeeded",
                "sandbox_mock": True,
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            sess["order"] = order
            mock.redeem_orders[rid] = {"fp": fingerprint, "order": order}
            return JSONResponse(_env(0, {"order": order}))
        return _rerr(404, "not_found", "unknown step")

    async def force(request: Request) -> JSONResponse:
        try:
            data = json.loads(await request.body())
            mock.force(str(data["path"]), str(data["quirk"]), int(data.get("times", 1)))
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"code": 400, "msg": "bad force request"}, status_code=400)
        return JSONResponse(_env(0))

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/__mock/force", force, methods=["POST"]),
            Route("/api/v1/cdk/{step}", cdk_step, methods=["GET", "POST"]),
            Route(
                PREFIX + "/{path:path}",
                dispatch,
                methods=["GET", "POST", "PUT", "DELETE"],
            ),
        ]
    )
    app.state.mock = mock
    return app


async def _main() -> None:
    import uvicorn

    if os.environ.get("APP_ENV", "dev") not in ("dev", "test"):
        raise SystemExit("mock_zovocard only runs with APP_ENV=dev|test")
    host = os.environ.get("APP_MOCKZOVO_HOST", "127.0.0.1")
    port = int(os.environ.get("APP_MOCKZOVO_PORT", "8005"))
    scenarios = os.environ.get("APP_MOCKZOVO_SCENARIOS", "") == "1"
    state = MockZovo(
        scenarios=scenarios,
        result_404=os.environ.get("APP_MOCKZOVO_RESULT_404", "") == "1",
        direct_polls=2 if scenarios else 3,  # the E2E smoke waits for the order: keep it short
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_mock_zovocard(state),
            host=host,
            port=port,
            access_log=False,
            log_config=None,
            server_header=False,
            proxy_headers=False,
            lifespan="off",
            loop="asyncio",
            http="h11",
        )
    )
    await server.serve()


if __name__ == "__main__":
    asyncio.run(_main())
