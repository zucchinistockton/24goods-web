"""issue #27 (review-v12 §20): plan x region variants, region list, generator, half-price check."""

from __future__ import annotations

import uuid

from tests.test_procurement_direct import DUp, _vendor
from tests.test_procurement_vcc import _zovo
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
    a = await add_variant(admin, p, "Plus PH", options={"plan": "plus", "region": "PH"})
    assert a["options"] == {"plan": "plus", "region": "PH"}
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
    detail = (await env.client().get(f"{API}/products/{p.id}")).json()
    assert detail["variant_options"] is False
    # patching: set, conflict, clear
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
        eg_direct = OfferSpec(
            "direct", {**GPT_PLUS, "payment_country": "EG", "payment_currency": "EGP"}
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
