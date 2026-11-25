"""Mock vmcardio V2 API (ADR-055): dev and tests only, never a real vendor.

Routes: ``GET /v2/getAccessToken``, ``POST /v2/getAccountBalance``, ``getProductCode``,
``createCard``, ``cardDetail``, ``getCardList``, ``refundCard``, ``deleteCard``. Envelope
``{"code": 0, "msg": "ok", "data": ...}``; ``Authorization: <token>`` without ``Bearer``;
``X-Client-Request-Id`` is unique forever (a repeat answers ``Duplicate Client Request Id`` and not
the first result). Card detail and card list carry the FULL card number (like the real vendor), the
list carries NO label.

Knobs on :class:`MockVmcardio`: ``balance``, ``products`` (bin -> (code, remaining)),
``create`` (``ok`` | ``lost`` = executed but the answer is a 504 | ``refuse`` = business refusal |
``bad_gateway`` = 502 before anything ran), ``refuse_delete``, ``token_ttl``,
``expire_tokens_once`` (the next call answers 400003 once). ``cards`` keeps the cards;
``creates`` counts executed creations.
Refuses to start as a server unless ``APP_ENV`` is dev or test.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

APP_ID = "vm_mock_app"
APP_SECRET = "vm_mock_secret"  # noqa: S105
TEST_PAN = "5378720000001234"


def _env(code: int, data: Any = None, msg: str = "ok") -> dict[str, Any]:
    return {"code": code, "msg": msg, "data": data}


@dataclass
class MockVmcardio:
    balance: Decimal = Decimal("500")
    # bin -> (product_code, remaining_open_card_num)
    products: dict[str, tuple[str, int]] = field(
        default_factory=lambda: {
            "537872": ("P5378OX", 5),
            "537882": ("P5378XB", 5),
            "532604": ("P5326XK", 5),
            "555659": ("P5556XV", 5),
            "446619": ("X4466BD", 5),
        }
    )
    create: str = "ok"
    refuse_delete: bool = False
    token_ttl: int = 7200
    expire_tokens_once: bool = False
    creates: int = 0
    tokens_issued: int = 0
    cards: dict[str, dict[str, Any]] = field(default_factory=dict)
    request_ids: set[str] = field(default_factory=set)
    requests: list[tuple[str, str]] = field(default_factory=list)
    token: str = ""
    # a recent create time (UTC) the next card gets; tests may age a card with it
    created_at: datetime | None = None


def create_mock_vmcardio(state: MockVmcardio | None = None) -> Starlette:
    mock = state or MockVmcardio()

    def err(code: int, msg: str, http: int = 200) -> JSONResponse:
        return JSONResponse(_env(code, None, msg), status_code=http)

    async def token(request: Request) -> JSONResponse:
        mock.requests.append(("GET", "/v2/getAccessToken"))
        q = request.query_params
        if q.get("app_id") != APP_ID:
            return err(400006, "Invalid AppId")
        if q.get("app_secret") != APP_SECRET:
            return err(400007, "Invalid AppSecret")
        mock.tokens_issued += 1
        mock.token = f"tok-{uuid.uuid4()}"
        return JSONResponse(_env(0, {"token": mock.token, "expired_time": mock.token_ttl}))

    async def dispatch(request: Request) -> JSONResponse:
        path = request.url.path
        mock.requests.append((request.method, path))
        if request.headers.get("authorization") != mock.token or not mock.token:
            return err(400003, "Invalid Token")
        if mock.expire_tokens_once:
            mock.expire_tokens_once = False
            mock.token = ""
            return err(400003, "Invalid Token")
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            return err(400001, "Params Content Error")
        rid = request.headers.get("x-client-request-id")
        if path in ("/v2/createCard", "/v2/refundCard", "/v2/deleteCard") and rid:
            if rid in mock.request_ids:
                return err(400009, "Duplicate Client Request Id")
            mock.request_ids.add(rid)
        if path == "/v2/getAccountBalance":
            return JSONResponse(_env(0, {"balance": float(mock.balance), "wallet_balance": 0}))
        if path == "/v2/getProductCode":
            return JSONResponse(_env(0, {"list": product_list()}))
        if path == "/v2/createCard":
            return create_card(body)
        if path == "/v2/cardDetail":
            return card_detail(body)
        if path == "/v2/getCardList":
            return card_list(body)
        if path == "/v2/refundCard":
            return refund(body)
        if path == "/v2/deleteCard":
            return delete(body)
        return err(404, "not found", 404)

    def product_list() -> list[dict[str, Any]]:
        return [
            {
                "bin": b,
                "product_code": code,
                "type": "save",
                "network": "MasterCard",
                "media": "virtual",
                "issuing_area": "US",
                "remaining_open_card_num": left,
            }
            for b, (code, left) in mock.products.items()
        ] + [
            {
                "bin": "000000",
                "product_code": "SHARE1",
                "type": "share",
                "network": "Visa",
                "media": "virtual",
                "issuing_area": "HK",
                "remaining_open_card_num": 9,
            }
        ]

    def create_card(body: dict[str, Any]) -> JSONResponse:
        if mock.create == "bad_gateway":
            return JSONResponse({}, status_code=502)
        if mock.create == "refuse":
            return err(500101, "Insufficient Balance")
        amount = Decimal(str(body.get("amount", 0)))
        code = body.get("product_code")
        prod = next((b for b, (c, _n) in mock.products.items() if c == code), None)
        if (
            prod is None
            or amount < 1
            or amount > mock.balance
            or not body.get("first_name")
            or not body.get("last_name")
            or mock.products[prod][1] <= 0
        ):
            return err(500100, "Create Card Failed")
        mock.creates += 1
        mock.balance -= amount
        c, n = mock.products[prod]
        mock.products[prod] = (c, n - 1)
        vid = f"vmc{uuid.uuid4().hex[:18]}"
        mock.cards[vid] = {
            "vm_card_id": vid,
            "card_id": f"card{uuid.uuid4().hex[:12]}",
            "card_number": f"{prod}{len(mock.cards):04d}{TEST_PAN[-6:]}"[:16],
            "cvv": "737",
            "expire": "12/30",
            "status": "ACTIVE",
            "available_amount": float(amount),
            "product_code": code,
            "label": body.get("label"),
            "card_address": body.get("card_address"),
            "create_time": (mock.created_at or datetime.now(timezone.utc)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        }
        if mock.create == "lost":
            return JSONResponse({}, status_code=504)  # executed, the answer never arrives
        return JSONResponse(_env(0, {"card_id": mock.cards[vid]["card_id"], "vm_card_id": vid}))

    def card_detail(body: dict[str, Any]) -> JSONResponse:
        c = mock.cards.get(str(body.get("vm_card_id")))
        if c is None:
            return err(400404, "Card Not Found")
        keep = (
            "card_id", "card_number", "cvv", "expire", "status", "available_amount",
        )  # fmt: skip
        return JSONResponse(_env(0, {k: c[k] for k in keep}))

    def card_list(body: dict[str, Any]) -> JSONResponse:
        rows = [
            {k: c[k] for k in (
                "card_id", "card_number", "product_code", "available_amount", "status",
                "create_time", "vm_card_id",
            )}
            for c in mock.cards.values()
            if (not body.get("status") or c["status"] == body["status"])
            and (not body.get("product_code") or c["product_code"] == body["product_code"])
        ]  # fmt: skip
        page, size = int(body.get("page") or 1), int(body.get("page_size") or 10)
        part = rows[(page - 1) * size : page * size]
        return JSONResponse(_env(0, {"total": len(rows), "list": part}))

    def refund(body: dict[str, Any]) -> JSONResponse:
        c = mock.cards.get(str(body.get("vm_card_id")))
        amount = Decimal(str(body.get("amount", 0)))
        if c is None or c["status"] != "ACTIVE":
            return err(400404, "Card Not Found")
        if amount <= 0 or Decimal(str(c["available_amount"])) - amount < Decimal("0.10"):
            return err(500102, "Below Minimum Balance")
        c["available_amount"] = float(Decimal(str(c["available_amount"])) - amount)
        mock.balance += amount
        return JSONResponse(_env(0, {"data": "true"}))

    def delete(body: dict[str, Any]) -> JSONResponse:
        if mock.refuse_delete:
            return err(500103, "Delete Failed")
        c = mock.cards.get(str(body.get("vm_card_id")))
        if c is None or c["status"] == "DELETED":
            return err(400404, "Card Not Found")
        mock.balance += Decimal(str(c["available_amount"]))
        c["available_amount"] = 0
        c["status"] = "DELETED"
        return JSONResponse(_env(0, {"data": "true"}))

    app = Starlette(
        routes=[
            Route("/v2/getAccessToken", token, methods=["GET"]),
            Route("/v2/{path:path}", dispatch, methods=["POST"]),
        ]
    )
    app.state.mock = mock
    return app


def main() -> None:
    import uvicorn

    if os.environ.get("APP_ENV", "dev") not in ("dev", "test"):
        raise SystemExit("mock_vmcardio only runs with APP_ENV=dev|test")
    uvicorn.run(
        create_mock_vmcardio(),
        host=os.environ.get("APP_MOCKVMCARDIO_HOST", "127.0.0.1"),
        port=int(os.environ.get("APP_MOCKVMCARDIO_PORT", "8007")),
    )


if __name__ == "__main__":
    main()
