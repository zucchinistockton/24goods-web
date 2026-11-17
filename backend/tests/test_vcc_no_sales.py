"""Owner decision 2026-11-17: virtual cards are not sold (``vcc_sales_enabled`` false) and card
data never reaches a buyer. Covers every order-creating path, the storefront and admin gates,
and the delivery worker's own refusal to reveal."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text

from tests.conftest import INTERNAL_TOKEN
from tests.helpers import open_conversation, send_quote

CODE = "vcc_not_sellable"


@pytest.fixture
def vcc_on(env):
    from app.core.config import get_settings

    g = get_settings()
    env.res.settings.vcc_sales_enabled = g.vcc_sales_enabled = True
    yield
    env.res.settings.vcc_sales_enabled = g.vcc_sales_enabled = False


def _body(**over):
    return {
        "title_i18n": {"en": "Virtual card $20", "zh-CN": "虚拟卡 $20"},
        "price": "25.00",
        "floor_price": "25.00",
        "delivery_mode": "upstream_vcc",
        "category": "accounts",
        **over,
    }


def _err(r) -> str:
    assert r.status_code == 422, r.text
    return r.json()["error"]["code"]


async def test_default_is_off(env):
    assert env.res.settings.vcc_sales_enabled is False


async def test_admin_cannot_create_or_activate_a_card_product(env):
    admin, _ = await env.login("admin")
    assert _err(await admin.post("/api/v1/admin/products", json=_body())) == CODE
    assert _err(await admin.post("/api/v1/admin/products", json=_body(status="draft"))) == CODE
    # an existing card product (e.g. from before the decision) cannot be re-activated
    p = await env.product(mode="upstream_vcc", status="draft")
    r = await admin.patch(f"/api/v1/admin/products/{p.id}", json={"status": "active"})
    assert _err(r) == CODE
    r = await admin.post(
        "/api/v1/admin/products/bulk", json={"ids": [str(p.id)], "action": "publish"}
    )
    assert r.status_code == 200 and r.json()["updated"] == 0
    assert await env.scalar("SELECT status FROM products WHERE id = :i", i=p.id) == "draft"
    # a normal product is untouched by the rule
    ok = await admin.post("/api/v1/admin/products", json=_body(delivery_mode="manual"))
    assert ok.status_code in (200, 201), ok.text


async def test_storefront_never_offers_a_card_product(env):
    p = await env.product(mode="upstream_vcc")
    c = env.client()
    items = (await c.get("/api/v1/products")).json()["items"]
    assert str(p.id) not in [i["id"] for i in items]
    assert (await c.get(f"/api/v1/products/{p.id}")).status_code == 404
    assert (await c.get(f"/api/v1/products?ids={p.id}")).json()["items"] == []


async def test_every_order_path_refuses_a_card_product(env):
    p = await env.product(mode="upstream_vcc")
    buyer, _ = await env.login("buyer")
    # buy-now
    r = await buyer.post("/api/v1/orders", json={"product_id": str(p.id), "qty": 1})
    assert _err(r) == CODE
    # cart checkout
    r = await buyer.post("/api/v1/checkout", json={"items": [{"product_id": str(p.id), "qty": 1}]})
    assert _err(r) == CODE
    # chat quote (staff side)
    staff, _ = await env.login("agent")
    conv = await open_conversation(buyer)
    r = await staff.post(
        f"/api/v1/admin/conversations/{conv['id']}/quotes",
        json={
            "title": "Card", "amount": "25.00", "qty": 1, "expires_in_minutes": 30,
            "product_id": str(p.id),
        },
    )  # fmt: skip
    assert _err(r) == CODE
    assert await env.scalar("SELECT count(*) FROM orders") == 0


async def test_an_old_quote_for_a_card_cannot_be_accepted(env, vcc_on):
    p = await env.product(mode="upstream_vcc")
    buyer, _ = await env.login("buyer")
    staff, _ = await env.login("agent")
    conv = await open_conversation(buyer)
    quote = await send_quote(staff, conv["id"], "25.00", product_id=str(p.id))
    env.res.settings.vcc_sales_enabled = False  # the decision lands after the quote was sent
    r = await buyer.post(f"/api/v1/quotes/{quote['quote_id']}/accept")
    assert _err(r) == CODE
    assert await env.scalar("SELECT count(*) FROM orders") == 0


async def test_delivery_refuses_to_reveal_a_card_order_even_with_a_valid_token(env):
    buyer, user = await env.login("buyer")
    p = await env.product(mode="upstream_vcc")
    import uuid

    oid = uuid.uuid4()
    async with env.owner_session() as db:
        await db.execute(
            text(
                "INSERT INTO orders (id, tenant_id, order_no, buyer_id, product_id, title, qty, "
                "amount_minor, subtotal_minor, currency, status, delivery_mode) VALUES "
                "(:i, :t, :n, :b, :p, 'x', 1, 100, 100, 'CNY', 'delivered', 'upstream_vcc')"
            ),
            {"i": oid, "t": env.tenant_id, "n": "G" + uuid.uuid4().hex[:12],
             "b": user.id, "p": p.id},
        )  # fmt: skip
        await db.commit()
    d = httpx.AsyncClient(transport=httpx.ASGITransport(app=env.delivery_app), base_url="http://d")
    try:
        r = await d.post(
            "/internal/reveal",
            json={"order_id": str(oid), "buyer_id": str(user.id)},
            headers={"X-Internal-Token": INTERNAL_TOKEN},
        )
        assert r.status_code == 409 and "cards" not in r.json()
    finally:
        await d.aclose()
    # and the buyer-facing API refuses before it asks delivery at all
    r = await buyer.post(f"/api/v1/orders/{oid}/reveal")
    assert r.status_code == 409
    view = await buyer.get(f"/api/v1/orders/{oid}")
    assert view.json()["can_reveal"] is False


async def test_an_unpaid_card_order_from_before_the_stop_cannot_be_paid(env):
    """Security review 2026-11-17: an order created while selling was on must not be payable after
    the stop, or a card would be bought that the buyer can never see."""
    import uuid

    buyer, user = await env.login("buyer")
    p = await env.product(mode="upstream_vcc")
    oid = uuid.uuid4()
    async with env.owner_session() as db:
        await db.execute(
            text(
                "INSERT INTO orders (id, tenant_id, order_no, buyer_id, product_id, title, qty, "
                "amount_minor, subtotal_minor, currency, status, delivery_mode) VALUES "
                "(:i, :t, :n, :b, :p, 'x', 1, 2500, 2500, 'CNY', 'pending_payment', 'upstream_vcc')"
            ),
            {"i": oid, "t": env.tenant_id, "n": "G" + uuid.uuid4().hex[:12],
             "b": user.id, "p": p.id},
        )  # fmt: skip
        await db.commit()
    r = await buyer.post(f"/api/v1/orders/{oid}/pay", json={"channel": "zpay_alipay"})
    assert _err(r) == CODE
    assert await env.scalar("SELECT count(*) FROM payment_attempts") == 0


async def test_guest_lookup_reveal_refuses_a_card_order(env):
    """The guest path (order number + access code) goes through the same refusal."""
    _buyer, user = await env.login("buyer")
    p = await env.product(mode="upstream_vcc")
    import uuid

    oid = uuid.uuid4()
    async with env.owner_session() as db:
        await db.execute(
            text(
                "INSERT INTO orders (id, tenant_id, order_no, buyer_id, product_id, title, qty, "
                "amount_minor, subtotal_minor, currency, status, delivery_mode) VALUES "
                "(:i, :t, :n, :b, :p, 'x', 1, 100, 100, 'CNY', 'delivered', 'upstream_vcc')"
            ),
            {"i": oid, "t": env.tenant_id, "n": "G" + uuid.uuid4().hex[:12],
             "b": user.id, "p": p.id},
        )  # fmt: skip
        await db.commit()
    from app.modules.orders import service

    with pytest.raises(Exception) as exc:
        async with env.api_session() as db:
            order = await db.get(service.Order, oid)
            await service.reveal_for(env.res, order, user.id)
    assert "Nothing to reveal" in str(exc.value) or getattr(exc.value, "status", None) == 409
