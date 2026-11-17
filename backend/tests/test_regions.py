"""issue #27 (review-v12 §20): plan x region variants, region list, generator, half-price check."""

from __future__ import annotations

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
