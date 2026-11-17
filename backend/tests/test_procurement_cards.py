# ruff: noqa: E501
"""Issue #29 part 2 (review-v12 §21.8): card pool view, card rules editor, reset of failed usage
and the decline-rate monitor. Real console endpoints -> real procurement internal app -> real
ZovoCard adapter -> ``app/devtools/mock_zovocard.py``."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from sqlalchemy import text

from app.procurement import risk
from tests.test_procurement_direct import (  # noqa: F401  (fixture helpers)
    DUp,
    _vendor,
    dup,
)
from tests.test_procurement_vcc import OK_KEY, _rows, _sql, _zovo

API = "/api/v1/admin/procurement/cards"
AUTO = {"product": "claude", "plan": "pro", "card_mode": "auto_existing"}
PAN = lambda cid: f"5378{cid:012d}"  # noqa: E731  (the mock's full card number)


def _vendor2():
    v = _vendor()  # card 4242
    v.add_funding_card(4243)
    return v


async def _sqlt(env, tenant_id, sql: str, **params):
    async with env.owner_session(tenant_id) as db:
        await db.execute(text(sql), params)
        await db.commit()


async def _order(env, tenant_id) -> uuid.UUID:
    buyer = await env.user("buyer", tenant_id=tenant_id)
    p = await env.product(mode="upstream_direct", tenant_id=tenant_id)
    oid = uuid.uuid4()
    await _sqlt(
        env,
        tenant_id,
        "INSERT INTO orders (id, tenant_id, order_no, buyer_id, product_id, title, qty, "
        "amount_minor, subtotal_minor, currency, status, delivery_mode) VALUES "
        "(:i, :t, :n, :b, :p, 'x', 1, 100, 100, 'CNY', 'paid', 'upstream_direct')",
        i=oid, t=tenant_id, n="G" + uuid.uuid4().hex[:12], b=buyer.id, p=p.id,
    )  # fmt: skip
    return oid


async def _job(
    env, tenant_id, sid, *, card: str, outcome: str, kind: str = "direct", days_ago: int = 1
):
    """A direct job that reached the vendor: ``outcome`` = declined | fulfilled | flight."""
    oid = await _order(env, tenant_id)
    jid = uuid.uuid4()
    state, code = {
        "declined": ("failed", "order_declined"),
        "fulfilled": ("fulfilled", None),
        "flight": ("accepted_pending", None),
    }[outcome]
    await _sqlt(
        env,
        tenant_id,
        "INSERT INTO procurement_jobs (id, tenant_id, order_id, unit_no, kind, state, step, "
        "idempotency_key, supplier_id, card_id, upstream_ref, error_code, maybe_sent, created_at) "
        "VALUES (:j, :t, :o, 1, :kind, :st, 'submit', :k, :s, :c, :u, :e, false, "
        "now() - make_interval(days => :d))",
        j=jid, t=tenant_id, o=oid, kind=kind, st=state, k="pj:" + jid.hex, s=sid, c=card,
        u="9" + jid.hex[:6].replace("a", "1").replace("b", "2").replace("c", "3")
        .replace("d", "4").replace("e", "5").replace("f", "6"), e=code, d=days_ago,
    )  # fmt: skip
    return jid


# ----------------------------------------------------------------------------- the maths
def test_rate_is_decimal_and_empty_periods_are_none():
    assert risk.rate(0, 0) is None
    assert risk.rate(5, 0) is None
    assert risk.rate(0, 10) == Decimal("0")
    assert risk.rate(15, 100) == Decimal("0.1500")
    assert risk.rate(1, 3) == Decimal("0.3333")
    assert isinstance(risk.rate(1, 3), Decimal)


def test_thresholds_warn_at_80_percent_and_ignore_tiny_samples():
    t1, t2 = Decimal("0.15"), Decimal("0.30")
    assert risk.state_of(Decimal("0.1199"), 100, t1) == "ok"
    assert risk.state_of(Decimal("0.12"), 100, t1) == "near"  # exactly 80 %
    assert risk.state_of(Decimal("0.1499"), 100, t1) == "near"
    assert risk.state_of(Decimal("0.15"), 100, t1) == "over"
    assert risk.state_of(None, 0, t1) == "ok"
    assert risk.state_of(Decimal("1"), risk.MIN_SAMPLE - 1, t1) == "ok"  # 1 of 1 is noise
    assert risk.level_of(Decimal("0.05"), 100, t1, t2) == "ok"
    assert risk.level_of(Decimal("0.13"), 100, t1, t2) == "warning"
    assert risk.level_of(Decimal("0.16"), 100, t1, t2) == "critical"  # over tier 1
    assert risk.level_of(Decimal("0.25"), 100, t1, t2) == "critical"
    assert risk.level_of(None, 0, t1, t2) == "ok"


# ----------------------------------------------------------------------------- authorization
async def test_roles_buyer_staff_admin_and_foreign_tenant(env, dup):  # noqa: F811
    await _zovo(dup, _vendor2(), AUTO)
    buyer, _ = await env.login("buyer")
    staff, _ = await env.login("agent")
    admin, _ = await env.login("admin")
    foreign, _ = await env.login("admin", tenant_id=env.other_tenant_id)
    reads = [f"{API}/suppliers", f"{API}/pool", f"{API}/schedule?plan=pro", f"{API}/risk"]
    admin_only = [f"{API}/rules"]
    for url in reads + admin_only:
        assert (await buyer.get(url)).status_code in (401, 403), url
    for url in reads:
        assert (await staff.get(url)).status_code == 200, url
        assert (await admin.get(url)).status_code == 200, url
        assert (await foreign.get(url)).status_code in (401, 403), url  # not the platform tenant
    for url in admin_only:
        assert (await staff.get(url)).status_code == 403
        assert (await admin.get(url)).status_code == 200
        assert (await foreign.get(url)).status_code in (401, 403)
    put = {"light_max_uses": 4}
    rst = {"card_ids": [4242], "confirm": True}
    for c, code in ((buyer, (401, 403)), (staff, (403,)), (foreign, (401, 403))):
        assert (await c.put(f"{API}/rules/gpt", json=put)).status_code in code
        assert (await c.post(f"{API}/reset-failed", json=rst)).status_code in code
    assert (
        await env.scalar("SELECT count(*) FROM audit_log WHERE action LIKE 'procurement.card_%'")
        == 0
    )


async def test_internal_card_routes_need_the_token(env, dup):  # noqa: F811
    await _zovo(dup, _vendor2(), AUTO)
    for path in ("pool", "schedule", "rules", "rules-set", "reset-failed"):
        r = await env.res.procurement.post(
            f"/internal/cards/{path}", json={"tenant_id": str(env.tenant_id)},
            headers={"X-Internal-Token": "x" * 40},
        )  # fmt: skip
        assert r.status_code == 401, path


# ----------------------------------------------------------------------------- the pool view
async def test_pool_shows_our_cards_with_counts_and_never_a_card_number(env, dup, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    vendor = _vendor2()
    vendor.usage_remaining[4242] = 2
    vendor.usage_failed[4242] = 3
    vendor.usage_busy.add(4243)
    sid, _ = await _zovo(dup, vendor, AUTO)
    # a card bought for a buyer lives in the same vendor account: it is never listed
    vendor.add_funding_card(7777)
    oid = await _order(env, env.tenant_id)
    await _sql(
        env,
        "INSERT INTO procurement_jobs (tenant_id, order_id, unit_no, kind, state, step, "
        "idempotency_key, supplier_id, upstream_ref, card_mask) VALUES (:t, :o, 1, 'vcc', "
        "'fulfilled', 'submit', 'pj:buyer7777', :s, '7777', '537800****7777')",
        t=env.tenant_id, o=oid, s=sid,
    )  # fmt: skip
    for outcome in ("declined", "fulfilled", "fulfilled", "flight"):
        await _job(env, env.tenant_id, sid, card="4242", outcome=outcome)
    await _job(env, env.tenant_id, sid, card="4242", outcome="declined", days_ago=45)  # too old
    # another tenant's jobs on the same card id are never counted (cross-tenant attempt)
    await _job(env, env.other_tenant_id, None, card="4242", outcome="declined")
    staff, _ = await env.login("agent")
    r = await staff.get(f"{API}/pool", params={"product": "gpt"})
    assert r.status_code == 200, r.text
    body = r.json()
    cards = {c["id"]: c for c in body["cards"]}
    assert set(cards) == {1, 4242, 4243}  # 1 = the scenario card; 7777 is a buyer's card
    assert body["excluded_buyer_cards"] == 1
    c = cards[4242]
    assert c["last4"] == "4242" and c["status"] == "ACTIVE" and c["balance"] == "500.0"
    u = c["usage"]
    assert u["light"]["remaining"] == 2 and u["light"]["cap"] == 5 and u["pro20"]["cap"] == 3
    assert u["failure_streak"] == 3 and u["light"]["failed"] == 3
    assert cards[4243]["usage"]["busy"] is True
    assert c["ours_30d"]["sent"] == 4 and c["ours_30d"]["declined"] == 1
    assert c["ours_30d"]["in_flight"] == 1 and c["ours_30d"]["rate"] == "0.2500"
    assert body["ours_30d"]["sent"] == 4  # the foreign tenant's decline is not ours
    # no card number beyond the last four anywhere: response, logs, audit rows
    assert (
        PAN(4242) not in r.text
        and PAN(4243) not in r.text
        and "5378" not in r.text.replace("537800", "")
    )
    assert PAN(4242) not in caplog.text and PAN(4243) not in caplog.text


async def test_schedule_preview_gives_the_pick_and_every_skip_reason(env, dup):  # noqa: F811
    vendor = _vendor2()
    vendor.add_funding_card(4244)
    vendor.card_pool = [4243, 4242, 4244]
    vendor.schedule_skip = {4243: "cooling_down"}
    await _zovo(dup, vendor, AUTO)
    staff, _ = await env.login("agent")
    r = await staff.get(f"{API}/schedule", params={"product": "claude", "plan": "pro"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pick"] == 4242
    assert {c["card_id"]: c["skip_reason"] for c in body["candidates"]} == {
        4243: "cooling_down", 4244: None
    }  # fmt: skip
    assert (await staff.get(f"{API}/schedule", params={"plan": "pro; drop"})).status_code == 422
    assert (
        await staff.get(f"{API}/schedule", params={"product": "x", "plan": "p"})
    ).status_code == 422


# ----------------------------------------------------------------------------- the rules editor
async def test_rules_are_read_edited_audited_and_compared_with_the_cost_model(env, dup):  # noqa: F811
    vendor = _vendor2()
    await _zovo(dup, vendor, AUTO)
    admin, user = await env.login("admin")
    r = await admin.get(f"{API}/rules")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["rules"]) == {"gpt", "claude", "grok"}
    assert body["rules"]["gpt"]["light_max_uses"] == 5 and body["cost_model_max_auto_switches"] == 2
    r = await admin.put(
        f"{API}/rules/claude",
        json={"light_max_uses": 8, "select_mode": "lowest_usage", "max_auto_switches": 4,
              "count_failures": False},
    )  # fmt: skip
    assert r.status_code == 200, r.text
    assert r.json()["before"]["light_max_uses"] == 5 and r.json()["after"]["light_max_uses"] == 8
    saved = vendor.card_rules["claude"]
    assert saved["light_max_uses"] == 8 and saved["select_mode"] == "lowest_usage"
    assert saved["count_failures"] is False and saved["max_auto_switches"] == 4
    assert saved["product"] == "claude" and "gpt" not in vendor.card_rules
    (row,) = await _rows(
        env,
        "SELECT actor_id, severity, before, after, detail FROM audit_log "
        "WHERE action = 'procurement.card_rules_updated'",
    )
    assert row.actor_id == user.id and row.severity == "warning"
    assert row.before["light_max_uses"] == 5 and row.after["light_max_uses"] == 8
    assert row.detail["fields"] == [
        "count_failures",
        "light_max_uses",
        "max_auto_switches",
        "select_mode",
    ]
    body = (await admin.get(f"{API}/rules")).json()
    assert body["rules"]["claude"]["matches_cost_model"] is False  # vendor 4, cost model 2
    assert body["rules"]["gpt"]["matches_cost_model"] is True
    r = await admin.put(f"{API}/rules/claude", json={"max_auto_switches": 2})
    assert r.status_code == 200
    assert (await admin.get(f"{API}/rules")).json()["rules"]["claude"]["matches_cost_model"] is True


async def test_rules_edits_are_validated_server_side(env, dup):  # noqa: F811
    vendor = _vendor2()
    await _zovo(dup, vendor, AUTO)
    admin, _ = await env.login("admin")
    bad = [
        {"light_max_uses": -1}, {"light_max_uses": 1001}, {"pro20_max_uses": -5},
        {"max_auto_switches": 11}, {"max_auto_switches": -1}, {"select_mode": "random"},
        {"count_failures": "yes"}, {"light_max_uses": "5"}, {"strict_select": 1.5},
        {"surprise": True}, {},
    ]  # fmt: skip
    for patch in bad:
        r = await admin.put(f"{API}/rules/gpt", json=patch)
        assert r.status_code == 422, (patch, r.text)
    assert (await admin.put(f"{API}/rules/other", json={"light_max_uses": 3})).status_code == 422
    # zero means "unlimited" / "no switch" at the vendor and is allowed
    r = await admin.put(f"{API}/rules/gpt", json={"light_max_uses": 0, "max_auto_switches": 0})
    assert r.status_code == 200, r.text
    assert vendor.card_rules["gpt"]["light_max_uses"] == 0
    assert len(vendor.card_rules) == 1  # nothing else was written
    assert (
        await env.scalar(
            "SELECT count(*) FROM audit_log WHERE action = 'procurement.card_rules_updated'"
        )
        == 1
    )


async def test_a_production_supplier_is_never_written_to(env, dup):  # noqa: F811
    vendor = _vendor2()
    sid, _ = await _zovo(dup, vendor, AUTO)
    await _sql(env, "UPDATE suppliers SET env = 'production' WHERE id = :i", i=sid)
    assert dup.ctx.settings.procurement_allow_production_writes is False
    admin, _ = await env.login("admin")
    r = await admin.put(f"{API}/rules/gpt", json={"light_max_uses": 3})
    assert r.status_code == 409 and r.json()["error"]["code"] == "production_writes_disabled"
    r = await admin.post(f"{API}/reset-failed", json={"card_ids": [4242], "confirm": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "production_writes_disabled"
    assert vendor.card_rules == {} and vendor.usage_resets == []
    assert not [x for x in vendor.requests if x[0] != "GET"]  # not one write reached the vendor
    assert (
        await env.scalar("SELECT count(*) FROM audit_log WHERE action LIKE 'procurement.card_%'")
        == 0
    )
    assert (await admin.get(f"{API}/rules")).status_code == 200  # reads stay open
    # with the opt-in the same write goes through
    from dataclasses import replace

    dup.ctx.settings = replace(dup.ctx.settings, procurement_allow_production_writes=True)
    r = await admin.put(f"{API}/rules/gpt", json={"light_max_uses": 3})
    assert r.status_code == 200, r.text
    assert vendor.card_rules["gpt"]["light_max_uses"] == 3


# ----------------------------------------------------------------------------- reset failed usage
async def test_reset_failed_is_explicit_confirmed_audited_and_scoped(env, dup):  # noqa: F811
    vendor = _vendor2()
    vendor.usage_failed[4242] = 3
    _sid, _ = await _zovo(dup, vendor, AUTO)
    admin, user = await env.login("admin")
    ok = {"card_ids": [4242], "confirm": True}
    for bad in (
        {"card_ids": [4242]},  # no confirmation
        {"card_ids": [4242], "confirm": False},
        {"card_ids": [], "confirm": True},  # an empty list would mean EVERY card at the vendor
        {"card_ids": list(range(1, 52)), "confirm": True},
        {"card_ids": [4242], "confirm": True, "scope": "all"},  # never from the console
        {"card_ids": [0], "confirm": True},
        {"confirm": True},
    ):
        assert (await admin.post(f"{API}/reset-failed", json=bad)).status_code == 422, bad
    # a card that is not one of OUR funding cards is refused before the vendor is called
    assert (
        await admin.post(f"{API}/reset-failed", json={"card_ids": [999], "confirm": True})
    ).status_code == 422
    assert vendor.usage_resets == []
    r = await admin.post(f"{API}/reset-failed", json=ok)
    assert r.status_code == 200, r.text
    assert vendor.usage_resets == [{"card_ids": [4242], "scope": "failed"}]
    assert 4242 not in vendor.usage_failed
    (row,) = await _rows(
        env,
        "SELECT actor_id, severity, detail FROM audit_log WHERE action = 'procurement.card_usage_reset'",
    )
    assert row.actor_id == user.id and row.detail["scope"] == "failed"
    assert row.detail["card_ids"] == [4242]
    assert PAN(4242) not in str(row.detail)


async def test_a_buyers_card_cannot_be_reset_as_a_funding_card(env, dup):  # noqa: F811
    vendor = _vendor2()
    sid, _ = await _zovo(dup, vendor, AUTO)
    vendor.add_funding_card(7777)
    oid = await _order(env, env.tenant_id)
    await _sql(
        env,
        "INSERT INTO procurement_jobs (tenant_id, order_id, unit_no, kind, state, step, "
        "idempotency_key, supplier_id, upstream_ref) VALUES (:t, :o, 1, 'vcc', 'fulfilled', "
        "'submit', 'pj:buyer7777b', :s, '7777')",
        t=env.tenant_id, o=oid, s=sid,
    )  # fmt: skip
    admin, _ = await env.login("admin")
    r = await admin.post(f"{API}/reset-failed", json={"card_ids": [7777], "confirm": True})
    assert r.status_code == 422 and vendor.usage_resets == []


async def _buyer_vcc(
    env, tenant_id, *, sid, ref: str | None, mask: str | None, key: str, state="fulfilled"
):
    oid = await _order(env, tenant_id)
    await _sqlt(
        env,
        tenant_id,
        "INSERT INTO procurement_jobs (tenant_id, order_id, unit_no, kind, state, step, "
        "idempotency_key, supplier_id, upstream_ref, card_mask) VALUES (:t, :o, 1, 'vcc', :st, "
        "'submit', :k, :s, :u, :m)",
        t=tenant_id, o=oid, st=state, k=key, s=sid, u=ref, m=mask,
    )  # fmt: skip


async def test_buyer_cards_are_excluded_whatever_the_supplier_row_or_the_record(env, dup):  # noqa: F811
    vendor = _vendor2()
    for cid in (7777, 7778, 7779, 7780):
        vendor.add_funding_card(cid)
    sid, _ = await _zovo(dup, vendor, AUTO)
    sid2, _ = await _zovo(dup, vendor, AUTO, code="zovocard_b", priority=20)
    # M1: bought through the OTHER supplier row of the same vendor account
    await _buyer_vcc(env, env.tenant_id, sid=sid2, ref="7777", mask=None, key="pj:o1")
    # a hand-pasted buyer card: no vendor id, no supplier, only the mask
    await _buyer_vcc(env, env.tenant_id, sid=None, ref=None, mask="537800****7778", key="pj:o2")
    # a voided job's mask does not exclude (the card is not a live buyer card)
    await _buyer_vcc(
        env, env.tenant_id, sid=None, ref=None, mask="537800****7779", key="pj:o3", state="voided"
    )
    # ANOTHER tenant's buyer card on the same ids/masks must not exclude ours
    await _buyer_vcc(
        env, env.other_tenant_id, sid=None, ref="7780", mask="537800****7780", key="pj:o4"
    )
    staff, _ = await env.login("agent")
    r = await staff.get(f"{API}/pool", params={"product": "gpt", "supplier_id": str(sid)})
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {c["id"] for c in body["cards"]}
    assert 7777 not in ids and 7778 not in ids
    assert {4242, 4243, 7779, 7780} <= ids
    assert body["excluded_buyer_cards"] == 2
    assert all("buyer_mask" not in c for c in body["cards"])
    assert "537800" not in r.text
    admin, _ = await env.login("admin")
    for cid in (7777, 7778):
        r = await admin.post(
            f"{API}/reset-failed",
            json={"card_ids": [cid], "confirm": True, "supplier_id": str(sid)},
        )
        assert r.status_code == 422, (cid, r.text)
    assert vendor.usage_resets == []
    r = await admin.post(
        f"{API}/reset-failed", json={"card_ids": [7780], "confirm": True, "supplier_id": str(sid)}
    )
    assert r.status_code == 200, r.text  # the foreign tenant's record does not block us


async def test_a_disabled_supplier_is_not_written_to_but_can_be_read(env, dup):  # noqa: F811
    vendor = _vendor2()
    sid, _ = await _zovo(dup, vendor, AUTO)
    await _sql(env, "UPDATE suppliers SET enabled = false WHERE id = :i", i=sid)
    admin, _ = await env.login("admin")
    q = {"supplier_id": str(sid)}
    r = await admin.put(f"{API}/rules/gpt", json={"light_max_uses": 3, **q})
    assert r.status_code == 409 and r.json()["error"]["code"] == "supplier_disabled", r.text
    r = await admin.post(f"{API}/reset-failed", json={"card_ids": [4242], "confirm": True, **q})
    assert r.status_code == 409 and r.json()["error"]["code"] == "supplier_disabled", r.text
    assert not [x for x in vendor.requests if x[0] != "GET"]
    assert (await admin.get(f"{API}/rules", params=q)).status_code == 200


def test_a_sandbox_row_pointing_at_a_public_non_sandbox_host_counts_as_production():
    from app.suppliers.zovocard.client import ZovoConfig

    def cfg(url: str, env: str = "sandbox") -> ZovoConfig:
        return ZovoConfig(base_url=url, app_secret=OK_KEY, env=env)

    assert cfg("https://zovocard.com/openapi/v1").production_target  # sandbox label, prod host
    assert cfg("https://sandbox.zovocard.com.evil.example/openapi/v1").production_target
    assert cfg("https://sandbox.zovocard.com/openapi/v1", "production").production_target
    assert not cfg("https://sandbox.zovocard.com/openapi/v1").production_target
    assert not cfg("http://m/openapi/v1").production_target  # our simulator, dev only


async def test_the_client_backstop_blocks_a_mislabelled_sandbox_write():
    import pytest

    from app.suppliers.zovocard.client import (
        ProductionWriteBlocked,
        TokenBucket,
        ZovoClient,
        ZovoConfig,
    )

    client = ZovoClient(
        ZovoConfig(base_url="https://zovocard.com/openapi/v1", app_secret=OK_KEY),
        bucket=TokenBucket(6000, 100),
    )
    with pytest.raises(ProductionWriteBlocked):
        await client.request("PUT", "/gpt-direct/card-rules", json_body={}, idempotency_key="k1")


async def test_write_calls_are_rate_limited(env, dup):  # noqa: F811
    await _zovo(dup, _vendor2(), AUTO)
    admin, _ = await env.login("admin")
    seen = set()
    for _ in range(32):
        r = await admin.post(f"{API}/reset-failed", json={"card_ids": [999], "confirm": True})
        seen.add(r.status_code)
    assert 429 in seen and seen <= {422, 429}


# ----------------------------------------------------------------------------- the risk monitor
async def test_risk_monitor_rates_windows_cards_and_chargebacks(env, dup):  # noqa: F811
    sid, _ = await _zovo(dup, _vendor2(), AUTO)
    # 7 days: card 4242 -> 12 sent, 2 declined (16.7 %: over tier 1); card 4243 -> 10 sent, 0 declined
    for i in range(12):
        await _job(
            env, env.tenant_id, sid, card="4242", outcome="declined" if i < 2 else "fulfilled"
        )
    for _ in range(10):
        await _job(env, env.tenant_id, sid, card="4243", outcome="fulfilled")
    # 20 days ago: 30-day window only
    for _ in range(8):
        await _job(env, env.tenant_id, sid, card="4243", outcome="fulfilled", days_ago=20)
    # another tenant's declines must not move our numbers
    for _ in range(15):
        await _job(env, env.other_tenant_id, None, card="4242", outcome="declined")
    staff, _ = await env.login("agent")
    r = await staff.get(f"{API}/risk")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["thresholds"] == {
        "tier1": "0.15", "tier2": "0.3", "warn_at": "0.8", "min_sample": 10, "tier1_min_tx": 100
    }  # fmt: skip
    assert body["chargebacks"] == {"available": False, "reason": "not_available_from_vendor_api"}
    w7, w30 = body["windows"]["7"], body["windows"]["30"]
    assert (w7["overall"]["sent"], w7["overall"]["declined"]) == (22, 2)
    assert w7["overall"]["rate"] == "0.0909" and w7["overall"]["level"] == "ok"
    assert (w30["overall"]["sent"], w30["overall"]["declined"]) == (30, 2)
    by7 = {c["card_id"]: c for c in w7["cards"]}
    assert by7["4242"]["rate"] == "0.1667" and by7["4242"]["tier1"] == "over"
    assert by7["4242"]["tier2"] == "ok" and by7["4243"]["rate"] == "0.0000"
    assert body["level"] == "critical"  # a card passed tier 1
    # an empty period has no rate, never a division error
    await _sql(env, "DELETE FROM procurement_jobs WHERE kind = 'direct'")
    body = (await staff.get(f"{API}/risk")).json()
    assert body["windows"]["7"]["overall"]["rate"] is None and body["level"] == "ok"
    assert body["windows"]["7"]["cards"] == []


async def test_hourly_check_warns_staff_once_a_day_per_crossing(env, dup):  # noqa: F811
    from app.modules import jobs

    sid, _ = await _zovo(dup, _vendor2(), AUTO)
    for i in range(10):  # 20 % declined: over tier 1, under 80 % of tier 2 (24 %)
        await _job(
            env, env.tenant_id, sid, card="4242", outcome="declined" if i < 2 else "fulfilled"
        )
    q = "SELECT target, severity, detail FROM audit_log WHERE action = 'procurement.decline_rate_warning'"
    n = await jobs.decline_risk_check(env.res.maker)
    rows = await _rows(env, q)
    assert n == len(rows) == 4  # tier 1 over for the account and the card, in both windows
    assert {r.detail["threshold"] for r in rows} == {"tier1"}
    assert all(r.severity == "warning" and r.detail["state"] == "over" for r in rows)
    assert {r.target for r in rows} == {
        "threshold:tier1:7d:all", "threshold:tier1:30d:all",
        "threshold:tier1:7d:card:4242", "threshold:tier1:30d:card:4242",
    }  # fmt: skip
    assert await jobs.decline_risk_check(env.res.maker) == 0  # the same day: nothing new
    await _sql(
        env,
        "UPDATE audit_log SET created_at = now() - interval '2 days' WHERE action = 'procurement.decline_rate_warning'",
    )
    assert await jobs.decline_risk_check(env.res.maker) == 4  # a day later: warned again
    await _sql(env, "DELETE FROM procurement_jobs WHERE kind = 'direct'")
    await _sql(env, "DELETE FROM audit_log WHERE action = 'procurement.decline_rate_warning'")
    for _ in range(9):  # below the sample floor: never a warning
        await _job(env, env.tenant_id, sid, card="4242", outcome="declined")
    assert await jobs.decline_risk_check(env.res.maker) == 0


async def test_reseller_tenants_get_no_risk_warning(env, dup):  # noqa: F811
    from app.modules import jobs

    _sid, _ = await _zovo(dup, _vendor2(), AUTO)
    for _ in range(12):
        await _job(env, env.other_tenant_id, None, card="4242", outcome="declined")
    assert await jobs.decline_risk_check(env.res.maker) == 0
    assert (
        await env.scalar(
            "SELECT count(*) FROM audit_log WHERE action = 'procurement.decline_rate_warning'"
        )
        == 0
    )


# ----------------------------------------------------------------------------- vendor shapes
def test_parsers_drop_unknown_shapes_and_never_keep_a_card_number():
    from app.suppliers.zovocard import cardpool as cp

    rows, total = cp.parse_card_rows(
        {"total": 3, "list": [
            {"id": 1, "card_number": "5378000000004242", "status": "ACTIVE", "available_amount": 5,
             "product_code": "P1", "cvv": "123"},
            {"id": 2, "status": "DELETED"}, {"id": "x", "status": "ACTIVE"}, "junk",
        ]}
    )  # fmt: skip
    assert total == 3 and len(rows) == 1
    assert rows[0] == {"id": 1, "last4": "4242", "product_code": "P1", "status": "ACTIVE",
                       "balance": "5", "buyer_mask": False}  # fmt: skip
    assert "5378" not in str(rows) and "123" not in str(rows)
    assert cp.parse_usage("junk") == {"readable": False}
    u = cp.parse_usage({"light": {"remaining": -1, "cap": 0}, "pro20": {}, "cooling": True})
    assert u["light"]["unlimited"] is True and u["light"]["remaining"] is None and u["cooling"]
    assert cp.parse_schedule_view({"pick": 5, "candidates": [{"id": 6, "skip_reason": "a b!"}]}) == {
        "pick": 5, "candidates": [{"card_id": 6, "skip_reason": "ab"}]
    }  # fmt: skip
    assert (
        cp.parse_rules({"list": [{"product": "gpt", "select_mode": "bogus"}]})["gpt"]["select_mode"]
        is None
    )
    import pytest

    for bad in ({"light_max_uses": True}, {"select_mode": "x"}, {"x": 1}, {}):
        with pytest.raises(ValueError):
            cp.check_rule_patch(bad)
    assert cp.rules_body("gpt", {"light_max_uses": 3, "select_priority": [{"issuer": "one"}]}) == {
        "product": "gpt", "light_max_uses": 3, "select_priority": [{"issuer": "one"}]
    }  # fmt: skip
