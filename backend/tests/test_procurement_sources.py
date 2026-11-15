"""review-v12 M2: admin API for sources per variant (offers, order, routing, availability)."""

from __future__ import annotations

# ruff: noqa: F811  (fixtures are imported from test_procurement_v12)
import uuid

from sqlalchemy import text

from tests.test_procurement_v12 import _create, proc  # noqa: F401

SRC = "/api/v1/admin/procurement/sources"
SUP = "/api/v1/admin/suppliers"


async def _variant(env, product, tenant_id: uuid.UUID | None = None) -> uuid.UUID:
    from app.models import ProductVariant

    tid = tenant_id or env.tenant_id
    vid = uuid.uuid4()
    async with env.owner_session(tid) as db:
        db.add(
            ProductVariant(
                id=vid, tenant_id=tid, product_id=product.id, name_i18n={"en": "Plus"},
                price_minor=1000, status="active",
            )
        )  # fmt: skip
        await db.commit()
    return vid


async def _foreign_supplier(env) -> uuid.UUID:
    from tests.test_procurement_v12 import _insert_supplier

    return await _insert_supplier(env, env.other_tenant_id)


def _body(product, supplier_id, **over) -> dict:
    body = {
        "product_id": str(product.id),
        "supplier_id": supplier_id,
        "params": {"plan": "plus"},
    }
    body.update(over)
    return body


async def test_sources_lifecycle_sort_routing_and_audit(env):
    admin, _ = await env.login("admin")
    product = await env.product(mode="upstream_cdk")
    vid = await _variant(env, product)
    a = (await _create(admin))["id"]
    b = (await _create(admin, code="zovocard_b"))["id"]
    # variant-level and product-level sources are separate targets
    r1 = await admin.post(SRC, json=_body(product, a, variant_id=str(vid), enabled=True))
    assert r1.status_code == 201, r1.text
    o1 = r1.json()
    assert o1["variant_id"] == str(vid) and o1["enabled"] is True
    assert o1["supplier_code"] == "zovocard" and o1["last_check_ok"] is None
    assert o1["params"] == {"plan": "plus", "product": "gpt"}
    o2 = (
        await admin.post(
            SRC,
            json=_body(product, b, variant_id=str(vid), params={"plan": "go"}, cost_estimate="3.5"),
        )
    ).json()
    assert o2["enabled"] is False and o2["cost_estimate"] == "3.5" and o2["currency"] == "USD"
    # duplicate (same supplier + same params on the same target)
    dup = await admin.post(SRC, json=_body(product, a, variant_id=str(vid)))
    assert dup.status_code == 409
    other = await admin.post(SRC, json=_body(product, a))  # product-level target: allowed
    assert other.status_code == 201

    listing = (
        await admin.get(SRC, params={"product_id": str(product.id), "variant_id": str(vid)})
    ).json()
    assert listing["routing"] == "priority"
    assert [i["id"] for i in listing["items"]] == [o1["id"], o2["id"]]
    plain = (await admin.get(SRC, params={"product_id": str(product.id)})).json()
    assert [i["id"] for i in plain["items"]] == [other.json()["id"]]

    # sort: every source exactly once
    bad = await admin.post(
        SRC + "/order",
        json={"product_id": str(product.id), "variant_id": str(vid), "ids": [o1["id"]]},
    )
    assert bad.status_code == 422
    r = await admin.post(
        SRC + "/order",
        json={"product_id": str(product.id), "variant_id": str(vid), "ids": [o2["id"], o1["id"]]},
    )
    assert r.status_code == 200
    assert [i["id"] for i in r.json()["items"]] == [o2["id"], o1["id"]]
    assert [i["priority"] for i in r.json()["items"]] == [10, 20]

    # enable / disable / edit; a params change resets the availability check
    async with env.owner_session() as db:
        await db.execute(text("UPDATE supplier_offers SET last_check_ok = true"))
        await db.commit()
    r = await admin.patch(f"{SRC}/{o2['id']}", json={"enabled": True, "priority": 5})
    assert r.status_code == 200 and r.json()["enabled"] is True and r.json()["priority"] == 5
    assert r.json()["last_check_ok"] is True
    r = await admin.patch(
        f"{SRC}/{o2['id']}", json={"params": {"plan": "plus"}, "clear_cost": True}
    )
    assert r.json()["last_check_ok"] is None and r.json()["cost_estimate"] is None
    r = await admin.patch(f"{SRC}/{o2['id']}", json={"enabled": False})
    assert r.json()["enabled"] is False

    # routing mode per target
    r = await admin.put(
        SRC + "/routing",
        json={"product_id": str(product.id), "variant_id": str(vid), "mode": "round_robin"},
    )
    assert r.status_code == 200 and r.json()["routing"] == "round_robin"
    listing = (
        await admin.get(SRC, params={"product_id": str(product.id), "variant_id": str(vid)})
    ).json()
    assert listing["routing"] == "round_robin"
    assert (await admin.get(SRC, params={"product_id": str(product.id)})).json()[
        "routing"
    ] == "priority"
    assert (
        await admin.put(SRC + "/routing", json={"product_id": str(product.id), "mode": "random"})
    ).status_code == 422

    actions = await env.scalar(
        "SELECT string_agg(DISTINCT action, ',') FROM audit_log WHERE action LIKE 'procurement.%'"
    )
    assert set(actions.split(",")) == {
        "procurement.offer_created", "procurement.offers_reordered",
        "procurement.offer_updated", "procurement.routing_changed",
    }  # fmt: skip
    # nothing secret or sealed is ever in a source view
    assert "secret" not in str(listing).lower()


