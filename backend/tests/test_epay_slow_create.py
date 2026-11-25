"""2026-11-25: a slow EPay create must not block the order; an old unknown attempt is recovered;
the admin gateway test goes through the relay."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, text

from app.core import safefetch
from app.models import PaymentAttempt
from app.modules.admin import payment_ops
from app.modules.payments.reconcile import OPEN_STATUSES
from app.payments import egress, zpay
from app.payments.zpay import QueryResult, ZPayClient, ZPayConfig, ZPayQueryError
from tests.test_gateway_routing_v10 import GW, PAY, _attempts, _order, _setup


def _client(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cfg = ZPayConfig(
        "pid", "k" * 16, "https://p.example", "https://p.example", "https://s/n", "https://s/r"
    )
    return ZPayClient(cfg, http)


async def test_create_gets_a_longer_read_budget_than_queries():
    seen: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = dict(request.extensions["timeout"])
        if request.url.path == "/mapi.php":
            return httpx.Response(
                200, json={"code": 1, "payurl": "https://qr.alipay.com/x", "trade_no": "T1"}
            )
        return httpx.Response(200, json={"code": -1, "msg": "not found"})

    c = _client(handler)
    out = await c.create_payment("GX1", "i", Decimal("1.00"), "1.1.1.1")
    assert out["payload"] == "https://qr.alipay.com/x"
    await c.query_order("GX1")
    # snpay answered a create after ~9 s: the old 6 s read budget gave up and blocked the order
    assert seen["/mapi.php"]["read"] >= 15.0 > 9.0
    assert seen["/api.php"]["read"] == zpay.QUERY_TIMEOUT_S == 6.0
    assert zpay.CREATE_TIMEOUT_S > seen["/mapi.php"]["read"]
    assert zpay.CREATE_CONNECT_S < zpay.CREATE_TIMEOUT_S


async def _unknown(env, monkeypatch, *, age_s=60):
    behaviour: dict = {"gwa": ZPayQueryError("query failed: ReadTimeout"), "gwb": None}
    _admin, buyer, _a, _b, _calls = await _setup(env, monkeypatch, behaviour)
    order = await _order(env, buyer)
    r = await buyer.post(PAY.format(order["id"]), json={"channel": "zpay_alipay"})
    assert r.status_code == 503, r.text
    (att,) = await _attempts(env)
    assert att.status == "unknown"
    behaviour["gwa"] = None
    async with env.owner_session() as db:
        await db.execute(
            text("UPDATE payment_attempts SET created_at = now() - make_interval(secs => :s)"),
            {"s": age_s},
        )
        await db.commit()
    return buyer, order, att


def _stub_query(monkeypatch, answer):
    asked: list[str] = []

    async def query(self, no):
        asked.append(no)
        if isinstance(answer, Exception):
            raise answer
        return answer(no)

    monkeypatch.setattr(ZPayClient, "query_order", query)
    return asked


@pytest.mark.parametrize(
    ("answer", "expect"),
    [
        (lambda no: QueryResult(False, False, None, "", ""), "not_found"),
        (lambda no: QueryResult(True, False, Decimal("1.00"), "G1", no), "unpaid"),
    ],
)
async def test_old_unknown_attempt_is_superseded_and_a_fresh_one_created(
    env, monkeypatch, answer, expect
):
    buyer, order, old = await _unknown(env, monkeypatch)
    asked = _stub_query(monkeypatch, answer)
    r = await buyer.post(PAY.format(order["id"]), json={"channel": "zpay_alipay"})
    assert r.status_code == 200 and r.json()["pay_url"].startswith("/pay/epay/"), r.text
    assert asked == [old.gateway_ref]
    first, second = await _attempts(env)
    assert first.status == "superseded" and first.id == old.id
    assert second.status == "ready" and second.gateway_ref != first.gateway_ref
    detail = await env.scalar(
        "SELECT detail->>'gateway_answer' FROM audit_log"
        " WHERE action='payment.attempt_superseded_unknown' AND tenant_id=:t",
        t=str(env.tenant_id),
    )
    assert detail == expect
    # a late payment on the old reference still settles: it stays in reconcile's poll set
    assert "superseded" in OPEN_STATUSES


async def test_old_unknown_attempt_that_is_paid_is_left_to_reconcile(env, monkeypatch):
    buyer, order, _old = await _unknown(env, monkeypatch)
    _stub_query(monkeypatch, lambda no: QueryResult(True, True, Decimal("1.00"), "G1", no))
    r = await buyer.post(PAY.format(order["id"]), json={"channel": "zpay_alipay"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "payment_pending_check", r.text
    assert [a.status for a in await _attempts(env)] == ["unknown"]


async def test_old_unknown_attempt_with_a_failing_query_keeps_the_block(env, monkeypatch):
    buyer, order, _old = await _unknown(env, monkeypatch)
    _stub_query(monkeypatch, ZPayQueryError("query failed: ReadTimeout"))
    r = await buyer.post(PAY.format(order["id"]), json={"channel": "zpay_alipay"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "payment_pending_check", r.text
    assert [a.status for a in await _attempts(env)] == ["unknown"]


async def test_young_unknown_attempt_is_not_even_queried(env, monkeypatch):
    buyer, order, _old = await _unknown(env, monkeypatch, age_s=2)
    asked = _stub_query(monkeypatch, lambda no: QueryResult(False, False, None, "", ""))
    r = await buyer.post(PAY.format(order["id"]), json={"channel": "zpay_alipay"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "payment_pending_check", r.text
    assert asked == []
    assert [a.status for a in await _attempts(env)] == ["unknown"]


async def test_recovery_never_touches_another_tenants_attempts(env, monkeypatch):
    buyer, order, _old = await _unknown(env, monkeypatch)
    other = str(env.other_tenant_id)
    count = "SELECT count(*) FROM payment_attempts WHERE tenant_id=:t"
    before = await env.scalar(count, t=other)
    _stub_query(monkeypatch, lambda no: QueryResult(False, False, None, "", ""))
    r = await buyer.post(PAY.format(order["id"]), json={"channel": "zpay_alipay"})
    assert r.status_code == 200, r.text
    assert await env.scalar(count, t=other) == before == 0
    assert (
        await env.scalar(
            "SELECT count(*) FROM audit_log WHERE action='payment.attempt_superseded_unknown'"
            " AND tenant_id=:t",
            t=other,
        )
        == 0
    )
    async with env.api_session(env.other_tenant_id) as db:
        assert list(await db.scalars(select(PaymentAttempt))) == []


# ---------------------------------------------------------------------------- admin gateway test
async def _gateway_test(env, monkeypatch, *, relay, relay_only, relay_ok=True, direct_ok=True):
    admin, _buyer, a, _b, _calls = await _setup(env, monkeypatch, {"gwa": None, "gwb": None})
    connects: list[tuple[str, int]] = []

    async def tcp(host, port):
        connects.append((host, port))
        if (host, port) == ("10.0.0.9", 20443) and not relay_ok:
            raise OSError("down")
        if host == "93.184.216.34" and not direct_ok:
            raise OSError("blocked")
        return 12

    async def vet(url):
        return SimpleNamespace(ip="93.184.216.34", port=443)

    async def probe(self, no):
        return {"code": "-1", "msg": "not found"}

    monkeypatch.setattr(payment_ops, "tcp_connect_ms", tcp)
    monkeypatch.setattr(safefetch, "vet_url", vet)
    monkeypatch.setattr(ZPayClient, "probe_order", probe)
    monkeypatch.setattr(
        egress, "relays", lambda: {"gwa.example": ("10.0.0.9", 20443)} if relay else {}
    )
    monkeypatch.setattr(egress, "relay_only", lambda: relay_only)
    async with env.owner_session() as db:
        await db.execute(
            text("UPDATE payment_gateways SET gateway='https://gwa.example' WHERE id=:i"),
            {"i": uuid.UUID(a[0])},
        )
        await db.commit()
    r = await admin.post(f"{GW}/{a[0]}/test")
    return r, connects


async def test_gateway_test_uses_the_relay_when_relay_only(env, monkeypatch):
    r, connects = await _gateway_test(
        env, monkeypatch, relay=True, relay_only=True, direct_ok=False
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["route"] == "relay", body
    assert connects == [("10.0.0.9", 20443)]


async def test_gateway_test_relay_only_reports_failure_without_trying_direct(env, monkeypatch):
    r, connects = await _gateway_test(env, monkeypatch, relay=True, relay_only=True, relay_ok=False)
    body = r.json()
    assert body["ok"] is False and body["stage"] == "connect" and body["route"] == "relay"
    assert connects == [("10.0.0.9", 20443)]


async def test_gateway_test_with_relay_falls_back_to_direct(env, monkeypatch):
    r, connects = await _gateway_test(
        env, monkeypatch, relay=True, relay_only=False, relay_ok=False
    )
    body = r.json()
    assert body["ok"] is True and body["route"] == "direct"
    assert [c[0] for c in connects] == ["10.0.0.9", "93.184.216.34"]


async def test_gateway_test_without_relay_is_direct(env, monkeypatch):
    r, connects = await _gateway_test(env, monkeypatch, relay=False, relay_only=False)
    body = r.json()
    assert body["ok"] is True and body["route"] == "direct"
    assert connects == [("93.184.216.34", 443)]


@pytest.mark.parametrize("msg", ["订单不存在", "订单号不存在", "order not found"])
def test_not_found_wordings_of_xiaodou_and_snpay(msg: str) -> None:
    from app.payments.zpay import parse_query_response

    q = parse_query_response({"code": -1, "msg": msg})
    assert q.found is False and q.paid is False


def test_other_refusals_are_not_read_as_not_found() -> None:
    from app.payments.zpay import ZPayQueryError, parse_query_response

    with pytest.raises(ZPayQueryError):
        parse_query_response({"code": -1, "msg": "签名错误"})
