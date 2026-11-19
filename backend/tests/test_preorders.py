"""Pre-orders (owner request 2026-11-19, plan U10): window shown before paying, confirmed by the
buyer, snapshot on the order, delivery deadline / overdue filter from the window, manual only,
tenant isolation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from tests.helpers import buy, pay_order
from tests.test_commerce_v4 import checkout, line

API = "/api/v1"
BODY = {
    "title_i18n": {"en": "Made to order", "zh-CN": "定制"},
    "price": "50.00",
    "delivery_mode": "manual",
    "category": "gift_cards",
    "manual_stock": 5,
    "preorder": {"min_days": 1, "max_days": 3, "note": {"en": "Made after payment"}},
}


async def _preorder_product(env, admin, **over) -> dict:
    r = await admin.post(f"{API}/admin/products", json={**BODY, **over})
    assert r.status_code == 200, r.text
    return r.json()


class _P:  # minimal stand-in for the helpers' product argument
    def __init__(self, pid: str) -> None:
        self.id = pid


async def test_admin_sets_window_manual_only_and_audited(env):
    admin, _ = await env.login("admin")
    p = await _preorder_product(env, admin)
    assert p["preorder"]["min_days"] == 1 and p["preorder"]["max_days"] == 3
    assert p["preorder"]["note"] == {"en": "Made after payment", "zh-CN": ""}
    r = await admin.patch(
        f"{API}/admin/products/{p['id']}", json={"preorder": {"min_days": 2, "max_days": 5}}
    )
    assert r.status_code == 200 and r.json()["preorder"]["max_days"] == 5
    a = await env.scalar(
        "SELECT after->'preorder'->>'max_days' FROM audit_log WHERE action='product.updated'"
        " ORDER BY created_at DESC LIMIT 1"
    )
    assert a == "5"
    # an inverted window, an automatic product and an unknown note language are refused
    bad = await admin.patch(
        f"{API}/admin/products/{p['id']}", json={"preorder": {"min_days": 4, "max_days": 2}}
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "preorder_invalid"
    auto = await admin.post(
        f"{API}/admin/products", json={**BODY, "delivery_mode": "auto_card", "manual_stock": None}
    )
    assert auto.status_code == 422 and auto.json()["error"]["code"] == "preorder_manual_only"
    r = await admin.patch(
        f"{API}/admin/products/{p['id']}",
        json={"preorder": {"min_days": 1, "max_days": 2, "note": {"fr": "x"}}},
    )
    assert r.status_code == 422
    # explicit null: no longer a pre-order
    r = await admin.patch(f"{API}/admin/products/{p['id']}", json={"preorder": None})
    assert r.json()["preorder"] is None


async def test_buyer_sees_window_and_must_confirm_it(env):
    admin, _ = await env.login("admin")
    p = await _preorder_product(env, admin)
    pub = (await env.client().get(f"{API}/products/{p['id']}")).json()
    assert pub["preorder"] == {
        "min_days": 1,
        "max_days": 3,
        "ends_at": None,
        "note": "Made after payment",
    }
    listed = (await env.client().get(f"{API}/products")).json()["items"]
    assert next(i for i in listed if i["id"] == p["id"])["preorder"]["max_days"] == 3
    b, _ = await env.login("buyer")
    r = await b.post(f"{API}/orders", json={"product_id": p["id"], "qty": 1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "preorder_ack_required"
    o = await buy(b, _P(p["id"]), preorder_ack=True)
    assert o["preorder"] == {
        "min_days": 1,
        "max_days": 3,
        "expected_from": None,
        "expected_to": None,
    }
    # the sold window is a snapshot: a later edit of the product does not change it
    await admin.patch(
        f"{API}/admin/products/{p['id']}", json={"preorder": {"min_days": 5, "max_days": 9}}
    )
    snap = await env.scalar(
        "SELECT (terms->'preorder'->>'min_days') || '-' || (terms->'preorder'->>'max_days')"
        " FROM orders WHERE id=:i",
        i=o["id"],
    )
    assert snap == "1-3"


async def test_paid_preorder_shows_dates_and_deadline_from_window(env):
    admin, _ = await env.login("admin")
    p = await _preorder_product(env, admin)
    b, _ = await env.login("buyer")
    o = await buy(b, _P(p["id"]), preorder_ack=True)
    await pay_order(env, b, o)
    view = (await b.get(f"{API}/orders/{o['id']}")).json()
    paid = datetime.fromisoformat(view["paid_at"].replace("Z", "+00:00"))
    exp_to = datetime.fromisoformat(view["preorder"]["expected_to"].replace("Z", "+00:00"))
    exp_from = datetime.fromisoformat(view["preorder"]["expected_from"].replace("Z", "+00:00"))
    assert exp_from - paid == timedelta(days=1) and exp_to - paid == timedelta(days=3)
    assert view["delivery_eta_hours"] == 72
    deadline = datetime.fromisoformat(view["delivery_deadline"].replace("Z", "+00:00"))
    assert deadline == exp_to


async def test_overdue_filter_uses_the_window(env):
    admin, _ = await env.login("admin")
    p = await _preorder_product(env, admin)
    b, _ = await env.login("buyer")
    o = await buy(b, _P(p["id"]), preorder_ack=True)
    await pay_order(env, b, o)

    async def paid_days_ago(days: int) -> None:
        async with env.owner_session() as db:
            await db.execute(
                text("UPDATE orders SET paid_at = :t WHERE id = :i"),
                {"t": datetime.now(UTC) - timedelta(days=days), "i": o["id"]},
            )
            await db.commit()

    async def overdue() -> list[str]:
        r = await admin.get(f"{API}/admin/orders", params={"overdue_manual": "true"})
        assert r.status_code == 200, r.text
        return [i["id"] for i in r.json()["items"]]

    await paid_days_ago(2)  # inside the 1-3 day window (the product ETA default is 24 h)
    assert o["id"] not in await overdue()
    await paid_days_ago(4)
    assert o["id"] in await overdue()


async def test_cart_checkout_needs_the_confirmation(env):
    admin, _ = await env.login("admin")
    p = await _preorder_product(env, admin)
    b, _ = await env.login("buyer")
    q = await b.post(f"{API}/cart/quote", json={"items": [line(_P(p["id"]))]})
    assert q.status_code == 200, q.text
    assert q.json()["lines"][0]["preorder"]["max_days"] == 3
    r = await checkout(b, [line(_P(p["id"]))])
    assert r.status_code == 422 and r.json()["error"]["code"] == "preorder_ack_required"
    r = await checkout(b, [line(_P(p["id"]))], preorder_ack=True)
    assert r.status_code == 200, r.text


async def test_ended_window_is_a_normal_product(env):
    admin, _ = await env.login("admin")
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    p = await _preorder_product(
        env, admin, preorder={"min_days": 1, "max_days": 3, "ends_at": past}
    )
    assert (await env.client().get(f"{API}/products/{p['id']}")).json()["preorder"] is None
    b, _ = await env.login("buyer")
    o = await buy(b, _P(p["id"]))  # no confirmation needed any more
    assert o["preorder"] is None


async def test_cross_tenant_edit_refused(env):
    admin, _ = await env.login("admin")
    other = await env.product(mode="manual", tenant_id=env.other_tenant_id)
    r = await admin.patch(
        f"{API}/admin/products/{other.id}", json={"preorder": {"min_days": 1, "max_days": 3}}
    )
    assert r.status_code == 404
    assert await env.scalar("SELECT preorder FROM products WHERE id=:i", i=str(other.id)) is None
