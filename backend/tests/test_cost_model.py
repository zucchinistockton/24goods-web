"""Issue #28: cost model, margins and price recommendations (formulas, API, isolation, audit)."""

from __future__ import annotations

# ruff: noqa: F811  (fixtures are imported from test_procurement_m2)
import csv
import io
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core import costmodel as cm
from app.suppliers.mock import MockSupplier
from tests.test_procurement_jobs import _foreign_job
from tests.test_procurement_m2 import Up, up  # noqa: F401

PRICING = "/api/v1/admin/procurement/pricing"
D = Decimal


def st(**over):
    return cm.normalize(over)


# ---------------------------------------------------------------- pure formulas


def test_defaults_are_the_owner_schedule():
    s = cm.normalize(None)
    assert s["card_open_fee"] == "1.5" and s["topup_fee_rate"] == "0.02"
    assert s["topup_fee_fixed"] == "0" and s["failed_free_count"] == 3
    assert s["failed_fee"] == "0.3" and s["auth_reversal_rate"] == "0.01"
    assert s["service_fee_direct"] == {"gpt": "0.3", "grok": "0.15", "claude": "0"}
    assert s["uses_per_card"] == {"standard": 3, "pro20x": 3}
    assert s["chargeback_tier1_rate"] == "0.15" and s["chargeback_tier1_min_tx"] == 100
    assert s["chargeback_tier2_rate"] == "0.3" and s["chargeback_tier2_deposit"] == "200"
    assert s["target_margin"] == "0.3" and s["fx"] == {} and s["targets"] == {}


def test_normalize_drops_junk_and_never_uses_float_syntax():
    s = cm.normalize(
        {
            "card_open_fee": "1e3",
            "topup_fee_rate": "-0.1",
            "failed_fee": True,
            "failed_free_count": "3",
            "service_fee_direct": {"gpt": "NaN", "grok": "0.5", "evil": "1"},
            "uses_per_card": {"standard": 0, "pro20x": 20},
            "fx": {"CNY": {"per_usd": "0"}, "cny": {"per_usd": "7"}, "EUR": {"per_usd": "0.9"}},
            "targets": {"a": "1.5", "b": "0.2"},
        }
    )
    assert s["card_open_fee"] == "1.5" and s["topup_fee_rate"] == "0.02"
    assert s["failed_fee"] == "0.3" and s["failed_free_count"] == 3
    assert s["service_fee_direct"] == {"gpt": "0.3", "grok": "0.5", "claude": "0"}
    assert s["uses_per_card"] == {"standard": 3, "pro20x": 20}
    assert list(s["fx"]) == ["EUR"] and s["targets"] == {"b": "0.2"}


@pytest.mark.parametrize(
    "patch",
    [
        {"card_open_fee": "1e3"},
        {"card_open_fee": "-1"},
        {"topup_fee_rate": "1.5"},
        {"topup_fee_rate": "NaN"},
        {"target_margin": "1"},
        {"failed_free_count": -1},
        {"failed_free_count": True},
        {"service_fee_direct": {"bogus": "1"}},
        {"service_fee_direct": {"gpt": "x"}},
        {"uses_per_card": {"standard": 0}},
        {"round_step_minor": 0},
    ],
)
def test_validate_patch_rejects(patch):
    with pytest.raises(cm.CostSettingsError):
        cm.validate_patch(patch)


def test_set_rate_stamps_time_and_validates():
    now = datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)
    s = cm.set_rate(st(), "CNY", "7.2000", "u1", now=now)
    assert s["fx"]["CNY"] == {
        "per_usd": "7.2",
        "updated_at": now.isoformat(),
        "updated_by": "u1",
    }
    assert cm.rate_for(s, "CNY") == D("7.2") and cm.rate_for(s, "USD") == 1
    assert cm.rate_for(s, "EUR") is None
    for bad in ("0", "-1", "abc", "1e2"):
        with pytest.raises(cm.CostSettingsError):
            cm.set_rate(s, "CNY", bad, "u1")
    with pytest.raises(cm.CostSettingsError):
        cm.set_rate(s, "USD", "1", "u1")


def test_estimate_direct_every_component():
    s = st()
    src = cm.Source("direct", D("20"), "gpt")
    e = cm.estimate(s, src, D("30"), D("0.016"))
    assert e.service_fee == D("0.30")
    assert e.topup_fee == D("0.40")  # 2 % of 20, no fixed fee
    assert e.card_share == 0  # a direct source opens no card per sale
    assert e.failure_cost == D("0.05") * D("0.30")
    assert e.channel_fee == D("0.480")
    assert e.gross == D("10")
    assert e.net == D("10") - D("0.30") - D("0.40") - D("0.015") - D("0.480")
    assert e.gross_margin == D("10") / 30 and e.net_margin == e.net / 30
    assert e.incomplete == []


def test_estimate_vcc_card_share_and_tier():
    s = st(uses_per_card={"standard": 3, "pro20x": 6})
    std = cm.estimate(s, cm.Source("vcc", D("10")), D("20"), D("0"))
    assert std.card_share == D("1.5") / 3 and std.service_fee == 0
    pro = cm.estimate(s, cm.Source("vcc", D("10"), card_tier="pro20x"), D("20"), D("0"))
    assert pro.card_share == D("1.5") / 6
    assert pro.topup_fee == D("0.2") and pro.net == D("10") - pro.fees


def test_estimate_cdk_and_manual_carry_no_card_fees():
    for kind in ("cdk", "manual"):
        e = cm.estimate(st(), cm.Source(kind, D("5")), D("10"), D("0.016"))
        assert e.fees == 0 and e.channel_fee == D("0.16")
        assert e.net == D("10") - D("5") - D("0.16")


