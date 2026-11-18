"""Recent purchases strip (owner request 2026-11-18): real orders delivered in the last hour,
product + option + time + masked buyer only; nothing from a reseller storefront or an archived
product, nothing older than an hour, never an order number, amount or contact."""

from __future__ import annotations

from sqlalchemy import text

from tests.helpers import buy, pay_order

URL = "/api/v1/recent-purchases"


async def _clear(env) -> None:
    keys = [k async for k in env.res.redis.scan_iter("cache:recent-purchases:*")]
    if keys:
        await env.res.redis.delete(*keys)


async def _delivered(env, product):
    buyer, user = await env.login("buyer")
    order = await buy(buyer, product, 1)
    await pay_order(env, buyer, order)
    await env.drain()
    status = (await buyer.get(f"/api/v1/orders/{order['id']}")).json()["status"]
    assert status == "delivered"
    return order, user


async def test_a_delivered_order_shows_masked_and_an_old_one_does_not(env):
    product = await env.product(title="Steam Wallet 20")
    await env.import_codes(product, ["CODE-AAAA-0001", "CODE-AAAA-0002"])
    await _clear(env)
    assert (await env.client().get(URL)).json() == {"items": []}
    order, user = await _delivered(env, product)
    await _clear(env)
    body = (await env.client().get(URL)).json()
    (item,) = body["items"]
    assert item["product_id"] == str(product.id) and item["title"] == "Steam Wallet 20"
    assert item["buyer"].endswith("***") and len(item["buyer"]) == 4
    assert item["at"].endswith("Z") and len(item["at"]) == 20  # to the second
    raw = str(body)
    for secret in (order["order_no"], order["id"], "CODE-AAAA", user.display_name):
        assert secret not in raw
    # older than an hour: gone
    async with env.owner_session() as db:
        await db.execute(text("UPDATE orders SET delivered_at = now() - interval '61 minutes'"))
        await db.commit()
    await _clear(env)
    assert (await env.client().get(URL)).json() == {"items": []}


async def test_an_archived_product_is_not_advertised(env):
    product = await env.product(title="Gone soon")
    await env.import_codes(product, ["CODE-BBBB-0001"])
    await _delivered(env, product)
    async with env.owner_session() as db:
        await db.execute(
            text("UPDATE products SET status = 'archived' WHERE id = :p"), {"p": product.id}
        )
        await db.commit()
    await _clear(env)
    assert (await env.client().get(URL)).json() == {"items": []}
