"""issue #27 (review-v12 §20): plan x region variants, region list, generator, half-price check."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import text

from tests.test_procurement_direct import DUp, _vendor
from tests.test_procurement_vcc import _rows, _zovo
from tests.test_variants import add_variant

API = "/api/v1"


async def test_variant_options_validation_and_buyer_two_selector_view(env):
    admin, _ = await env.login("admin")
    p = await env.product(mode="manual", price=1990, floor=100)
    url = f"{API}/admin/products/{p.id}/variants"
    base = {"name_i18n": {"en": "Plus PH"}, "price": "5.00", "floor_price": "1.00"}
    for bad in (
        {"plan": "Plus Half", "region": "PH"},  # plan key charset
        {"plan": "plus", "region": "ph"},  # ISO country, upper case
        {"plan": "plus", "region": "PHL"},
        {"plan": "plus"},  # both are required
        {"plan": "plus", "region": "PH", "extra": 1},  # nothing else
    ):
        r = await admin.post(url, json={**base, "options": bad})
        assert r.status_code == 422, (bad, r.text)
    # an options variant cannot be ACTIVE without a costed source (``cost_unknown``)
    r = await admin.post(url, json={**base, "options": {"plan": "plus", "region": "PH"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "cost_unknown"
    a = await add_variant(
        admin, p, "Plus PH", status="archived", options={"plan": "plus", "region": "PH"}
    )
    assert a["options"] == {"plan": "plus", "region": "PH"}
    async with env.owner_session() as db:  # staff costed a source and activated it
        await db.execute(text("UPDATE product_variants SET status = 'active' WHERE id = :i"),
                         {"i": uuid.UUID(a["id"])})  # fmt: skip
        await db.commit()
    # one variant per (plan, region), any status
    r = await admin.post(url, json={**base, "options": {"plan": "plus", "region": "PH"}})
    assert r.status_code == 409 and r.json()["error"]["code"] == "variant_options_taken"
    # a product whose active variants all have options gets the two-selector flag
    detail = (await env.client().get(f"{API}/products/{p.id}")).json()
    assert detail["variant_options"] is True
    (v,) = detail["variants"]
    assert v["options"] == {"plan": "plus", "region": "PH"} and v["region_currency"] == "PHP"
    # a plain variant switches the product back to the existing variant chips
    b = await add_variant(admin, p, "Plain", "6.00")
    assert b["options"] is None
    # setting options on the ACTIVE plain variant is refused: no costed source
    r = await admin.patch(f"{url}/{b['id']}", json={"options": {"plan": "plus", "region": "JP"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "cost_unknown"
    detail = (await env.client().get(f"{API}/products/{p.id}")).json()
    assert detail["variant_options"] is False
    # patching: set, conflict, clear (archived: it is not sold while it is being set up)
    assert (await admin.patch(f"{url}/{b['id']}", json={"status": "archived"})).status_code == 200
    r = await admin.patch(f"{url}/{b['id']}", json={"options": {"plan": "plus", "region": "PH"}})
    assert r.status_code == 409
    r = await admin.patch(f"{url}/{b['id']}", json={"options": {"plan": "plus", "region": "JP"}})
    assert r.status_code == 200 and r.json()["options"]["region"] == "JP"
    r = await admin.patch(f"{url}/{b['id']}", json={"options": None})
    assert r.status_code == 200 and r.json()["options"] is None


async def test_variant_options_cross_tenant_is_not_found(env):
    admin, _ = await env.login("admin")
    foreign_p = await env.product(mode="manual", tenant_id=env.other_tenant_id)
    body = {"name_i18n": {"en": "x"}, "price": "5.00", "options": {"plan": "plus", "region": "PH"}}
    r = await admin.post(f"{API}/admin/products/{foreign_p.id}/variants", json=body)
    assert r.status_code == 404
    foreign, _ = await env.login("admin", tenant_id=env.other_tenant_id)
    r = await foreign.get(f"{API}/admin/products/{foreign_p.id}/variants")
    assert r.status_code in (401, 403, 404)


# --------------------------------------------------------------------------- vendor regions


OPT = f"{API}/admin/procurement/options"
GPT_PLUS = {"product": "gpt", "plan": "plus", "card_id": 4242}


async def test_region_list_is_the_vendors_filtered_by_the_allowlist(env):
    up = DUp(env)
    await up.start()
    try:
        vendor = _vendor()
        await _zovo(up, vendor, GPT_PLUS)
        admin, _ = await env.login("admin")
        # default allowlist: the five ChatGPT regions
        r = await admin.get(OPT)
        assert r.status_code == 200
        assert r.json()["region_allowlist"] == ["PH", "EG", "CL", "JP", "US"]
        assert r.json()["half_price_check"] == "vendor_preflight"
        r = await admin.get(f"{API}/admin/procurement/regions", params={"product": "gpt"})
        body = r.json()
        assert body["available"] is True
        assert {x["country"]: x["currency"] for x in body["regions"]} == {
            "PH": "PHP", "EG": "EGP", "CL": "CLP", "JP": "JPY", "US": "USD"
        }  # fmt: skip
        assert all(x["allowed"] for x in body["regions"])
        # the owner narrows the list; invalid entries are dropped, the method is validated
        r = await admin.put(OPT, json={"region_allowlist": ["PH", "jp", "XXX", "US"]})
        assert r.json()["region_allowlist"] == ["PH", "US"]
        assert (await admin.put(OPT, json={"half_price_check": "nope"})).status_code == 422
        r = await admin.get(f"{API}/admin/procurement/regions", params={"product": "gpt"})
        allowed = {x["country"] for x in r.json()["regions"] if x["allowed"]}
        assert allowed == {"PH", "US"}
        # an audit row records who changed what
        assert (
            await env.scalar(
                "SELECT count(*) FROM audit_log WHERE action='procurement.options_updated'"
            )
            == 1
        )
        # an unknown product is refused; a reseller-less buyer cannot read or change anything
        assert (await admin.get(f"{API}/admin/procurement/regions?product=x")).status_code == 422
        buyer, _ = await env.login("buyer")
        assert (await buyer.get(OPT)).status_code in (401, 403)
        assert (await buyer.put(OPT, json={"half_price_check": "manual"})).status_code in (401, 403)
    finally:
        await up.close()


async def test_region_the_vendor_stops_offering_is_unavailable_never_forced(env):
    import app.devtools.mock_zovocard as mz
    from app.suppliers.base import OfferSpec

    up = DUp(env)
    await up.start()
    try:
        sid, _oid = await _zovo(up, _vendor(), GPT_PLUS)
        supplier = up.mocks[sid]
        eg_cdk = OfferSpec("cdk", {"plan": "plus", "payment_country": "EG"})
        eg_direct = OfferSpec(  # ADR-048: a non-USD plan needs its configured USD cost
            "direct",
            {**GPT_PLUS, "payment_country": "EG", "payment_currency": "EGP"},
            cost_usd=Decimal("12"),
        )
        assert (await supplier.availability(eg_cdk, fresh=True)).available is True
        assert (await supplier.availability(eg_direct, fresh=True)).available is True
        old = mz.MOCK_GPT_REGIONS
        mz.MOCK_GPT_REGIONS = tuple(x for x in old if x[0] != "EG")
        try:
            for spec in (eg_cdk, eg_direct):
                av = await supplier.availability(spec, fresh=True)
                assert av.available is False and av.reason == "region_not_offered"
            # a region that is still offered is unaffected
            ph = OfferSpec("cdk", {"plan": "plus", "payment_country": "PH"})
            assert (await supplier.availability(ph, fresh=True)).available is True
        finally:
            mz.MOCK_GPT_REGIONS = old
    finally:
        await up.close()


# --------------------------------------------------------------------------- generator
GEN = f"{API}/admin/procurement/generate-variants"


async def _gen_setup(env, mode="upstream_cdk"):
    up = DUp(env)
    up.mode, up.kind = mode, ("cdk" if mode == "upstream_cdk" else "direct")
    await up.start()
    await _zovo(up, _vendor(), GPT_PLUS if mode == "upstream_direct" else {"plan": "plus"})
    sid = await env.scalar("SELECT id FROM suppliers LIMIT 1")
    # the base source of the product itself is irrelevant for the variants
    return up, sid


async def test_generator_creates_plan_x_region_variants_idempotently(env):
    up, sid = await _gen_setup(env)
    try:
        admin, _ = await env.login("admin")
        body = {
            "product_id": str(up.product.id),
            "supplier_id": str(sid),
            "plans": ["plus", "plus_half"],
            "regions": ["PH", "JP", "EG", "FR", "ZZ"],  # FR not allowed, ZZ not allowed
        }
        await admin.put(OPT, json={"region_allowlist": ["PH", "JP", "EG", "CL", "US", "FR"]})
        # FR is allowed by the owner but the vendor does not offer it
        r = await admin.post(GEN, json=body)
        assert r.status_code == 200, r.text
        out = r.json()
        assert len(out["created"]) == 6 and out["kept"] == []
        assert {(s["region"], s["reason"]) for s in out["skipped"]} == {
            ("FR", "not_offered"), ("ZZ", "not_allowed")
        }  # fmt: skip
        vs = (await admin.get(f"{API}/admin/products/{up.product.id}/variants")).json()["items"]
        assert len(vs) == 6
        assert all(v["status"] == "archived" and v["price"] == "50.00" for v in vs)
        assert {(v["options"]["plan"], v["options"]["region"]) for v in vs} == {
            (p, r) for p in ("plus", "plus_half") for r in ("PH", "JP", "EG")
        }
        offers = await env.scalar(
            "SELECT count(*) FROM supplier_offers WHERE variant_id IS NOT NULL AND enabled = false "
            "AND params->>'payment_country' IS NOT NULL AND kind = 'cdk'"
        )
        assert offers == 6  # one disabled source each, nothing switched on by itself
        # idempotent: a second run keeps everything and creates nothing
        r = await admin.post(GEN, json=body)
        assert r.json()["created"] == [] and len(r.json()["kept"]) == 6
        assert await env.scalar("SELECT count(*) FROM product_variants") == 6
        # a new region only adds the missing combinations
        r = await admin.post(GEN, json={**body, "regions": ["PH", "CL"]})
        assert len(r.json()["created"]) == 2 and len(r.json()["kept"]) == 2
        assert (
            await env.scalar("SELECT count(*) FROM audit_log WHERE action='variant.generated'") == 8
        )
    finally:
        await up.close()


async def test_generator_direct_needs_a_card_and_is_platform_admin_and_tenant_scoped(env):
    up, sid = await _gen_setup(env, "upstream_direct")
    try:
        admin, _ = await env.login("admin")
        body = {
            "product_id": str(up.product.id), "supplier_id": str(sid),
            "vendor_product": "gpt", "plans": ["plus"], "regions": ["PH"],
        }  # fmt: skip
        r = await admin.post(GEN, json=body)
        assert r.status_code == 422 and r.json()["error"]["code"] == "card_required"
        r = await admin.post(GEN, json={**body, "card_id": 4242})
        assert r.status_code == 200 and len(r.json()["created"]) == 1
        params = await env.scalar("SELECT params FROM supplier_offers WHERE variant_id IS NOT NULL")
        assert params == {
            "plan": "plus", "product": "gpt", "card_id": 4242,
            "payment_country": "PH", "payment_currency": "PHP",
        }  # fmt: skip
        # nobody but platform admins; no foreign product / supplier
        buyer, _ = await env.login("buyer")
        assert (await buyer.post(GEN, json={**body, "card_id": 4242})).status_code in (401, 403)
        foreign_p = await env.product(mode="upstream_direct", tenant_id=env.other_tenant_id)
        r = await admin.post(GEN, json={**body, "card_id": 4242, "product_id": str(foreign_p.id)})
        assert r.status_code == 404
        r = await admin.post(GEN, json={**body, "card_id": 4242, "supplier_id": str(uuid.uuid4())})
        assert r.status_code == 404
        manual = await env.product(mode="manual")
        r = await admin.post(GEN, json={**body, "card_id": 4242, "product_id": str(manual.id)})
        assert r.status_code == 422 and r.json()["error"]["code"] == "bad_mode"
    finally:
        await up.close()


async def test_direct_topups_are_switched_off_by_product_status_not_by_code(env):
    """A CDK-only shop is configuration: an unpublished ``upstream_direct`` product is neither
    listed nor buyable, the CDK product next to it is unaffected."""
    up, _sid = await _gen_setup(env, "upstream_direct")
    try:
        cdk = await env.product(mode="upstream_cdk", title="CDK", price=900, floor=100)
        buyer, _ = await env.login("buyer")
        ids = {i["id"] for i in (await buyer.get(f"{API}/products")).json()["items"]}
        assert str(up.product.id) in ids and str(cdk.id) in ids
        async with env.owner_session() as db:
            from sqlalchemy import text

            await db.execute(
                text("UPDATE products SET status = 'draft' WHERE id = :i"), {"i": up.product.id}
            )
            await db.commit()
        ids = {i["id"] for i in (await buyer.get(f"{API}/products")).json()["items"]}
        assert str(up.product.id) not in ids and str(cdk.id) in ids
        r = await buyer.post(
            f"{API}/orders", json={"product_id": str(up.product.id), "qty": 1, "session": "x" * 20}
        )
        assert r.status_code == 404
    finally:
        await up.close()


async def test_generated_variants_cannot_be_activated_at_the_placeholder_price(env):
    up, sid = await _gen_setup(env)
    try:
        admin, _ = await env.login("admin")
        r = await admin.put(OPT, json={"region_allowlist": ["PH", "US"]})
        assert r.status_code == 200
        r = await admin.post(
            GEN,
            json={
                "product_id": str(up.product.id),
                "supplier_id": str(sid),
                "plans": ["plus"],
                "regions": ["PH", "US"],
                "cost_estimate": "9",
                "cost_estimates": {"PH": "6.5"},  # a PH plan costs less than the default
            },
        )
        assert r.status_code == 200, r.text
        costs = {
            row[0]: str(row[1])
            for row in await _rows(
                env,
                "SELECT params->>'payment_country', cost_estimate::numeric(20,2) "
                "FROM supplier_offers WHERE variant_id IS NOT NULL",
            )
        }
        assert costs == {"PH": "6.50", "US": "9.00"}
        vs = (await admin.get(f"{API}/admin/products/{up.product.id}/variants")).json()["items"]
        ph = next(v for v in vs if v["options"]["region"] == "PH")
        url = f"{API}/admin/products/{up.product.id}/variants/{ph['id']}"
        assert ph["status"] == "archived"
        # no price set on purpose: refused, whatever the placeholder says
        r = await admin.patch(url, json={"status": "active"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "variant_unpriced"
        # a price needs the FX rate to be compared with the cost estimate
        r = await admin.patch(url, json={"status": "active", "price": "30.00"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "fx_rate_missing"
        r = await admin.put(
            f"{API}/admin/procurement/pricing/settings",
            json={"fx": {"currency": "CNY", "per_usd": "7.2"}},
        )
        assert r.status_code == 200, r.text
        # 6.50 USD * 7.2 = 46.80 CNY: 30.00 is below cost, 50.00 covers it
        r = await admin.patch(url, json={"status": "active", "price": "30.00"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "price_below_cost"
        # the price covers it but the FLOOR (quotes use it) is still 0, below cost: refused too
        r = await admin.patch(url, json={"status": "active", "price": "50.00"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "price_below_cost"
        r = await admin.patch(
            url, json={"status": "active", "price": "50.00", "floor_price": "46.80"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "active" and r.json()["price"] == "50.00"
        assert (
            await env.scalar(
                "SELECT price_set FROM product_variants WHERE id = :i", i=uuid.UUID(ph["id"])
            )
            is True
        )
    finally:
        await up.close()


async def test_generated_variants_get_the_converted_cost_as_floor_and_guards_hold(env):
    """A generated variant's floor is its cost estimate converted at the staff FX rate (rounded
    up), so a quote cannot go below cost; creating an ACTIVE variant runs the same guard; enabling
    a source or raising its cost under an active variant is refused while price / floor is below."""
    up, sid = await _gen_setup(env)
    try:
        admin, _ = await env.login("admin")
        assert (await admin.put(OPT, json={"region_allowlist": ["PH", "US"]})).status_code == 200
        r = await admin.put(
            f"{API}/admin/procurement/pricing/settings",
            json={"fx": {"currency": "CNY", "per_usd": "7.2"}},
        )
        assert r.status_code == 200, r.text
        r = await admin.post(
            GEN,
            json={
                "product_id": str(up.product.id), "supplier_id": str(sid), "plans": ["plus"],
                "regions": ["PH"], "cost_estimate": "6.5",
            },
        )  # fmt: skip
        assert r.status_code == 200, r.text
        (row,) = await _rows(env, "SELECT id, floor_price_minor FROM product_variants")
        assert row[1] == 4680  # 6.5 USD * 7.2 = 46.80 CNY
        vurl = f"{API}/admin/products/{up.product.id}/variants"
        url = f"{vurl}/{row[0]}"
        r = await admin.patch(url, json={"status": "active", "price": "50.00"})
        assert r.status_code == 200, r.text  # price 50.00 and the generated floor 46.80 cover it
        # creating an ACTIVE variant with options is the same guard: no costed source yet
        r = await admin.post(
            vurl,
            json={"name_i18n": {"en": "EG"}, "price": "99.00", "floor_price": "99.00",
                  "options": {"plan": "plus", "region": "EG"}},
        )  # fmt: skip
        assert r.status_code == 422 and r.json()["error"]["code"] == "cost_unknown"
        # raising the cost of the (enabled) source above the active price is refused
        src = f"{API}/admin/procurement/sources"
        (oid,) = (
            r[0]
            for r in await _rows(env, "SELECT id FROM supplier_offers WHERE variant_id IS NOT NULL")
        )
        r = await admin.patch(f"{src}/{oid}", json={"enabled": True})
        assert r.status_code == 200, r.text
        r = await admin.patch(f"{src}/{oid}", json={"cost_estimate": "9"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "price_below_cost"
        # a foreign tenant cannot touch it
        foreign, _ = await env.login("admin", tenant_id=env.other_tenant_id)
        r = await foreign.patch(f"{src}/{oid}", json={"cost_estimate": "1"})
        assert r.status_code in (401, 403, 404)
    finally:
        await up.close()


async def test_options_cannot_change_under_open_orders_or_away_from_enabled_sources(env):
    from tests.helpers import buy

    admin, _ = await env.login("admin")
    buyer, _ = await env.login("buyer")
    p = await env.product(mode="manual", price=1990, floor=100)
    url = f"{API}/admin/products/{p.id}/variants"
    v = await add_variant(admin, p, "Plain", "6.00", status="archived", manual_stock=5)
    async with env.owner_session() as db:
        await db.execute(text("UPDATE product_variants SET status = 'active'"))
        await db.commit()
    order = await buy(buyer, p, 1, variant_id=v["id"])
    # a paid / unfulfilled order of the variant pins the options the engine reads live
    r = await admin.patch(f"{url}/{v['id']}", json={"options": {"plan": "plus", "region": "PH"}})
    assert r.status_code == 409 and r.json()["error"]["code"] == "variant_has_open_orders"
    async with env.owner_session() as db:
        await db.execute(text("UPDATE orders SET status = 'delivered'"))
        await db.execute(text("UPDATE product_variants SET status = 'archived'"))
        await db.commit()
    assert order["id"]
    r = await admin.patch(f"{url}/{v['id']}", json={"options": {"plan": "plus", "region": "PH"}})
    assert r.status_code == 200, r.text
    # another tenant's variant is not reachable
    foreign, _ = await env.login("admin", tenant_id=env.other_tenant_id)
    r = await foreign.patch(f"{url}/{v['id']}", json={"options": None})
    assert r.status_code in (401, 403, 404)


async def test_options_change_refused_when_an_enabled_source_would_no_longer_match(env):
    up, sid = await _gen_setup(env)
    try:
        admin, _ = await env.login("admin")
        assert (await admin.put(OPT, json={"region_allowlist": ["PH", "US"]})).status_code == 200
        r = await admin.post(
            GEN,
            json={"product_id": str(up.product.id), "supplier_id": str(sid), "plans": ["plus"],
                  "regions": ["PH"], "cost_estimate": "6.5"},
        )  # fmt: skip
        assert r.status_code == 200, r.text
        (vid,) = (r[0] for r in await _rows(env, "SELECT id FROM product_variants"))
        async with env.owner_session() as db:
            await db.execute(
                text("UPDATE supplier_offers SET enabled = true WHERE variant_id = :v"), {"v": vid}
            )
            await db.commit()
        url = f"{API}/admin/products/{up.product.id}/variants/{vid}"
        r = await admin.patch(url, json={"options": {"plan": "plus", "region": "EG"}})
        assert r.status_code == 422 and r.json()["error"]["code"] == "source_options_mismatch"
        r = await admin.patch(url, json={"options": {"plan": "plus", "region": "PH"}})
        assert r.status_code == 200, r.text  # unchanged options are no change
    finally:
        await up.close()


def test_region_offered_fails_closed_for_a_pinned_country_without_a_vendor_list():
    from app.suppliers.zovocard.supplier import PlanCatalog, region_offered

    empty = PlanCatalog("gpt", 1, regions=())
    assert region_offered(empty, "PH") is False  # pinned country, nothing to confirm it
    assert region_offered(empty, None) is True  # no pinned country: unaffected
    some = PlanCatalog("gpt", 1, regions=(("PH", "PHP"),))
    assert region_offered(some, "PH") is True and region_offered(some, "EG") is False