def test_fixed_fee_and_edge_values():
    s = st(topup_fee_fixed="0.5", expected_failure_rate="0", target_margin="0.3")
    e = cm.estimate(s, cm.Source("direct", D("0.1"), "claude"), D("1"), D("0"))
    assert e.topup_fee == D("0.502") and e.failure_cost == 0 and e.service_fee == 0
    free = cm.estimate(s, cm.Source("direct", D("0"), "claude"), D("1"), D("0"))
    assert free.incomplete == ["supplier_price"] and free.net is None
    zero = cm.estimate(s, cm.Source("cdk", D("1")), D("0"), D("0"))
    assert zero.gross == -1 and zero.gross_margin is None and zero.net_margin is None
    tiny = cm.estimate(st(), cm.Source("cdk", D("0.000001")), D("0.000002"), D("0.016"))
    assert tiny.gross == D("0.000001")
    loss = cm.estimate(st(), cm.Source("cdk", D("30")), D("20"), D("0.016"))
    assert loss.net < 0 and loss.gross_margin < 0


def test_incomplete_is_flagged_never_guessed():
    e = cm.estimate(st(), cm.Source("cdk", None), D("10"), D("0"))
    assert e.incomplete == ["supplier_price"] and e.gross is None and e.net is None
    e = cm.estimate(st(), cm.Source("cdk", D("1")), None, D("0"))
    assert e.incomplete == ["fx_rate"] and e.net is None and e.net_margin is None
    e = cm.estimate(st(), cm.Source("direct", D("1")), D("5"), D("0"))
    assert "direct_product" in e.incomplete
    assert cm.recommend_usd(st(), cm.Source("cdk", None), D("0"), D("0.3")) is None


def test_recommendation_hits_the_target_net_margin():
    s = st()
    src = cm.Source("direct", D("20"), "gpt")
    g, m = D("0.016"), D("0.30")
    p = cm.recommend_usd(s, src, g, m)
    assert p is not None
    e = cm.estimate(s, src, p, g)
    assert abs(e.net_margin - m) < D("0.0000001")  # exact before rounding
    minor = cm.recommend_minor(p, D("7.2"), 10)
    assert minor % 10 == 0 and minor >= int(p * D("7.2") * 100)
    back = cm.estimate(s, src, D(minor) / 100 / D("7.2"), g)
    assert back.net_margin >= m  # rounding is always up
    assert minor - int(p * D("7.2") * 100) < 10 + 1


def test_recommendation_unreachable_and_rounding_floor():
    s = st()
    assert cm.recommend_usd(s, cm.Source("cdk", D("1")), D("0.7"), D("0.3")) is None
    assert cm.recommend_usd(s, cm.Source("cdk", D("1")), D("0.5"), D("0.6")) is None
    assert cm.recommend_usd(s, cm.Source("cdk", D("0")), D("0"), D("0")) is None  # zero = unknown
    assert cm.recommend_minor(D("0"), D("7.2"), 10) == 10  # never a free product
    assert cm.recommend_minor(D("1"), D("7.2"), 100) == 800  # 7.20 -> 8.00
    assert cm.recommend_minor(D("1"), D("7.2"), 1) == 720
    assert cm.recommend_minor(D("1.0000001"), D("1"), 1) == 101  # ceil, never down


def test_target_override_lookup():
    pid, vid = uuid.uuid4(), uuid.uuid4()
    s = st(targets={str(pid): "0.4", cm.target_key(pid, vid): "0.5"})
    assert cm.target_margin(s, pid) == D("0.4")
    assert cm.target_margin(s, pid, vid) == D("0.5")
    assert cm.target_margin(s, uuid.uuid4()) == D("0.3")
    assert cm.target_margin(s, pid, uuid.uuid4()) == D("0.4")  # a variant falls back to its product


# ---------------------------------------------------------------- API helpers


async def _offer(env, product, *, cost="2.00", cur="USD", kind="cdk", params=None,
                 variant_id=None, tenant_id=None, code="cost_sup", priority=10):  # fmt: skip
    from app.models import SupplierAccount, SupplierOffer

    tid = tenant_id or env.tenant_id
    sid, oid = uuid.uuid4(), uuid.uuid4()
    async with env.owner_session(tid) as db:
        db.add(
            SupplierAccount(
                id=sid, tenant_id=tid, code=code, adapter="mock", label=code, env="sandbox",
                enabled=True, settings={},
            )
        )  # fmt: skip
        await db.flush()
        db.add(
            SupplierOffer(
                id=oid, tenant_id=tid, supplier_id=sid, product_id=product.id,
                variant_id=variant_id, kind=kind, params=params or {"plan": "plus"},
                priority=priority, enabled=True,
                cost_estimate=Decimal(cost) if cost is not None else None, currency=cur,
            )
        )  # fmt: skip
        await db.commit()
    return oid


async def _rate(admin, per_usd="7.2"):
    r = await admin.put(f"{PRICING}/settings", json={"fx": {"currency": "CNY", "per_usd": per_usd}})
    assert r.status_code == 200, r.text
    return r.json()


