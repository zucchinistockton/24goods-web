"""Issue #29 part 1 (review-v12 §21): card modes of ``upstream_direct`` orders.

``fixed`` (today's behaviour), ``auto_existing`` (the vendor's card-pool pick, run through OUR
checks), the optional ``vendor_switch``, the parked "waiting for card" job (bounded, no vendor
spend, never a double submit), the reserved ``auto_issue`` and the switch cost in the cost rows.
Real ZovoCard adapter against ``app/devtools/mock_zovocard.py`` and the engine."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from app.core import costmodel as cm
from app.devtools.mock_zovocard import MockZovo
from tests.test_procurement_direct import (
    CLAUDE,
    _drive,
    _job,
    _order_of,
    _settle,
    _sql,
    _vendor,
    _zovo,
    claude_session,
    dup,  # noqa: F401  (fixture)
    new_marker,
)
from tests.test_procurement_vcc import _posts, _rows

AUTO = {"product": "claude", "plan": "pro", "card_mode": "auto_existing"}
PAN = lambda cid: f"5378{cid:012d}"  # noqa: E731  (the mock's full card number)
ORDERS = "/gpt-direct/orders"


def _two_cards() -> MockZovo:
    v = _vendor()  # card 4242
    v.add_funding_card(4243)
    return v


async def _audit(env, action: str):
    return await _rows(
        env, "SELECT severity, detail, before, after FROM audit_log WHERE action = :a", a=action
    )


# ------------------------------------------------------------------ fixed mode is unchanged
async def test_fixed_mode_sends_its_card_and_no_switch(env, dup):  # noqa: F811
    vendor = _vendor()
    await _zovo(dup, vendor, CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    assert vendor.direct_cards_sent == [4242] and vendor.direct_switch_seen == [True]
    assert vendor.schedule_calls == 0  # the card-pool schedule is never asked
    job = await _job(dup, order)
    assert job.state == "fulfilled" and job.card_id == "4242" and job.card_last4 == "4242"
    assert job.card_wait_since is None and job.card_candidates is None


# ------------------------------------------------------------------ auto_existing
async def test_auto_existing_uses_the_vendor_pick(env, dup):  # noqa: F811
    vendor = _two_cards()
    vendor.card_pool = [4243, 4242]  # the vendor would pick 4243
    await _zovo(dup, vendor, AUTO)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    assert vendor.direct_cards_sent == [4243] and vendor.schedule_calls >= 1
    job = await _job(dup, order)
    assert job.state == "fulfilled" and job.card_id == "4243" and job.card_last4 == "4243"
    assert job.card_candidates is None  # consumed by the lease that sent the order


async def test_a_pick_that_fails_our_checks_falls_back_to_a_candidate(env, dup):  # noqa: F811
    vendor = _two_cards()
    vendor.card_pool = [4243, 4242]
    vendor.cards[4243]["available_amount"] = 0.0  # the pick cannot pay: our balance check fails
    await _zovo(dup, vendor, AUTO)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    assert vendor.direct_cards_sent == [4242]
    assert (await _job(dup, order)).card_id == "4242"


async def test_a_candidate_with_a_skip_reason_is_never_tried(env, dup):  # noqa: F811
    vendor = _two_cards()
    vendor.card_pool = [4243, 4242]
    vendor.schedule_skip[4243] = "cooling_down"
    vendor.cards[4242]["available_amount"] = 0.0  # the only usable one is too poor
    await _zovo(dup, vendor, AUTO)
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    job = await _job(dup, order)
    assert job.error_code == "waiting_for_card" and vendor.direct_cards_sent == []


# ------------------------------------------------------------------ waiting for a card
async def test_no_pick_parks_then_a_retry_succeeds_without_a_double_submit(env, dup):  # noqa: F811
    vendor = _two_cards()
    vendor.schedule_none = True  # "no card available"
    await _zovo(dup, vendor, AUTO)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    job = await _job(dup, order)
    assert job.state == "queued" and job.error_code == "waiting_for_card"
    assert not job.maybe_sent and job.next_poll_at is not None and job.card_wait_since is not None
    assert vendor.direct_executions == 0 and _posts(vendor, ORDERS) == []  # no vendor spend
    key = job.idempotency_key
    # parking again does not write a second staff warning
    await _settle(dup, 2)
    rows = await _audit(env, "procurement.waiting_for_card")
    assert len(rows) == 1 and rows[0][0] == "warning"
    assert rows[0][1]["reason"] == "no_card_pick" and "card" not in str(rows[0][1]["order_id"])
    # the buyer sees the coarse status only
    info = (await buyer.get(f"/api/v1/orders/{order['id']}/direct")).json()
    assert info["waiting_for_supplier"] is True
    assert "card" not in str(info).lower().replace("waiting_for_supplier", "")
    # a card turns up: the same job goes on with the same key and request id
    vendor.schedule_none = False
    await _drive(env, dup, order, buyer)
    job = await _job(dup, order)
    assert job.state == "fulfilled" and job.idempotency_key == key
    assert vendor.direct_executions == 1 and len(_posts(vendor, ORDERS)) == 1
    assert _order_of(vendor)["client_request_id"] == f"pj{job.id.hex}"
    assert (await buyer.get(f"/api/v1/orders/{order['id']}/direct")).json()[
        "waiting_for_supplier"
    ] is False


async def test_the_wait_is_bounded_and_ends_in_needs_manual(env, dup):  # noqa: F811
    vendor = _two_cards()
    vendor.schedule_none = True
    await _zovo(dup, vendor, AUTO)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    assert (await _job(dup, order)).error_code == "waiting_for_card"
    await _sql(env, "UPDATE procurement_jobs SET card_wait_since = now() - interval '31 minutes'")
    await _settle(dup, 1)
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "card_wait_exhausted"
    assert vendor.direct_executions == 0 and _posts(vendor, ORDERS) == []
    assert len(await _audit(env, "procurement.waiting_for_card")) == 1  # still just one
    assert buyer is not None


async def test_all_candidates_busy_parks(env, dup):  # noqa: F811
    vendor = _two_cards()
    vendor.card_pool = [4243, 4242]
    vendor.usage_busy.update({4242, 4243})
    await _zovo(dup, vendor, AUTO)
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    job = await _job(dup, order)
    assert job.error_code == "waiting_for_card" and vendor.direct_cards_sent == []
    (row,) = await _audit(env, "procurement.waiting_for_card")
    assert row[1]["reason"] == "card_pool_busy"


async def test_one_order_in_flight_per_picked_card(env, dup):  # noqa: F811
    vendor = _vendor()
    vendor.card_pool = [4242]  # a single card in the pool
    vendor.direct_end = "running"  # the first order stays at the vendor
    await _zovo(dup, vendor, AUTO)
    _b1, o1 = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    _b2, o2 = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    j2 = await _job(dup, o2)
    assert (await _job(dup, o1)).card_id == "4242"
    assert j2.state == "queued" and j2.error_code == "waiting_for_card" and not j2.maybe_sent
    assert vendor.direct_executions == 1


async def test_a_vendor_no_card_refusal_parks_and_retries_under_the_same_key(env, dup):  # noqa: F811
    vendor = _two_cards()
    vendor.direct_no_card_next = 1
    vendor.direct_no_card_code = "CARD_POOL_EMPTY"  # [I] code
    await _zovo(dup, vendor, AUTO)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    job = await _job(dup, order)
    assert job.state == "queued" and job.error_code == "waiting_for_card" and not job.maybe_sent
    assert job.card_id is None and vendor.direct_executions == 0
    assert len(_posts(vendor, ORDERS)) == 1  # one refused POST, no order
    await _drive(env, dup, order, buyer)
    keys = _posts(vendor, ORDERS)
    assert len(keys) == 2 and keys[0] == keys[1]  # same idempotency key both times
    assert vendor.direct_executions == 1 and (await _job(dup, order)).state == "fulfilled"
    (row,) = await _audit(env, "procurement.waiting_for_card")
    assert row[0] == "warning" and row[3]["state"] == "queued"


# ------------------------------------------------------------------ vendor switch
async def test_vendor_switch_sends_the_flag_and_records_the_final_card(env, dup):  # noqa: F811
    vendor = _two_cards()
    vendor.direct_switch_to = 4243  # the vendor moves the order to another card
    await _zovo(dup, vendor, {**CLAUDE, "vendor_switch": True})
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    assert vendor.direct_cards_sent == [4242] and vendor.direct_switch_seen == [False]
    job = await _job(dup, order)
    assert job.card_id == "4243" and job.card_last4 == "4243"  # the card that paid


async def test_vendor_switch_off_is_the_default_flag(env, dup):  # noqa: F811
    vendor = _two_cards()
    await _zovo(dup, vendor, {**CLAUDE, "vendor_switch": False})
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    assert vendor.direct_switch_seen == [True]


# ------------------------------------------------------------------ console validation
async def _make_source(env, dup, params, *, expect=201):  # noqa: F811
    from tests.test_procurement_vcc import _zovo as zovo

    sid, _oid = await zovo(
        dup, _vendor(), CLAUDE, code="zovocard_" + uuid.uuid4().hex[:8], priority=30
    )
    admin, _ = await env.login("admin")
    r = await admin.post(
        "/api/v1/admin/procurement/sources",
        json={"product_id": str(dup.product.id), "supplier_id": str(sid), "params": params,
              "cost_estimate": "25", "enabled": False},
    )  # fmt: skip
    assert r.status_code == expect, r.text
    return admin, r


async def test_source_validation_of_card_modes(env, dup):  # noqa: F811
    # fixed (default): card_id required; auto_existing: optional and never stored
    await _make_source(env, dup, {"product": "claude", "plan": "pro"}, expect=422)
    _a, r = await _make_source(env, dup, {"product": "claude", "plan": "pro", "card_id": 7})
    assert r.json()["params"] == {"product": "claude", "plan": "pro", "card_id": 7}
    _a, r = await _make_source(
        env, dup, {"product": "claude", "plan": "max_5x", "card_mode": "auto_existing",
                   "card_id": 9, "vendor_switch": True},
    )  # fmt: skip
    assert r.json()["params"] == {
        "product": "claude", "plan": "max_5x", "card_mode": "auto_existing", "vendor_switch": True
    }  # fmt: skip


async def test_auto_issue_is_reserved_and_refused(env, dup):  # noqa: F811
    _a, r = await _make_source(
        env, dup, {"product": "claude", "plan": "pro", "card_mode": "auto_issue"}, expect=422
    )
    assert r.json()["error"]["code"] == "card_mode_unavailable"
    _a, r = await _make_source(
        env, dup, {"product": "claude", "plan": "pro", "card_mode": "bogus"}, expect=422
    )
    assert r.json()["error"]["code"] == "bad_params"
    # and the adapter itself refuses it, whatever reached the database
    from app.suppliers.base import OfferSpec
    from app.suppliers.zovocard.direct import direct_spec

    bad = OfferSpec(kind="direct", params={**AUTO, "card_mode": "auto_issue"})
    assert direct_spec(bad) is None


async def test_changing_vendor_switch_is_audited(env, dup):  # noqa: F811
    admin, r = await _make_source(env, dup, {"product": "claude", "plan": "pro", "card_id": 7})
    oid = r.json()["id"]
    p = await admin.patch(
        f"/api/v1/admin/procurement/sources/{oid}",
        json={"params": {"product": "claude", "plan": "pro", "card_id": 7, "vendor_switch": True}},
    )
    assert p.status_code == 200, p.text
    (row,) = await _audit(env, "procurement.vendor_switch_changed")
    assert row[0] == "warning" and row[2] == {"vendor_switch": False}
    assert row[3] == {"vendor_switch": True}


# ------------------------------------------------------------------ cost model
D = Decimal


def test_cost_rows_include_the_switch_cost():
    st = cm.normalize({})
    base = cm.Source("direct", D("20"), "claude")
    sw = cm.Source("direct", D("20"), "claude", vendor_switch=True)
    e0, e1 = cm.estimate(st, base, D("30"), D("0.02")), cm.estimate(st, sw, D("30"), D("0.02"))
    # 5 % failures x 2 switches x 0.30 fee, 3 free failures per card -> billed share 1 / 4
    assert e0.switch_cost == 0
    assert e1.switch_cost == D("0.05") * 2 * D("0.30") / 4 == D("0.00750")
    assert e1.total_cost - e0.total_cost == e1.switch_cost
    assert cm.recommend_usd(st, sw, D("0.02"), D("0.3")) > cm.recommend_usd(
        st, base, D("0.02"), D("0.3")
    )
    # no free allowance: every extra attempt is billed
    st0 = cm.normalize({"failed_free_count": 0})
    assert cm.estimate(st0, sw, D("30"), D("0.02")).switch_cost == D("0.03")
    # only direct sources switch cards
    assert cm.estimate(st, cm.Source("cdk", D("20"), vendor_switch=True), D("30"), D("0")
                       ).switch_cost == 0  # fmt: skip


async def test_the_cost_row_shows_the_switch_cost_and_never_changes_a_price(env, dup):  # noqa: F811
    from app.models import SupplierOffer
    from app.modules.admin.procurement_pricing import _source

    off = SupplierOffer(
        kind="direct", params={**CLAUDE, "vendor_switch": True}, cost_estimate=D("20"),
        currency="USD",
    )  # fmt: skip
    st = cm.normalize({})
    src, _why = _source(off, st, None, "USD")
    assert src.vendor_switch is True
    off.params = dict(CLAUDE)
    assert _source(off, st, None, "USD")[0].vendor_switch is False


# ------------------------------------------------------------------ isolation and leaks
async def test_card_in_flight_ignores_another_tenants_jobs(env, dup):  # noqa: F811
    from app.core.db import tenant_session
    from app.procurement.funding import card_in_flight
    from tests.test_procurement_jobs import _foreign_job

    vendor = _two_cards()
    sid, oid = await _zovo(dup, vendor, AUTO)
    fj = await _foreign_job(env)
    await _sql(
        env,
        "UPDATE procurement_jobs SET kind='direct', state='submitting', maybe_sent=true, "
        "card_id='4243', offer_id=:o, supplier_id=:s WHERE id=:j",
        o=oid, s=sid, j=fj,
    )  # fmt: skip
    async with tenant_session(dup.ctx.maker, env.tenant_id) as db:
        assert await card_in_flight(db, env.tenant_id, sid, "4243", uuid.uuid4()) == 0
    # the buyer of another tenant cannot read the waiting status of this tenant's order
    vendor.schedule_none = True
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    stranger, _ = await env.login("buyer")
    assert (await stranger.get(f"/api/v1/orders/{order['id']}/direct")).status_code == 404


async def test_no_card_number_in_logs_audit_rows_or_responses(env, dup, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    vendor = _two_cards()
    vendor.card_pool = [4243, 4242]
    vendor.direct_no_card_next = 1
    await _zovo(dup, vendor, {**AUTO, "vendor_switch": True})
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    admin, _ = await env.login("admin")
    job = await _job(dup, order)
    bodies = [
        (await admin.get("/api/v1/admin/procurement/jobs")).text,
        (await admin.get(f"/api/v1/admin/procurement/jobs/{job.id}")).text,
        (await admin.get(f"/api/v1/admin/procurement/sources?product_id={dup.product.id}")).text,
        (await buyer.get(f"/api/v1/orders/{order['id']}")).text,
        (await buyer.get(f"/api/v1/orders/{order['id']}/direct")).text,
        str(await _rows(env, "SELECT row_to_json(a)::text FROM audit_log a")),
        str(await _rows(env, "SELECT row_to_json(j)::text FROM procurement_jobs j")),
        caplog.text,
    ]
    for body in bodies:
        for cid in (4242, 4243):
            assert PAN(cid) not in body and f"{cid:012d}" not in body
    assert job.card_last4 == "4243" and job.card_id == "4243"  # id + last four only
