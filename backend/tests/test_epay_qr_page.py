"""EPay pay page: QR only, first-party hop link, no upstream gateway host in buyer JSON."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.models import CustomDomain, Order, PaymentAttempt
from app.payments.zpay import ZPayClient
from tests.helpers import buy
from tests.test_epay_gateways_v6 import _gateway

SNPAY = "https://www.snpay.cn/pay/submit/2025112500001/"


async def _pay(env, monkeypatch, payload, kind="redirect"):
    admin, _ = await env.login("admin")
    buyer, _ = await env.login()
    await _gateway(env, admin, monkeypatch)

    async def create(self, *args):
        return {"kind": kind, "payload": payload}

    monkeypatch.setattr(ZPayClient, "create_payment", create)
    order = await buy(buyer, await env.product(mode="manual"))
    r = await buyer.post(f"/api/v1/orders/{order['id']}/pay", json={"channel": "zpay_alipay"})
    assert r.status_code == 200, r.text
    async with env.api_session() as db:
        att = await db.scalar(
            select(PaymentAttempt).where(PaymentAttempt.tenant_id == env.tenant_id)
        )
    return buyer, order, att


async def test_redirect_kind_returns_hop_qr_and_no_gateway_host(env, monkeypatch):
    buyer, order, att = await _pay(env, monkeypatch, SNPAY)
    r = await buyer.get(f"/api/v1/pay/epay/{order['order_no']}")
    j = r.json()
    hop = f"/api/v1/pay/epay/go/{att.id}"
    assert j["open_url"].endswith(hop) and j["qr_svg"].startswith("<")
    assert "snpay" not in r.text and "redirect_url" not in j and "redirect_host" not in j

    anon = env.client(csrf=False)
    g = await anon.get(hop)
    assert g.status_code == 302 and g.headers["location"] == SNPAY
    assert g.headers["cache-control"] == "no-store"
    assert g.headers["referrer-policy"] == "no-referrer"


async def test_hop_refuses_everything_else_without_leaking(env, monkeypatch):
    _buyer, order, att = await _pay(env, monkeypatch, SNPAY)
    hop = f"/api/v1/pay/epay/go/{att.id}"
    anon = env.client(csrf=False)

    async def denied(c, path):
        g = await c.get(path)
        assert g.status_code == 302 and g.headers["location"] == "/orders", g.text
        assert "snpay" not in g.text and g.headers["cache-control"] == "no-store"

    await denied(anon, f"/api/v1/pay/epay/go/{uuid.uuid4()}")  # unknown id
    # wrong tenant: the same id on the other tenant's own host
    async with env.owner_session(env.other_tenant_id) as db:
        db.add(
            CustomDomain(
                tenant_id=env.other_tenant_id,
                hostname="shop.other.test",
                verify_token="t",
                status="active",
                verified_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()
    await denied(env.client(host="shop.other.test", csrf=False), hop)

    async def set_att(**kw):
        async with env.owner_session() as db:
            await db.execute(update(PaymentAttempt).where(PaymentAttempt.id == att.id).values(**kw))
            await db.commit()

    await set_att(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    await denied(anon, hop)  # expired
    await set_att(expires_at=datetime.now(timezone.utc) + timedelta(hours=1), status="unknown")
    await denied(anon, hop)  # not ready
    await set_att(status="ready")
    assert (await anon.get(hop)).status_code == 302
    async with env.owner_session() as db:
        await db.execute(
            update(Order).where(Order.id == uuid.UUID(order["id"])).values(status="paid")
        )
        await db.commit()
    await denied(anon, hop)  # no longer pending_payment


async def test_qr_kind_unchanged_and_open_url_only_for_alipay(env, monkeypatch):
    buyer, order, _ = await _pay(env, monkeypatch, "https://qr.alipay.com/example", "qr")
    j = (await buyer.get(f"/api/v1/pay/epay/{order['order_no']}")).json()
    assert j["kind"] == "qr" and j["qr_svg"] and j["open_url"] == "https://qr.alipay.com/example"
    async with env.owner_session() as db:
        await db.execute(
            update(PaymentAttempt).values(data={"kind": "qr", "payload": "https://pay.example/q"})
        )
        await db.commit()
    j = (await buyer.get(f"/api/v1/pay/epay/{order['order_no']}")).json()
    assert j["qr_svg"] and j["open_url"] is None
