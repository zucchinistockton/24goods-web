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

Force a quirk from tests with ``MockZovo.force(path, name)`` or, on a running dev server,
``POST /__mock/force {"path": "/balance", "quirk": "429"}``. Refuses to start unless
``APP_ENV`` is dev or test.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

PREFIX = "/openapi/v1"
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


def plans_fixture(product: str) -> dict[str, Any]:
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
        regions = [{"country": "PH", "currency": "PHP"}, {"country": "US", "currency": "USD"}]
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
        if queue:
            status, body = QUIRKS[queue.popleft()]
            return JSONResponse(body, status_code=status)
        return None

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
        if request.method != "GET":
            return JSONResponse({"code": 404, "msg": "not implemented in mock"}, status_code=404)
        if path == "/balance":
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
            return JSONResponse(_env(0, plans_fixture(product)))
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
            return JSONResponse(_env(0, {"total": 0, "list": [], "spendable_balance": 12.34}))
        return JSONResponse({"code": 404, "msg": "not found"}, status_code=404)

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
    server = uvicorn.Server(
        uvicorn.Config(
            create_mock_zovocard(),
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