async def test_source_validation(env):
    admin, _ = await env.login("admin")
    product = await env.product(mode="upstream_cdk")
    sid = (await _create(admin))["id"]
    for params in ({"plan": "Bad Plan"}, {"plan": ""}, {"plan": "plus", "product": "claude"},
                   {"plan": "plus", "payment_country": "usa"}, {}):  # fmt: skip
        r = await admin.post(SRC, json=_body(product, sid, params=params))
        assert r.status_code == 422, params
    for bad in ({"cost_estimate": "-1"}, {"cost_estimate": "1e3"}, {"currency": "usd"},
                {"priority": -1}):  # fmt: skip
        assert (await admin.post(SRC, json=_body(product, sid, **bad))).status_code == 422, bad
    # unknown keys in params are dropped by the schema, not stored
    r = await admin.post(SRC, json=_body(product, sid, params={"plan": "plus", "evil": "x"}))
    assert r.status_code == 201 and "evil" not in r.json()["params"]
    # at most 10 sources per target
    for i in range(9):
        s = (await _create(admin, code=f"zovocard_{i}"))["id"]
        assert (await admin.post(SRC, json=_body(product, s))).status_code == 201
    s = (await _create(admin, code="zovocard_x"))["id"]
    r = await admin.post(SRC, json=_body(product, s))
    assert r.status_code == 422 and r.json()["error"]["code"] == "too_many_sources"


async def test_sources_roles_and_cross_tenant_access(env, proc):
    admin, _ = await env.login("admin")
    agent, _ = await env.login("agent")
    buyer, _ = await env.login("buyer")
    product = await env.product(mode="upstream_cdk")
    sid = (await _create(admin))["id"]
    offer = (await admin.post(SRC, json=_body(product, sid))).json()
    for c in (agent, buyer):
        assert (await c.get(SRC, params={"product_id": str(product.id)})).status_code == 403
        assert (await c.post(SRC, json=_body(product, sid))).status_code == 403
        assert (await c.patch(f"{SRC}/{offer['id']}", json={"enabled": True})).status_code == 403
        assert (await c.post(f"{SRC}/{offer['id']}/check")).status_code == 403
    # another tenant's admin cannot reach, or be reached through, this tenant's rows
    foreign_admin, _ = await env.login("admin", tenant_id=env.other_tenant_id)
    assert (await foreign_admin.get(SRC, params={"product_id": str(product.id)})).status_code in (
        401,
        403,
        404,
    )
    assert (
        await foreign_admin.patch(f"{SRC}/{offer['id']}", json={"enabled": True})
    ).status_code in (401, 403, 404)
    # ... and this admin cannot touch another tenant's product, variant, supplier or offer
    foreign_product = await env.product(mode="upstream_cdk", tenant_id=env.other_tenant_id)
    foreign_variant = await _variant(env, foreign_product, env.other_tenant_id)
    foreign_supplier = await _foreign_supplier(env)
    foreign_offer = uuid.uuid4()
    async with env.owner_session(env.other_tenant_id) as db:
        await db.execute(
            text(
                "INSERT INTO supplier_offers (id, tenant_id, supplier_id, product_id, kind, params,"
                " enabled) VALUES (:o, :t, :s, :p, 'cdk', '{\"plan\": \"plus\"}', false)"
            ),
            {"o": foreign_offer, "t": env.other_tenant_id, "s": foreign_supplier,
             "p": foreign_product.id},
        )  # fmt: skip
        await db.commit()
    assert (await admin.get(SRC, params={"product_id": str(foreign_product.id)})).status_code == 404
    assert (await admin.post(SRC, json=_body(foreign_product, sid))).status_code == 404
    assert (await admin.post(SRC, json=_body(product, str(foreign_supplier)))).status_code == 404
    r = await admin.post(SRC, json=_body(product, sid, variant_id=str(foreign_variant)))
    assert r.status_code == 404  # a variant of another product (and tenant)
    assert (await admin.patch(f"{SRC}/{foreign_offer}", json={"enabled": True})).status_code == 404
    assert (await admin.post(f"{SRC}/{foreign_offer}/check")).status_code == 404
    assert (
        await admin.post(
            SRC + "/order",
            json={"product_id": str(foreign_product.id), "ids": [str(foreign_offer)]},
        )
    ).status_code == 404
    assert (
        await admin.put(
            SRC + "/routing", json={"product_id": str(foreign_product.id), "mode": "round_robin"}
        )
    ).status_code == 404
    async with env.owner_session(env.other_tenant_id) as db:
        assert await db.scalar(text("SELECT enabled FROM supplier_offers WHERE id = :o"),
                               {"o": foreign_offer}) is False  # fmt: skip
        routing = await db.scalar(
            text("SELECT procurement_routing FROM products WHERE id = :p"),
            {"p": foreign_product.id},
        )
        assert routing == "priority"


