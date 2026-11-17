"""issue #27 (review-v12 §20): plan x region variants, region list, generator, half-price check."""

from __future__ import annotations

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