async def _units(admin, **params):
    r = await admin.get(f"{PRICING}/units", params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def _audit(env, action: str) -> list:
    async with env.owner_session() as db:
        rows = await db.execute(
            text("SELECT before, after, detail FROM audit_log WHERE action = :a ORDER BY id"),
            {"a": action},
        )
        return list(rows.all())


# ---------------------------------------------------------------- settings API


async def test_settings_defaults_update_audit_and_validation(env):
    admin, _ = await env.login("admin")
    g = (await admin.get(f"{PRICING}/settings")).json()
    assert g["settings"]["card_open_fee"] == "1.5" and g["currency"] == "USD"
    assert g["channel"]["id"] == "zpay_alipay" and g["channel"]["fee_rate"] == "0.016000"
    assert g["settings"]["fx"] == {}
    r = await admin.put(
        f"{PRICING}/settings",
        json={
            "card_open_fee": "2",
            "service_fee_direct": {"claude": "0.1"},
            "uses_per_card": {"pro20x": 5},
            "target_margin": "0.25",
        },
    )
    assert r.status_code == 200, r.text
    s = r.json()["settings"]
    assert s["card_open_fee"] == "2" and s["target_margin"] == "0.25"
    assert s["service_fee_direct"] == {"gpt": "0.3", "grok": "0.15", "claude": "0.1"}
    assert s["uses_per_card"] == {"standard": 3, "pro20x": 5}
    assert (await admin.get(f"{PRICING}/settings")).json()["settings"] == s  # persisted
    (row,) = await _audit(env, "pricing.settings_updated")
    before, after, detail = row
    assert before["card_open_fee"] == "1.5" and after["card_open_fee"] == "2"
    assert detail["fields"] == sorted(
        ["card_open_fee", "service_fee_direct", "uses_per_card", "target_margin"]
    )
    # an unchanged PUT writes no audit row
    await admin.put(f"{PRICING}/settings", json={"card_open_fee": "2"})
    assert len(await _audit(env, "pricing.settings_updated")) == 1
    # validation: strings that are not plain decimals, rates above 1, unknown fields
    for bad in (
        {"card_open_fee": "1e3"},
        {"card_open_fee": "-1"},
        {"topup_fee_rate": "1.5"},
        {"service_fee_direct": {"nope": "1"}},
        {"uses_per_card": {"standard": 0}},
        {"target_margin": "1"},
        {"unknown": 1},
        {"fx": {"currency": "cny", "per_usd": "7"}},
        {"fx": {"currency": "CNY", "per_usd": "0"}},
        {"fx": {"currency": "USD", "per_usd": "1"}},
    ):
        r = await admin.put(f"{PRICING}/settings", json=bad)
        assert r.status_code in (400, 422), (bad, r.status_code)
    assert len(await _audit(env, "pricing.settings_updated")) == 1


async def test_fx_rate_is_stamped_and_audited(env):
    admin, u = await env.login("admin")
    out = await _rate(admin, "7.25")
    e = out["settings"]["fx"]["CNY"]
    assert e["per_usd"] == "7.25" and e["updated_by"] == str(u.id) and e["updated_at"]
    (row,) = await _audit(env, "pricing.settings_updated")
    assert row[1]["fx"]["CNY"]["per_usd"] == "7.25" and row[0]["fx"] == {}
    assert row[2]["fields"] == ["fx"]


async def test_settings_staff_only_and_tenant_isolated(env):
    admin, _ = await env.login("admin")
    await admin.put(f"{PRICING}/settings", json={"card_open_fee": "9"})
    await _rate(admin)
    buyer, _ = await env.login("buyer")
    anon = env.client()
    for c, codes in ((buyer, (403,)), (anon, (401, 403))):
        for method, path, body in (
            ("get", "/settings", None),
            ("put", "/settings", {"card_open_fee": "1"}),
            ("put", "/target", {"product_id": str(uuid.uuid4())}),
            ("get", "/units", None),
            (
                "post",
                "/apply",
                {
                    "product_id": str(uuid.uuid4()),
                    "current_minor": 1,
                    "recommended_minor": 1,
                    "fingerprint": "0" * 16,
                },
            ),
            ("get", "/actual", None),
            ("get", "/actual.csv", None),
        ):
            r = await getattr(c, method)(PRICING + path, **({"json": body} if body else {}))
            assert r.status_code in codes, (c, path, r.status_code)
    # agents and other staff roles follow the existing cost-report rule: admin only
    for role in ("agent",):
        c, _ = await env.login(role)
        assert (await c.get(f"{PRICING}/settings")).status_code == 403
    # another tenant has its own schedule: neither side sees or overwrites the other
    async with env.owner_session(env.other_tenant_id) as db:
        await db.execute(
            text("UPDATE tenants SET settings = :s WHERE id = :i"),
            {"s": json.dumps({"cost_model": {"card_open_fee": "77"}}), "i": env.other_tenant_id},
        )
        await db.commit()
    mine = (await admin.get(f"{PRICING}/settings")).json()["settings"]
    assert mine["card_open_fee"] == "9" and mine["fx"]["CNY"]
    await admin.put(f"{PRICING}/settings", json={"card_open_fee": "8"})
    async with env.owner_session(env.other_tenant_id) as db:
        raw = await db.scalar(
            text("SELECT settings FROM tenants WHERE id = :i"), {"i": env.other_tenant_id}
        )
    assert raw == {"cost_model": {"card_open_fee": "77"}}
    # a foreign tenant's session is not accepted on this tenant's host
    other, _ = await env.login("admin", tenant_id=env.other_tenant_id)
    for path in ("/settings", "/units", "/actual"):
        assert (await other.get(PRICING + path)).status_code in (401, 403)
    assert (await other.put(f"{PRICING}/settings", json={"card_open_fee": "3"})).status_code in (
        401,
        403,
    )


# ---------------------------------------------------------------- units


async def test_unit_estimate_row_with_components_and_recommendation(env):
    admin, _ = await env.login("admin")
    await _rate(admin, "7.2")
    p = await env.product(mode="upstream_direct", price=21600, floor=0, title="GPT Plus")
    oid = await _offer(env, p, cost="20", kind="direct", params={"product": "gpt"})
    body = await _units(admin)
    (row,) = [r for r in body["items"] if r["product_id"] == str(p.id)]
    assert row["offer_id"] == str(oid) and row["region"] is None and row["variant_id"] is None
    assert row["price"] == {"minor": 21600, "amount": "216.00", "currency": "CNY",
                            "usd": "30.000000"}  # fmt: skip
    c = row["cost_usd"]
    assert c["supplier_price"] == "20.000000" and c["service_fee"] == "0.300000"
    assert c["topup_fee"] == "0.400000" and c["card_share"] == "0.000000"
    assert c["failure_cost"] == "0.015000" and c["channel_fee"] == "0.480000"
    assert c["total"] == "21.195000"
    assert row["gross_usd"] == "10.000000" and row["net_usd"] == "8.805000"
    assert row["gross_margin_pct"] == "33.33" and row["net_margin_pct"] == "29.35"
    assert row["target_margin_pct"] == "30.00" and row["cost_incomplete"] is False
    rec = row["recommended"]
    # P = (20 + .3 + .4 + .015) / (1 - .016 - .3) USD, x 7.2, rounded up to 0.10 CNY
    exact = D("20.715") / D("0.684") * D("7.2")
    assert rec["minor"] == int((exact * 100 / 10).to_integral_value(rounding="ROUND_CEILING")) * 10
    assert rec["currency"] == "CNY" and rec["diff_minor"] == rec["minor"] - 21600
    assert body["fx"]["CNY"]["per_usd"] == "7.2"


async def test_unit_without_rate_or_cost_is_incomplete(env):
    admin, _ = await env.login("admin")
    p = await env.product(price=1000, floor=0, title="No rate")
    await _offer(env, p, cost="2")
    q = await env.product(price=1000, floor=0, title="No source")  # no offer, no manual cost
    rows = {r["title"]: r for r in (await _units(admin))["items"]}
    assert rows["No rate"]["incomplete"] == ["fx_rate"] and rows["No rate"]["net_usd"] is None
    assert rows["No rate"]["recommended"] is None
    await _rate(admin)
    rows = {r["title"]: r for r in (await _units(admin))["items"]}
    assert rows["No rate"]["cost_incomplete"] is False
    assert rows["No source"]["incomplete"] == ["supplier_price"]
    assert rows["No source"]["recommended"] is None and rows["No source"]["gross_usd"] is None
    # a manual cost on the product (shop currency) is the fallback source
    async with env.owner_session() as db:
        await db.execute(text("UPDATE products SET cost_minor = 720 WHERE id = :i"), {"i": q.id})
        await db.commit()
    rows = {r["title"]: r for r in (await _units(admin))["items"]}
    assert rows["No source"]["source"]["kind"] == "manual"
    assert rows["No source"]["cost_usd"]["supplier_price"] == "1.000000"


async def test_unit_foreign_currency_offer_uses_its_own_rate(env):
    admin, _ = await env.login("admin")
    await _rate(admin, "7")
    p = await env.product(price=1000, floor=0, title="EUR offer")
    await _offer(env, p, cost="9", cur="EUR")  # no EUR rate: not convertible
    (row,) = [r for r in (await _units(admin))["items"] if r["title"] == "EUR offer"]
    assert row["incomplete"] == ["supplier_price"]
    await admin.put(f"{PRICING}/settings", json={"fx": {"currency": "EUR", "per_usd": "0.9"}})
    (row,) = [r for r in (await _units(admin))["items"] if r["title"] == "EUR offer"]
    assert row["cost_usd"]["supplier_price"] == "10.000000"


async def test_variant_rows_use_variant_sources_else_product_sources(env):
    from tests.test_procurement_sources import _variant

    admin, _ = await env.login("admin")
    await _rate(admin, "10")
    p = await env.product(price=5000, title="Variants")
    v_own = await _variant(env, p)
    v_inh = await _variant(env, p)
    await _offer(env, p, cost="1", code="prod_level")
    await _offer(env, p, cost="3", variant_id=v_own, code="var_level")
    rows = [r for r in (await _units(admin))["items"] if r["product_id"] == str(p.id)]
    by = {r["variant_id"]: r for r in rows}
    assert set(by) == {str(v_own), str(v_inh)}  # the product itself is not a sellable unit
    assert by[str(v_own)]["source"]["supplier"] == "var_level"
    assert by[str(v_own)]["cost_usd"]["supplier_price"] == "3.000000"
    assert by[str(v_inh)]["source"]["supplier"] == "prod_level"
    assert by[str(v_inh)]["price"]["minor"] == 1000


async def test_units_tenant_isolation(env):
    admin, _ = await env.login("admin")
    await _rate(admin)
    foreign = await env.product(price=1000, floor=0, title="Foreign", tenant_id=env.other_tenant_id)
    await _offer(env, foreign, cost="1", tenant_id=env.other_tenant_id, code="foreign_sup")
    mine = await env.product(price=1000, floor=0, title="Mine")
    titles = [r["title"] for r in (await _units(admin))["items"]]
    assert "Mine" in titles and "Foreign" not in titles
    # a foreign product can neither get a target nor a price applied
    r = await admin.put(f"{PRICING}/target", json={"product_id": str(foreign.id), "margin": "0.5"})
    assert r.status_code == 404
    r = await admin.post(
        f"{PRICING}/apply",
        json={
            "product_id": str(foreign.id),
            "current_minor": 1000,
            "recommended_minor": 1000,
            "fingerprint": "0" * 16,
        },
    )
    assert r.status_code == 404
    async with env.owner_session(env.other_tenant_id) as db:
        price = await db.scalar(text("SELECT price_minor FROM products WHERE id = :i"),
                                {"i": foreign.id})  # fmt: skip
    assert price == 1000
    assert mine


async def test_target_override_changes_the_recommendation_and_is_audited(env):
    admin, _ = await env.login("admin")
    await _rate(admin, "10")
    p = await env.product(price=5000, title="Target")
    await _offer(env, p, cost="2")
    first = next(r for r in (await _units(admin))["items"] if r["title"] == "Target")
    assert first["target_margin_pct"] == "30.00"
    r = await admin.put(f"{PRICING}/target", json={"product_id": str(p.id), "margin": "0.5"})
    assert r.status_code == 200 and r.json()["margin"] == "0.5"
    second = next(r for r in (await _units(admin))["items"] if r["title"] == "Target")
    assert second["target_margin_pct"] == "50.00"
    assert second["recommended"]["minor"] > first["recommended"]["minor"]
    r = await admin.put(f"{PRICING}/target", json={"product_id": str(p.id), "margin": "1.2"})
    assert r.status_code in (400, 422)
    await admin.put(f"{PRICING}/target", json={"product_id": str(p.id), "margin": None})
    third = next(r for r in (await _units(admin))["items"] if r["title"] == "Target")
    assert third["target_margin_pct"] == "30.00"
    rows = await _audit(env, "pricing.target_updated")
    assert [(a[0]["target_margin"], a[1]["target_margin"]) for a in rows] == [
        (None, "0.5"),
        ("0.5", None),
    ]


# ---------------------------------------------------------------- apply


def _body(row, **over) -> dict:
    body = {
        "product_id": row["product_id"],
        "variant_id": row["variant_id"],
        "offer_id": row["offer_id"],
        "current_minor": row["price"]["minor"],
        "recommended_minor": (row["recommended"] or {"minor": 1})["minor"],
        "fingerprint": row["fingerprint"],
    }
    body.update(over)
    return body


async def _row(admin, title):
    return next(r for r in (await _units(admin))["items"] if r["title"] == title)


async def _price(env, table, i):
    async with env.owner_session() as db:
        return await db.scalar(text(f"SELECT price_minor FROM {table} WHERE id = :i"), {"i": i})  # noqa: S608


async def test_apply_is_the_normal_audited_price_edit(env):
    admin, _ = await env.login("admin")
    await _rate(admin, "10")
    p = await env.product(price=5000, floor=100, title="Apply")
    await _offer(env, p, cost="2")
    row = await _row(admin, "Apply")
    rec = row["recommended"]
    r = await admin.post(f"{PRICING}/apply", json=_body(row))
    assert r.status_code == 200, r.text
    assert r.json()["price"]["minor"] == rec["minor"]
    assert await _price(env, "products", p.id) == rec["minor"]
    # both the pricing audit row and the standard product edit row exist
    (a,) = await _audit(env, "pricing.recommendation_applied")
    assert a[0]["price_minor"] == 5000 and a[1]["price_minor"] == rec["minor"]
    upd = await _audit(env, "product.updated")
    assert any(u[1]["price_minor"] == rec["minor"] and u[0]["price_minor"] == 5000 for u in upd)
    # the same stale view is refused, nothing changes
    r = await admin.post(f"{PRICING}/apply", json=_body(row))
    assert r.status_code == 409 and r.json()["error"]["code"] == "recommendation_changed"
    # the client never names an amount
    fresh = await _row(admin, "Apply")
    r = await admin.post(f"{PRICING}/apply", json={**_body(fresh), "price": "0.01"})
    assert r.status_code in (400, 422)


async def test_apply_refused_when_the_recommendation_changed_but_the_price_did_not(env):
    admin, _ = await env.login("admin")
    await _rate(admin, "10")
    p = await env.product(price=5000, floor=100, title="Moved")
    oid = await _offer(env, p, cost="2")
    row = await _row(admin, "Moved")
    # the supplier cost changes after the staff member loaded the table
    async with env.owner_session() as db:
        await db.execute(
            text("UPDATE supplier_offers SET cost_estimate = 3 WHERE id = :i"), {"i": oid}
        )
        await db.commit()
    r = await admin.post(f"{PRICING}/apply", json=_body(row))
    assert r.status_code == 409 and r.json()["error"]["code"] == "recommendation_changed"
    # a wrong shown recommendation or fingerprint is refused as well
    fresh = await _row(admin, "Moved")
    for bad in (
        {"recommended_minor": fresh["recommended"]["minor"] + 10},
        {"fingerprint": "f" * 16},
    ):
        r = await admin.post(f"{PRICING}/apply", json=_body(fresh, **bad))
        assert r.status_code == 409
    assert await _price(env, "products", p.id) == 5000
    # a settings change (target margin) also invalidates the shown number
    await admin.put(f"{PRICING}/settings", json={"target_margin": "0.4"})
    r = await admin.post(f"{PRICING}/apply", json=_body(fresh))
    assert r.status_code == 409
    assert not await _audit(env, "pricing.recommendation_applied")


async def test_apply_noop_writes_nothing(env):
    admin, _ = await env.login("admin")
    await _rate(admin, "10")
    p = await env.product(price=5000, floor=100, title="Noop")
    await _offer(env, p, cost="2")
    row = await _row(admin, "Noop")
    assert (await admin.post(f"{PRICING}/apply", json=_body(row))).status_code == 200
    again = await _row(admin, "Noop")
    assert again["recommended"]["diff_minor"] == 0
    r = await admin.post(f"{PRICING}/apply", json=_body(again))
    assert r.status_code == 200 and r.json()["noop"] is True and r.json()["applied"] is False
    assert len(await _audit(env, "pricing.recommendation_applied")) == 1
    assert len(await _audit(env, "product.updated")) == 1


async def test_apply_variant_and_refuses_incomplete(env):
    from tests.test_procurement_sources import _variant

    admin, _ = await env.login("admin")
    await _rate(admin, "10")
    p = await env.product(price=5000, floor=0, title="Var apply")
    vid = await _variant(env, p, floor=2000)  # the guard needs floor >= cost (2 USD x 10)
    await _offer(env, p, cost="2", variant_id=vid)
    row = await _row(admin, "Var apply")
    r = await admin.post(f"{PRICING}/apply", json=_body(row))
    assert r.status_code == 200, r.text
    v_price = await _price(env, "product_variants", vid)
    assert v_price == r.json()["price"]["minor"] and await _price(env, "products", p.id) == 5000
    assert await _audit(env, "variant.updated")
    bare = await env.product(price=1000, floor=0, title="No cost")
    row = await _row(admin, "No cost")
    r = await admin.post(f"{PRICING}/apply", json=_body(row))
    assert r.status_code == 422 and r.json()["error"]["code"] == "no_recommendation"
    assert len(await _audit(env, "pricing.recommendation_applied")) == 1
    assert bare


async def test_apply_below_floor_and_large_drop(env):
    admin, _ = await env.login("admin")
    await _rate(admin, "10")
    p = await env.product(price=50000, floor=40000, title="Floor")
    await _offer(env, p, cost="2")
    row = await _row(admin, "Floor")
    r = await admin.post(f"{PRICING}/apply", json=_body(row))
    assert r.status_code == 422 and r.json()["error"]["code"] == "below_floor"
    q = await env.product(price=50000, floor=0, title="Drop")
    await _offer(env, q, cost="2", code="drop_sup")
    row = await _row(admin, "Drop")
    assert row["large_drop"] is True
    r = await admin.post(f"{PRICING}/apply", json=_body(row))
    assert r.status_code == 422 and r.json()["error"]["code"] == "confirm_large_drop"
    assert await _price(env, "products", q.id) == 50000
    r = await admin.post(f"{PRICING}/apply", json=_body(row, confirm_large_drop=True))
    assert r.status_code == 200, r.text
    assert await _price(env, "products", q.id) == row["recommended"]["minor"]
    (a,) = await _audit(env, "pricing.recommendation_applied")
    assert a[2]["large_drop_confirmed"] is True


async def test_zero_cost_source_is_incomplete_never_free(env):
    admin, _ = await env.login("admin")
    await _rate(admin, "10")
    p = await env.product(price=5000, floor=0, title="Zero")
    await _offer(env, p, cost="0")
    row = await _row(admin, "Zero")
    assert row["incomplete"] == ["supplier_price"] and row["recommended"] is None
    assert row["net_usd"] is None and row["gross_usd"] is None
    r = await admin.post(f"{PRICING}/apply", json=_body(row))
    assert r.status_code == 422 and r.json()["error"]["code"] == "no_recommendation"
    m = await env.product(price=5000, floor=0, title="ZeroManual")
    async with env.owner_session() as db:
        await db.execute(text("UPDATE products SET cost_minor = 0 WHERE id = :i"), {"i": m.id})
        await db.commit()
    assert (await _row(admin, "ZeroManual"))["incomplete"] == ["supplier_price"]


async def test_disabled_offer_and_several_offers_need_a_choice(env):
    admin, _ = await env.login("admin")
    await _rate(admin, "10")
    p = await env.product(price=5000, floor=0, title="Multi")
    o1 = await _offer(env, p, cost="2", code="m1", priority=10)
    o2 = await _offer(env, p, cost="1.5", code="m2", priority=20)
    rows = [r for r in (await _units(admin))["items"] if r["title"] == "Multi"]
    assert len(rows) == 2
    body = _body(rows[0], offer_id=None)
    r = await admin.post(f"{PRICING}/apply", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "offer_required"
    async with env.owner_session() as db:
        await db.execute(
            text("UPDATE supplier_offers SET enabled = false WHERE id = :i"), {"i": o2}
        )
        await db.commit()
    rows = {r["offer_id"]: r for r in (await _units(admin))["items"] if r["title"] == "Multi"}
    r = await admin.post(f"{PRICING}/apply", json=_body(rows[str(o2)]))
    assert r.status_code == 422 and r.json()["error"]["code"] == "offer_disabled"
    assert await _price(env, "products", p.id) == 5000
    r = await admin.post(f"{PRICING}/apply", json=_body(rows[str(o1)]))
    assert r.status_code == 200, r.text


async def test_apply_staff_only_rate_limited_and_other_prices_untouched(env):
    admin, _ = await env.login("admin")
    await _rate(admin, "10")
    p = await env.product(price=5000, floor=0, title="Untouched")
    await _offer(env, p, cost="2")
    row = await _row(admin, "Untouched")
    for role in ("buyer", "agent"):
        c, _u = await env.login(role)
        assert (await c.post(f"{PRICING}/apply", json=_body(row))).status_code == 403
    # an open order keeps the price it was created with (quotes and orders snapshot their price)
    _buyer, u = await env.login("buyer")
    async with env.owner_session() as db:
        oid = await db.scalar(
            text(
                "INSERT INTO orders (id, tenant_id, order_no, buyer_id, product_id, title, qty, "
                "amount_minor, subtotal_minor, currency, status, delivery_mode) VALUES "
                "(gen_random_uuid(), :t, 'KEEP1', :b, :p, 'x', 1, 5000, 5000, 'CNY', "
                "'pending_payment', 'manual') RETURNING id"
            ),
            {"t": env.tenant_id, "b": u.id, "p": p.id},
        )
        await db.commit()
    assert (await admin.post(f"{PRICING}/apply", json=_body(row))).status_code == 200
    async with env.owner_session() as db:
        amount = await db.scalar(text("SELECT amount_minor FROM orders WHERE id = :i"), {"i": oid})
    assert amount == 5000  # the open order keeps its price; only the catalog price moved
    assert await _price(env, "products", p.id) != 5000
    # the rate limit (60 / hour) refuses a runaway client
    codes = set()
    for _ in range(62):
        codes.add((await admin.post(f"{PRICING}/apply", json=_body(row))).status_code)
    assert 429 in codes


async def test_concurrent_price_edit_wins_or_409_never_silently_overwritten(env):
    import asyncio

    admin, _ = await env.login("admin")
    await _rate(admin, "10")
    p = await env.product(price=5000, floor=0, title="Race")
    await _offer(env, p, cost="2")
    row = await _row(admin, "Race")
    edit = admin.patch(f"/api/v1/admin/products/{p.id}", json={"price": "40.00"})
    app_ = admin.post(f"{PRICING}/apply", json=_body(row))
    r_edit, r_apply = await asyncio.gather(edit, app_)
    assert r_edit.status_code == 200, r_edit.text
    final = await _price(env, "products", p.id)
    if r_apply.status_code == 200:
        # apply ran first (its price was the one shown); the edit then correctly came after it
        assert final == 4000
    else:
        # the edit committed first: the apply saw a changed price and refused
        assert r_apply.status_code == 409 and final == 4000
    # a stale apply after the edit is a 409, the edit survives
    r = await admin.post(f"{PRICING}/apply", json=_body(row))
    assert r.status_code == 409 and await _price(env, "products", p.id) == 4000


# ---------------------------------------------------------------- actual margins


async def _ledger(env, sid, ext, amount, *, cur="USD", job_id=None):
    async with env.owner_session() as db:
        await db.execute(
            text(
                "INSERT INTO supplier_ledger (tenant_id, supplier_id, external_id, type, amount, "
                "currency, job_id, occurred_at) VALUES (:t, :s, :e, 'gpt_direct_cdk', :a, :c, :j, "
                ":o)"
            ),
            {"t": env.tenant_id, "s": sid, "e": ext, "a": Decimal(amount), "c": cur, "j": job_id,
             "o": datetime.now(timezone.utc)},
        )  # fmt: skip
        await db.commit()


async def test_actual_margin_per_order_incomplete_until_the_ledger_matches(env, up):
    admin, _ = await env.login("admin")
    sid, _ = await up.supplier(MockSupplier("mock_a"))
    _b, order = await up.paid_order()
    await up.run()
    (job,) = await up.jobs(order["id"])
    today = datetime.now(timezone.utc).date()
    window = {"date_from": f"{today.year}-01-01", "date_to": today.isoformat()}
    r = await admin.get(f"{PRICING}/actual", params=window)
    assert r.status_code == 200, r.text
    (item,) = r.json()["items"]
    # no rate and no ledger row yet: never a guessed margin
    assert item["cost_incomplete"] is True and item["net_usd"] is None
    await _rate(admin, "10")
    (item,) = (await admin.get(f"{PRICING}/actual", params=window)).json()["items"]
    assert item["cost_incomplete"] is True  # the job has no ledger row (unreconciled)
    await _ledger(env, sid, "L1", "-1.5", job_id=job.id)
    (item,) = (await admin.get(f"{PRICING}/actual", params=window)).json()["items"]
    rev = D(item["revenue"]) / 10
    assert item["revenue_usd"] == cm.q(rev) and item["cost_usd"] == "1.500000"
    assert item["cost_incomplete"] is False
    assert D(item["gross_usd"]) == rev - D("1.5")
    assert D(item["net_usd"]) == D(item["gross_usd"]) - D(item["channel_fee_usd"])
    assert item["net_margin_pct"] is not None
    # CSV: same figures, audited, formula-safe
    r = await admin.get(f"{PRICING}/actual.csv", params=window)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][0] == "order_no" and rows[1][0] == item["order_no"]
    assert rows[1][11] == "no" and rows[1][8] == item["gross_usd"] and rows[1][12] == ""
    assert await _audit(env, "procurement.margin_exported")


async def test_actual_other_tenant_sees_nothing(env, up):
    """An order of the foreign tenant never shows in this tenant's report."""
    await up.supplier(MockSupplier("mock_a"))
    admin, _ = await env.login("admin")
    await _foreign_job(env)  # a foreign order with a job
    today = datetime.now(timezone.utc).date()
    r = await admin.get(
        f"{PRICING}/actual",
        params={"date_from": f"{today.year}-01-01", "date_to": today.isoformat()},
    )
    assert r.status_code == 200 and r.json()["total"] == 0 and r.json()["items"] == []


# ---------------------------------------------------------------- buyers never see it


async def test_no_buyer_facing_endpoint_exposes_cost_or_margin(env):
    admin, _ = await env.login("admin")
    await _rate(admin)
    p = await env.product(price=1000, floor=0, title="Public view")
    await _offer(env, p, cost="2")
    await admin.put(f"{PRICING}/target", json={"product_id": str(p.id), "margin": "0.5"})
    buyer, _ = await env.login("buyer")
    anon = env.client()
    forbidden = ("cost", "margin", "recommend", "supplier", "net_usd", "gross", "per_usd")
    for c in (buyer, anon):
        for url in ("/api/v1/products", f"/api/v1/products/{p.id}", "/api/v1/site"):
            r = await c.get(url)
            if r.status_code != 200:
                continue
            blob = json.dumps(r.json()).lower()
            assert not any(w in blob for w in forbidden), (url, blob[:300])
    # the settings blob is not part of the public site payload either
    assert "cost_model" not in json.dumps((await anon.get("/api/v1/site")).json())


async def test_cost_model_survives_site_settings_updates(env):
    admin, _ = await env.login("admin")
    await _rate(admin, "7.1")
    r = await admin.put("/api/v1/admin/site", json={"defaults": {"max_pending_orders": 3}})
    assert r.status_code == 200, r.text
    got = (await admin.get(f"{PRICING}/settings")).json()["settings"]
    assert got["fx"]["CNY"]["per_usd"] == "7.1"


# ---------------------------------------------------------------- actual: revenue rules


async def _actual_one(admin):
    today = datetime.now(timezone.utc).date()
    window = {"date_from": f"{today.year}-01-01", "date_to": today.isoformat()}
    return (await admin.get(f"{PRICING}/actual", params=window)).json()["items"]


async def _paid(env, up, **order_sets):
    admin, _ = await env.login("admin")
    sid, _ = await up.supplier(MockSupplier("mock_a"))
    _b, order = await up.paid_order()
    await up.run()
    (job,) = await up.jobs(order["id"])
    await _ledger(env, sid, "A1", "-1", job_id=job.id)
    await _rate(admin, "10")
    if order_sets:
        sets = ", ".join(f"{k} = :{k}" for k in order_sets)
        async with env.owner_session() as db:
            await db.execute(
                text(f"UPDATE orders SET {sets} WHERE id = :i"),  # noqa: S608
                {**order_sets, "i": uuid.UUID(order["id"])},
            )
            await db.commit()
    return admin, order


async def test_actual_revenue_excludes_surcharge_and_reseller_markup(env, up):
    admin, order = await _paid(env, up)
    (base,) = await _actual_one(admin)
    async with env.owner_session() as db:
        row = (
            await db.execute(
                text("SELECT amount_minor, fee_minor FROM orders WHERE id = :i"),
                {"i": uuid.UUID(order["id"])},
            )
        ).one()
    assert D(base["paid"]) == D(row.amount_minor) / 100
    assert D(base["revenue"]) == D(row.amount_minor - row.fee_minor) / 100


async def test_actual_reseller_markup_is_not_platform_revenue(env, up):
    admin, order = await _paid(env, up)
    (base,) = await _actual_one(admin)
    async with env.owner_session() as db:
        await db.execute(
            text("UPDATE orders SET reseller_markup_minor = 1000 WHERE id = :i"),
            {"i": uuid.UUID(order["id"])},
        )
        await db.commit()
    (item,) = await _actual_one(admin)
    assert D(item["revenue"]) == D(base["revenue"]) - 10
    assert D(item["paid"]) == D(base["paid"])


async def test_actual_null_channel_is_incomplete_not_zpay(env, up):
    admin, order = await _paid(env, up)
    async with env.owner_session() as db:
        await db.execute(
            text("UPDATE orders SET channel = NULL WHERE id = :i"), {"i": uuid.UUID(order["id"])}
        )
        await db.execute(
            text("UPDATE payments SET channel = channel WHERE order_id = :i"),
            {"i": uuid.UUID(order["id"])},
        )
        await db.commit()
    (item,) = await _actual_one(admin)
    # the payment row still names the channel, so the fee is known (estimated, never stored)
    assert item["fee_estimated"] is True and item["channel_fee_usd"] is not None
    async with env.owner_session() as db:
        await db.execute(
            text("DELETE FROM payments WHERE order_id = :i"), {"i": uuid.UUID(order["id"])}
        )
        await db.commit()
    (item,) = await _actual_one(admin)
    assert item["channel_fee_usd"] is None and item["net_usd"] is None
    assert "channel_unknown" in item["reasons"] and item["cost_incomplete"] is True


async def test_actual_after_sales_adjustment_flags_the_order(env, up):
    admin, order = await _paid(env, up)
    (item,) = await _actual_one(admin)
    assert item["reasons"] == [] and item["net_usd"] is not None
    async with env.owner_session() as db:
        await db.execute(
            text(
                "INSERT INTO after_sales (id, tenant_id, order_id, buyer_id, reason, description, "
                "status) SELECT gen_random_uuid(), tenant_id, id, buyer_id, 'other', 'x', "
                "'replaced' FROM orders WHERE id = :i"
            ),
            {"i": uuid.UUID(order["id"])},
        )
        await db.commit()
    (item,) = await _actual_one(admin)
    assert "after_sales_adjusted" in item["reasons"] and item["net_usd"] is None
    assert item["cost_incomplete"] is True


async def test_actual_refunded_order_is_not_a_margin_row(env, up):
    admin, order = await _paid(env, up)
    async with env.owner_session() as db:
        await db.execute(
            text("UPDATE orders SET status = 'refunded' WHERE id = :i"),
            {"i": uuid.UUID(order["id"])},
        )
        await db.commit()
    assert await _actual_one(admin) == []


async def test_actual_multi_job_order_sums_the_ledger(env, up):
    admin = (await env.login("admin"))[0]
    sid, _ = await up.supplier(MockSupplier("mock_a"))
    _b, order = await up.paid_order(2)
    await up.run()
    jobs = await up.jobs(order["id"])
    assert len(jobs) == 2
    await _rate(admin, "10")
    await _ledger(env, sid, "J1", "-1", job_id=jobs[0].id)
    (item,) = await _actual_one(admin)
    assert item["jobs"] == 2 and item["unreconciled_jobs"] == 1
    assert item["cost_incomplete"] is True and "unreconciled" in item["reasons"]
    await _ledger(env, sid, "J2", "-1.25", job_id=jobs[1].id)
    (item,) = await _actual_one(admin)
    assert item["cost_usd"] == "2.250000" and item["cost_incomplete"] is False


async def test_csv_neutralises_formulas(env, up):
    admin, order = await _paid(env, up)
    async with env.owner_session() as db:
        await db.execute(
            text("UPDATE orders SET order_no = '=1+1' WHERE id = :i"), {"i": uuid.UUID(order["id"])}
        )
        await db.commit()
    today = datetime.now(timezone.utc).date()
    r = await admin.get(
        f"{PRICING}/actual.csv",
        params={"date_from": f"{today.year}-01-01", "date_to": today.isoformat()},
    )
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[1][0] == "'=1+1"


# ---------------------------------------------------------------- FX rules


async def test_fx_rate_band_change_confirmation_and_staleness(env):
    admin, _ = await env.login("admin")
    put = lambda body: admin.put(f"{PRICING}/settings", json={"fx": body})  # noqa: E731
    # the inverted rate (USD per CNY) and absurd values are refused for CNY
    for bad in ("0.139", "2.9", "15.1", "720"):
        r = await put({"currency": "CNY", "per_usd": bad})
        assert r.status_code == 422 and r.json()["error"]["code"] == "fx_out_of_band", bad
    assert (await put({"currency": "CNY", "per_usd": "7"})).status_code == 200
    # more than 20 % away from the saved rate needs confirm
    r = await put({"currency": "CNY", "per_usd": "9"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "fx_change_confirm"
    assert (await put({"currency": "CNY", "per_usd": "9", "confirm": True})).status_code == 200
    assert (await put({"currency": "CNY", "per_usd": "9.5"})).status_code == 200  # < 20 %
    # the band is a setting
    r = await admin.put(f"{PRICING}/settings", json={"fx_bands": {"CNY": {"min": "5", "max": "9"}}})
    assert r.status_code == 200
    assert (await put({"currency": "CNY", "per_usd": "9.5"})).status_code == 422
    # a stale rate marks rows and refuses apply
    await admin.put(f"{PRICING}/settings", json={"fx_bands": {"CNY": {"min": "3", "max": "15"}}})
    await put({"currency": "CNY", "per_usd": "10", "confirm": True})
    p = await env.product(price=5000, floor=0, title="Stale")
    await _offer(env, p, cost="2")
    row = await _row(admin, "Stale")
    assert row["fx_stale"] is False
    async with env.owner_session() as db:
        await db.execute(
            text(
                "UPDATE tenants SET settings = jsonb_set(settings, "
                "'{cost_model,fx,CNY,updated_at}', to_jsonb(cast(:t as text))) WHERE id = :i"
            ),
            {"t": "2020-01-01T00:00:00+00:00", "i": env.tenant_id},
        )
        await db.commit()
    stale = await _row(admin, "Stale")
    assert stale["fx_stale"] is True
    r = await admin.post(f"{PRICING}/apply", json=_body(stale))
    assert r.status_code == 409 and r.json()["error"]["code"] == "fx_stale"
    assert await _price(env, "products", p.id) == 5000
    # re-saving the rate refreshes the stamp
    await put({"currency": "CNY", "per_usd": "10"})
    assert (await _row(admin, "Stale"))["fx_stale"] is False


def test_fx_helpers():
    s = cm.set_rate(st(), "CNY", "7.2", "u", now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert cm.fx_stale(s, "CNY", datetime(2026, 1, 9, tzinfo=timezone.utc)) is True
    assert cm.fx_stale(s, "CNY", datetime(2026, 1, 7, tzinfo=timezone.utc)) is False
    assert cm.fx_stale(s, "USD") is False and cm.fx_stale(s, "EUR") is False
    with pytest.raises(cm.CostSettingsError) as e:
        cm.set_rate(st(), "CNY", "0.14", "u")
    assert e.value.code == "fx_out_of_band"
    assert cm.set_rate(st(), "EUR", "0.01", "u")["fx"]["EUR"]  # no band configured


def test_delivery_worker_gets_only_the_deadline_settings():
    from app.delivery.worker import _DEADLINE_KEYS

    assert "cost_model" not in _DEADLINE_KEYS
    assert {"after_sales_default", "auto_confirm_hours"} <= _DEADLINE_KEYS