async def test_reseller_tenant_admin_cannot_manage_sources(env):
    admin, _ = await env.login("admin")
    product = await env.product(mode="upstream_cdk")
    async with env.owner_session(None) as db:
        await db.execute(
            text("UPDATE tenants SET kind = 'reseller' WHERE id = :t"), {"t": env.tenant_id}
        )
        await db.commit()
    try:
        assert (await admin.get(SRC, params={"product_id": str(product.id)})).status_code == 403
    finally:
        async with env.owner_session(None) as db:
            await db.execute(
                text("UPDATE tenants SET kind = 'platform' WHERE id = :t"), {"t": env.tenant_id}
            )
            await db.commit()


async def test_check_availability_goes_through_procurement(env, proc):
    admin, _ = await env.login("admin")
    product = await env.product(mode="upstream_cdk")
    sid = (await _create(admin))["id"]
    ok = (await admin.post(SRC, json=_body(product, sid))).json()
    r = await admin.post(f"{SRC}/{ok['id']}/check")
    assert r.status_code == 200, r.text
    assert r.json()["available"] is True and r.json()["reason"] is None
    assert await env.scalar("SELECT last_check_ok FROM supplier_offers WHERE id = :o",
                            o=uuid.UUID(ok["id"])) is True  # fmt: skip
    # a plan the catalog does not list is not available (and a disabled source can be tested)
    bad = (await admin.post(SRC, json=_body(product, sid, params={"plan": "nope"}))).json()
    r = await admin.post(f"{SRC}/{bad['id']}/check")
    assert r.status_code == 200 and r.json()["available"] is False
    assert r.json()["reason"] == "not_purchasable"
    assert await env.scalar("SELECT last_check_ok FROM supplier_offers WHERE id = :o",
                            o=uuid.UUID(bad["id"])) is False  # fmt: skip
    listing = (await admin.get(SRC, params={"product_id": str(product.id)})).json()
    assert {i["id"]: i["last_check_ok"] for i in listing["items"]} == {
        ok["id"]: True,
        bad["id"]: False,
    }
    assert (
        await env.scalar(
            "SELECT count(*) FROM audit_log WHERE action = 'procurement.offer_checked'"
        )
        == 2
    )


async def test_internal_check_offer_token_and_tenant(env, proc):
    import httpx

    from tests.test_procurement_v12 import TOKEN

    admin, _ = await env.login("admin")
    product = await env.product(mode="upstream_cdk")
    sid = (await _create(admin))["id"]
    offer = (await admin.post(SRC, json=_body(product, sid))).json()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proc.app), base_url="http://p"
    ) as c:
        body = {"tenant_id": str(env.tenant_id), "offer_id": offer["id"]}
        assert (await c.post("/internal/check-offer", json=body)).status_code == 401
        bad = await c.post("/internal/check-offer", json=body, headers={"X-Internal-Token": "x"})
        assert bad.status_code == 401
        h = {"X-Internal-Token": TOKEN}
        assert (await c.post("/internal/check-offer", json={}, headers=h)).status_code == 422
        # the offer is not reachable under another tenant id
        other = {"tenant_id": str(env.other_tenant_id), "offer_id": offer["id"]}
        assert (await c.post("/internal/check-offer", json=other, headers=h)).status_code == 404
        assert (await c.post("/internal/check-offer", json=body, headers=h)).status_code == 200
