"""Mock card.zzshu.pro third-party API (ADR-054): dev and tests only, never a real vendor.

Routes under ``/api/v1/third-party``: ``GET /user``, ``POST /orders/direct`` (answers 201 with
``card_key`` and ``status: processing``), ``POST /orders/status`` (a status reply that, like the
real one, carries the full card number and the session ``token``: the adapter must drop them).

Knobs on :class:`MockZzshu`: ``points``, ``create`` (``ok`` | ``lost`` = executed but the answer is
a 504 | ``queue_full`` | ``bad_gateway`` | ``refuse_region`` | ``refuse_points``),
``polls_before_final`` and ``final`` (``success`` | ``failed``). ``orders`` keeps what was
created; ``creates`` counts every create call (a second one proves a resend).
Refuses to start as a server unless ``APP_ENV`` is dev or test.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

PREFIX = "/api/v1/third-party"
OK_KEY = "zz_mock_ok"
PAN_FIELD = "bank_card_no"


@dataclass
class MockZzshu:
    points: int = 20
    create: str = "ok"
    final: str = "success"
    polls_before_final: int = 1
    failure_reason: str = "payment declined"
    creates: int = 0
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    # the card + session of every create, so tests can assert that none of it leaked
    seen_cards: list[dict[str, Any]] = field(default_factory=list)
    requests: list[tuple[str, str]] = field(default_factory=list)


def _env(code: int, data: Any = None, msg: str = "success") -> dict[str, Any]:
    return {"code": code, "message": msg, "data": data}


def create_mock_zzshu(state: MockZzshu | None = None) -> Starlette:
    mock = state or MockZzshu()

    def err(http: int, code: int, msg: str) -> JSONResponse:
        return JSONResponse(_env(code, None, msg), status_code=http)

    async def dispatch(request: Request) -> JSONResponse:
        path = request.url.path[len(PREFIX) :]
        mock.requests.append((request.method, path))
        if request.headers.get("x-api-key") != OK_KEY:
            return err(401, 40107, "invalid key")
        if path == "/user" and request.method == "GET":
            return JSONResponse(_env(0, {"id": "0", "username": "api-user", "points": mock.points}))
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            return err(422, 42200, "bad json")
        if path == "/orders/direct" and request.method == "POST":
            return create_order(body)
        if path == "/orders/status" and request.method == "POST":
            return order_status(body)
        return err(404, 40400, "not found")

    def create_order(body: dict[str, Any]) -> JSONResponse:
        mock.creates += 1
        tok = body.get("token")
        card = {k: body.get(k) for k in ("cardNumber", "expMonth", "expYear", "cvv")}
        mock.seen_cards.append({**card, "token": tok})
        if mock.create == "queue_full":
            return err(429, 42902, "busy")
        if mock.create == "bad_gateway":
            return err(502, 50300, "unavailable")
        if mock.create == "refuse_region":
            return err(400, 40020, "region not offered")
        if mock.create == "refuse_points":
            return err(403, 40306, "no points")
        if (
            body.get("orderType") != "direct"
            or not isinstance(tok, dict)
            or not re.fullmatch(r"[0-9]{12,19}", str(card["cardNumber"]))
            or not isinstance(card["expMonth"], int)
            or not isinstance(card["expYear"], int)
            or card["expYear"] < 2000
            or not re.fullmatch(r"[0-9]{3,4}", str(card["cvv"]))
            or body.get("planType") not in ("go", "plus", "pro5", "pro20")
            or body.get("region") not in ("PH", "US", "EG", "CL")
        ):
            return err(400, 40020, "bad parameters")
        n = len(mock.orders) + 1
        key = f"DIRECT-{n:04d}"
        mock.orders[key] = {
            "order_no": str(n),
            "plan_type": body["planType"],
            "region": body["region"],
            "email": (tok.get("user") or {}).get("email"),
            "polls": 0,
            "card": card,
            "token": tok,
        }
        if mock.create == "lost":
            return JSONResponse({}, status_code=504)  # executed, the answer never arrives
        return JSONResponse(
            _env(
                0,
                {
                    "order_no": str(n),
                    "status": "processing",
                    "email": mock.orders[key]["email"],
                    "card_key": key,
                    "order_type": "direct",
                    "plan_type": body["planType"],
                    "region": body["region"],
                },
            ),
            status_code=201,
        )

    def order_status(body: dict[str, Any]) -> JSONResponse:
        o = mock.orders.get(str(body.get("cardKey")))
        if o is None:
            return err(404, 40403, "no such order")
        o["polls"] += 1
        status = mock.final if o["polls"] > mock.polls_before_final else "processing"
        return JSONResponse(
            _env(
                0,
                {
                    "order_no": o["order_no"],
                    "order_type": "direct",
                    "plan_type": o["plan_type"],
                    "region": o["region"],
                    "card_key": str(body.get("cardKey")),
                    "email": o["email"],
                    "is_subscription_cancelled": 1 if status == "success" else 0,
                    "status": status,
                    "failure_reason": mock.failure_reason if status == "failed" else None,
                    # like the real reply: the full card number and the session come back
                    PAN_FIELD: o["card"]["cardNumber"],
                    "token": o["token"],
                },
            )
        )

    app = Starlette(routes=[Route(PREFIX + "/{path:path}", dispatch, methods=["GET", "POST"])])
    app.state.mock = mock
    return app


def main() -> None:
    import uvicorn

    if os.environ.get("APP_ENV", "dev") not in ("dev", "test"):
        raise SystemExit("mock_zzshu only runs with APP_ENV=dev|test")
    uvicorn.run(
        create_mock_zzshu(),
        host=os.environ.get("APP_MOCKZZSHU_HOST", "127.0.0.1"),
        port=int(os.environ.get("APP_MOCKZZSHU_PORT", "8006")),
    )


if __name__ == "__main__":
    main()
