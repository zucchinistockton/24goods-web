"""Issue #29 part 1 (review-v12 §21): card modes of ``upstream_direct`` orders.

``fixed`` (today's behaviour), ``auto_existing`` (OUR pick from the card list, ADR-048: the
vendor's pick preview is no longer asked), the optional ``vendor_switch``, the parked "waiting for
card" job (bounded, no vendor spend, never a double submit), the reserved ``auto_issue`` and the
switch cost in the cost rows; the funding of the card (top-ups, caps, the pre-send card read).
Real ZovoCard adapter against ``app/devtools/mock_zovocard.py`` and the engine."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

import pytest

from app.core import costmodel as cm
from app.devtools.mock_zovocard import MockZovo
from tests.test_procurement_direct import (
    CLAUDE,
    _drive,
    _job,
    _order_of,
    _settle,
    _sql,
    _zovo,
    claude_session,
    dup,  # noqa: F401  (fixture)
    new_marker,
)
from tests.test_procurement_direct import (
    _vendor as _base_vendor,
)
from tests.test_procurement_vcc import _posts, _rows

AUTO = {"product": "claude", "plan": "pro", "card_mode": "auto_existing"}
PAN = lambda cid: f"5378{cid:012d}"  # noqa: E731  (the mock's full card number)
ORDERS = "/gpt-direct/orders"


def _vendor() -> MockZovo:
    """The direct tests' vendor WITHOUT the E2E smoke's card 1 (USD 5,000): our own pick
    (ADR-048) would otherwise always choose it."""
    v = _base_vendor()
    v.cards.pop(1, None)
    v.recharges.pop(1, None)
    return v


def _two_cards() -> MockZovo:
    v = _vendor()  # card 4242 (USD 500)
    v.add_funding_card(4243, available=600.0)  # the larger balance: our pick (ADR-048 order)
    return v


def _card_reads(vendor: MockZovo) -> int:
    return sum(1 for m, p, _h in vendor.requests if m == "GET" and p == "/cards")


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
    assert job.state == "fulfilled" and job.used_card_id == "4242" and job.used_last4 == "4242"
    assert job.card_id is None  # fixed: the offer's card is the sent one
    assert job.card_wait_since is None and job.card_candidates is None


# ------------------------------------------------------------------ auto_existing
async def test_auto_existing_picks_our_own_card_without_the_vendor_preview(env, dup):  # noqa: F811
    vendor = _two_cards()
    vendor.card_pool = [4242, 4243]  # the vendor's preview would say 4242: never asked
    await _zovo(dup, vendor, AUTO)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    assert vendor.direct_cards_sent == [4243] and vendor.schedule_calls == 0
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


async def test_a_cooling_card_is_never_tried(env, dup):  # noqa: F811
    vendor = _two_cards()
    vendor.usage_busy.add(4243)  # cooling down / busy at the vendor
    vendor.cards[4242]["available_amount"] = 0.0  # the only other one is too poor
    await _zovo(dup, vendor, AUTO)
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    job = await _job(dup, order)
    assert job.error_code == "waiting_for_card" and vendor.direct_cards_sent == []


# ------------------------------------------------------------------ waiting for a card
async def test_no_free_card_parks_then_a_retry_succeeds_without_a_double_submit(env, dup):  # noqa: F811
    vendor = _two_cards()
    vendor.usage_busy.update({4242, 4243})  # every card that has the money is busy
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
    assert rows[0][1]["reason"] == "card_pool_busy" and "card" not in str(rows[0][1]["order_id"])
    # the buyer sees the coarse status only
    info = (await buyer.get(f"/api/v1/orders/{order['id']}/direct")).json()
    assert info["waiting_for_supplier"] is True
    assert "card" not in str(info).lower().replace("waiting_for_supplier", "")
    # a card turns up: the same job goes on with the same key and request id
    vendor.usage_busy.clear()
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
    vendor.usage_busy.update({4242, 4243})
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


async def test_a_documented_refusal_re_keys_only_after_a_verified_no_order_lookup(env, dup):  # noqa: F811
    vendor = _two_cards()
    vendor.direct_no_card_next = 1
    vendor.direct_no_card_code = "CARD_FUNDING_BUSY"  # [V] code
    await _zovo(dup, vendor, AUTO)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    job = await _job(dup, order)
    first_key = f"pj:{job.id.hex}:submit"
    assert job.state == "queued" and job.error_code == "waiting_for_card" and not job.maybe_sent
    assert job.card_id is None and vendor.direct_executions == 0 and job.card_attempt == 1
    assert job.idempotency_key == f"pj:{job.id.hex}:c1" != first_key
    assert len(_posts(vendor, ORDERS)) == 1  # one refused POST, no order
    # the lookup by client_request_id ran AFTER the refused POST and BEFORE the re-key
    calls = [(m, p) for m, p, _h in vendor.requests if p == ORDERS]
    assert calls[0][0] == "POST" and ("GET", ORDERS) in calls[1:]
    await _drive(env, dup, order, buyer)
    keys = _posts(vendor, ORDERS)
    assert len(keys) == 2 and keys[0] == first_key and keys[1] == f"pj:{job.id.hex}:c1"
    assert vendor.direct_executions == 1 and (await _job(dup, order)).state == "fulfilled"
    assert _order_of(vendor)["client_request_id"] == f"pj{job.id.hex}a1"  # attempt-scoped id
    (row,) = await _audit(env, "procurement.waiting_for_card")
    assert row[0] == "warning" and row[3]["state"] == "queued"
    (rk,) = await _audit(env, "procurement.card_rekeyed")
    assert rk[0] == "warning" and rk[3]["key"].endswith(":c1")


async def test_a_refusal_whose_lookup_fails_is_held_never_re_keyed(env, dup, monkeypatch):  # noqa: F811
    from app.suppliers.base import Outcome, Result
    from app.suppliers.zovocard.supplier import ZovoCardSupplier

    async def broken(self, spec, rid, account_mask):
        return Result(Outcome.AMBIGUOUS, error_code="malformed_order_list")

    monkeypatch.setattr(ZovoCardSupplier, "_direct_find", broken)
    vendor = _two_cards()
    vendor.direct_no_card_next = 1
    vendor.direct_no_card_code = "CARD_IN_USE"
    await _zovo(dup, vendor, AUTO)
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await _settle(dup, 3)
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "card_refusal_unverified"
    assert job.card_attempt == 0 and job.idempotency_key.endswith(":submit") and job.maybe_sent
    assert len(_posts(vendor, ORDERS)) == 1 and vendor.direct_executions == 0
    assert await _audit(env, "procurement.card_rekeyed") == []


@pytest.mark.parametrize("code", ["NO_CARD_AVAILABLE", "CARD_POOL_EMPTY", "SUPPLIER_BUSY"])
async def test_a_guessed_code_goes_to_needs_manual_never_parks_or_re_keys(env, dup, code):  # noqa: F811
    vendor = _two_cards()
    vendor.direct_no_card_next = 1
    vendor.direct_no_card_code = code  # [I] codes, not in the vendor's docs
    await _zovo(dup, vendor, AUTO)
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await _settle(dup, 3)
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "card_code_unconfirmed"
    assert job.card_attempt == 0 and job.maybe_sent and vendor.direct_executions == 0
    assert len(_posts(vendor, ORDERS)) == 1
    assert await _audit(env, "procurement.waiting_for_card") == []


async def test_a_crash_after_the_lease_never_re_sends_or_re_keys(env, dup, monkeypatch):  # noqa: F811
    from app.suppliers.zovocard.supplier import ZovoCardSupplier

    real = ZovoCardSupplier._direct_run
    boom = {"n": 1}

    async def crash_once(self, job):
        if boom["n"]:
            boom["n"] = 0
            raise RuntimeError("worker died")
        return await real(self, job)

    monkeypatch.setattr(ZovoCardSupplier, "_direct_run", crash_once)
    vendor = _two_cards()
    await _zovo(dup, vendor, AUTO)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    job = await _job(dup, order)
    assert job.state == "fulfilled" and job.card_attempt == 0
    assert vendor.direct_executions == 1 and len(vendor.direct_orders) == 1
    assert set(_posts(vendor, ORDERS)) <= {f"pj:{job.id.hex}:submit"}  # the same key only


async def test_a_fixed_source_with_a_documented_card_busy_refusal_behaves_as_before(env, dup):  # noqa: F811
    vendor = _vendor()
    vendor.direct_no_card_next = 1
    vendor.direct_no_card_code = "CARD_FUNDING_BUSY"
    await _zovo(dup, vendor, CLAUDE)
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await _settle(dup, 4)
    job = await _job(dup, order)
    # UNAVAILABLE (nothing executed) -> reconcile (same-key lookup finds nothing) -> the SAME key is
    # resubmitted: exactly today's behaviour. Never parks, never re-keys, never waits
    assert job.state == "accepted_pending" and job.card_attempt == 0
    assert job.card_wait_since is None and job.idempotency_key.endswith(":submit")
    keys = _posts(vendor, ORDERS)
    assert len(keys) == 2 and keys[0] == keys[1] and vendor.direct_executions == 1
    assert await _audit(env, "procurement.waiting_for_card") == []
    assert await _audit(env, "procurement.card_rekeyed") == []


async def test_the_guessed_codes_are_not_mapped_to_unavailable():
    from app.suppliers.base import Outcome
    from app.suppliers.zovocard.errors import classify

    for code in ("NO_CARD_AVAILABLE", "CARD_POOL_EMPTY", "SUPPLIER_BUSY"):
        assert classify(400, code, write=True).outcome is Outcome.AMBIGUOUS
    for code in ("CARD_FUNDING_BUSY", "CARD_IN_USE", "CARD_CAPACITY_REACHED",
                 "CARD_CAPACITY_PRO20", "CARD_COOLING_DOWN"):  # fmt: skip
        assert classify(400, code, write=True).outcome is Outcome.UNAVAILABLE


# ------------------------------------------------------------------ buyer-card checks on picks
async def _buyer_card_job(env, *, mask=None, ref=None, tenant_id=None):
    """A ``vcc`` job (a card bought for a buyer) holding the given vendor id / masked number."""
    from sqlalchemy import text

    tid = tenant_id or env.tenant_id
    buyer0 = await env.user("buyer")
    p = await env.product(mode="upstream_vcc")
    oid, vid = uuid.uuid4(), uuid.uuid4()
    async with env.owner_session() as db:
        await db.execute(
            text(
                "INSERT INTO orders (id, tenant_id, order_no, buyer_id, product_id, title, qty, "
                "amount_minor, subtotal_minor, currency, status, delivery_mode) VALUES "
                "(:i, :t, :n, :b, :p, 'x', 1, 100, 100, 'CNY', 'paid', 'upstream_vcc')"
            ),
            {"i": oid, "t": tid, "n": "G" + uuid.uuid4().hex[:12], "b": buyer0.id, "p": p.id},
        )
        await db.execute(
            text(
                "INSERT INTO procurement_jobs (id, tenant_id, order_id, unit_no, kind, state, "
                "step, idempotency_key, card_mask, upstream_ref) VALUES "
                "(:j, :t, :o, 1, 'vcc', 'needs_manual', 'submit', :k, :m, :r)"
            ),
            {"j": vid, "t": tid, "o": oid, "k": "pj:" + vid.hex + ":submit", "m": mask, "r": ref},
        )
        await db.commit()
    return vid


async def test_an_auto_pick_whose_number_is_a_buyers_card_is_skipped(env, dup):  # noqa: F811
    from app.suppliers.zovocard.webhook import mask_pan

    vendor = _two_cards()
    vendor.card_pool = [4243, 4242]  # the vendor would pick 4243
    await _zovo(dup, vendor, AUTO)
    await _buyer_card_job(env, mask=mask_pan(vendor.cards[4243]["card_number"]))
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    assert vendor.direct_cards_sent == [4242]  # 4243 was never spent on
    job = await _job(dup, order)
    assert job.card_id == "4242" and "4243" not in str(job.card_candidates)


async def test_when_every_candidate_is_a_buyers_card_the_job_waits_and_spends_nothing(env, dup):  # noqa: F811
    from app.suppliers.zovocard.webhook import mask_pan

    vendor = _two_cards()
    vendor.card_pool = [4243, 4242]
    await _zovo(dup, vendor, AUTO)
    await _buyer_card_job(env, mask=mask_pan(vendor.cards[4243]["card_number"]))
    await _buyer_card_job(env, ref="4242")  # by vendor card id
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await _settle(dup, 3)
    job = await _job(dup, order)
    assert job.error_code == "waiting_for_card" and vendor.direct_cards_sent == []


async def test_another_tenants_buyer_card_does_not_block_a_pick(env, dup):  # noqa: F811
    from app.suppliers.zovocard.webhook import mask_pan
    from tests.test_procurement_jobs import _foreign_job

    vendor = _two_cards()
    vendor.card_pool = [4243, 4242]
    await _zovo(dup, vendor, AUTO)
    fj = await _foreign_job(env)
    await _sql(
        env,
        "UPDATE procurement_jobs SET kind='vcc', card_mask=:m, upstream_ref='4243' WHERE id=:j",
        m=mask_pan(vendor.cards[4243]["card_number"]), j=fj,
    )  # fmt: skip
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    assert vendor.direct_cards_sent == [4243]


async def test_stale_candidates_are_re_checked_before_any_spend(env, dup, monkeypatch):  # noqa: F811
    from app.procurement import engine

    monkeypatch.setattr(engine, "CARD_FRESH_S", -1)  # every pre-check is already too old
    vendor = _two_cards()
    await _zovo(dup, vendor, AUTO)
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await _settle(dup, 2)
    job = await _job(dup, order)
    assert vendor.direct_cards_sent == [] and not job.maybe_sent  # never spent on a stale pick
    assert _card_reads(vendor) >= 2  # it kept re-reading the cards instead
    monkeypatch.setattr(engine, "CARD_FRESH_S", 120)
    await _settle(dup, 2)
    assert vendor.direct_cards_sent and vendor.direct_executions == 1


async def test_a_buyer_card_recorded_after_the_pre_check_forces_a_new_check(env, dup, monkeypatch):  # noqa: F811
    """Re-audit M2: a buyer card pasted by hand (mask only, no vendor id) between the pre-check
    and the spend, matching the picked card, sends the job back to the full-mask pre-check."""
    from app.procurement import engine
    from app.suppliers.zovocard.webhook import mask_pan

    vendor = _two_cards()
    vendor.card_pool = [4243, 4242]  # the vendor would pick 4243
    await _zovo(dup, vendor, AUTO)
    orig, raced = engine._assign_card, []

    async def racing(ctx, db, job, offer, now):
        if not raced:  # the buyer card lands after the pre-check vetted 4243, before the spend
            raced.append(_card_reads(vendor))
            await _buyer_card_job(env, mask=mask_pan(vendor.cards[4243]["card_number"]))
        return await orig(ctx, db, job, offer, now)

    monkeypatch.setattr(engine, "_assign_card", racing)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    assert raced and vendor.direct_cards_sent == [4242]  # 4243 was never spent on
    assert _card_reads(vendor) > raced[0]  # the vetted list was dropped and checked again


# ------------------------------------------------------------------ vendor-reported card
async def test_a_reported_card_other_than_the_sent_one_is_a_discrepancy(env, dup):  # noqa: F811
    vendor = _two_cards()
    vendor.direct_order_over = {"card_id": 4243, "card_last_four": "4243"}  # a lie / a move
    await _zovo(dup, vendor, {**CLAUDE, "vendor_switch": False})
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await _settle(dup, 5)
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "card_mismatch" and job.maybe_sent
    assert job.used_card_id == "4243" and job.card_id is None  # the sent card is not overwritten
    (row,) = await _audit(env, "procurement.card_discrepancy")
    assert row[0] == "warning" and row[1]["reported"] == "4243" and "card_number" not in str(row)


async def test_in_flight_counts_the_sent_and_the_reported_card(env, dup):  # noqa: F811
    from app.core.db import tenant_session
    from app.procurement.funding import card_in_flight

    vendor = _two_cards()
    sid, _oid = await _zovo(dup, vendor, AUTO)
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    job = await _job(dup, order)
    await _sql(
        env,
        "UPDATE procurement_jobs SET state='submitting', maybe_sent=true, card_id='4242', "
        "used_card_id='4243' WHERE id=:j", j=job.id,
    )  # fmt: skip
    async with tenant_session(dup.ctx.maker, env.tenant_id) as db:
        for cid in ("4242", "4243"):
            assert await card_in_flight(db, env.tenant_id, sid, cid, uuid.uuid4()) == 1
        assert await card_in_flight(db, env.tenant_id, sid, "9999", uuid.uuid4()) == 0


async def test_vendor_switch_to_a_buyers_card_is_held(env, dup):  # noqa: F811
    vendor = _two_cards()
    vendor.direct_switch_to = 4243
    await _zovo(dup, vendor, {**CLAUDE, "vendor_switch": True})
    await _buyer_card_job(env, ref="4243")
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await _settle(dup, 5)
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "switched_to_buyer_card"
    assert job.used_card_id == "4243" and job.maybe_sent
    (row,) = await _audit(env, "procurement.card_discrepancy")
    assert row[0] == "warning"


async def test_an_unusable_reported_card_id_is_rejected_not_truncated(env, dup):  # noqa: F811
    from app.suppliers.zovocard import direct as dr

    assert dr.order_card({"card_id": "9" * 25}) == (dr.INVALID_CARD, None)
    assert dr.order_card({"card_id": 4243, "card_last_four": "4243"}) == ("4243", "4243")
    assert dr.order_card({}) == (None, None)
    vendor = _two_cards()
    vendor.direct_order_over = {"card_id": "9" * 25}
    await _zovo(dup, vendor, CLAUDE)
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await _settle(dup, 5)
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "card_mismatch"
    assert job.used_card_id is None


# ------------------------------------------------------------------ vendor switch: production
async def test_vendor_switch_is_refused_for_a_production_supplier(env, dup):  # noqa: F811
    from tests.test_procurement_vcc import _zovo as zovo

    sid, _oid = await zovo(dup, _vendor(), CLAUDE, code="zovocard_" + uuid.uuid4().hex[:8])
    await _sql(env, "UPDATE suppliers SET env = 'production' WHERE id = :s", s=sid)
    admin, _ = await env.login("admin")
    base = {"product": "claude", "plan": "pro", "card_id": 7}
    r = await admin.post(
        "/api/v1/admin/procurement/sources",
        json={"product_id": str(dup.product.id), "supplier_id": str(sid),
              "params": {**base, "vendor_switch": True}, "cost_estimate": "25",
              "enabled": False},
    )  # fmt: skip
    assert r.status_code == 422 and r.json()["error"]["code"] == "vendor_switch_unavailable"
    r = await admin.post(
        "/api/v1/admin/procurement/sources",
        json={"product_id": str(dup.product.id), "supplier_id": str(sid), "params": base,
              "cost_estimate": "25", "enabled": False},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    oid = r.json()["id"]
    r = await admin.patch(
        f"/api/v1/admin/procurement/sources/{oid}",
        json={"params": {**base, "vendor_switch": True}},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "vendor_switch_unavailable"
    # a row that already carries the flag cannot be enabled either
    await _sql(
        env,
        "UPDATE supplier_offers SET params = params || '{\"vendor_switch\": true}' WHERE id = :o",
        o=uuid.UUID(oid),
    )
    r = await admin.patch(f"/api/v1/admin/procurement/sources/{oid}", json={"enabled": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "vendor_switch_unavailable"


async def test_the_body_never_allows_a_switch_for_a_production_supplier():
    from app.suppliers.zovocard import direct as dr

    spec = dr.DirectSpec("claude", "pro", 7, "US", "USD", "fixed", True)
    kw = {"credential": "x", "preflight_token": None, "request_id": "r", "pricing_version": None}
    assert dr.order_body(spec, **kw)["no_auto_card_switch"] is False
    assert dr.order_body(spec, **kw, allow_switch=False)["no_auto_card_switch"] is True


# ------------------------------------------------------------------ vendor switch
async def test_vendor_switch_sends_the_flag_and_records_the_final_card(env, dup):  # noqa: F811
    vendor = _two_cards()
    vendor.direct_switch_to = 4243  # the vendor moves the order to another card
    await _zovo(dup, vendor, {**CLAUDE, "vendor_switch": True})
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    assert vendor.direct_cards_sent == [4242] and vendor.direct_switch_seen == [False]
    job = await _job(dup, order)
    assert job.state == "fulfilled" and job.card_id is None  # sent: the offer's card 4242
    assert job.used_card_id == "4243" and job.used_last4 == "4243"  # the card that paid


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
    # upper bound: 5 % failures x 2 switches x 0.30 fee, NO free-failure discount
    assert e0.switch_cost == 0
    assert e1.switch_cost == D("0.05") * 2 * D("0.30") == D("0.030")
    assert e1.total_cost - e0.total_cost == e1.switch_cost
    # with the first failed attempt (failure_cost) all 1 + max_auto_switches attempts are covered
    # once: the final failed attempt is neither double-counted nor missed
    assert e1.failure_cost + e1.switch_cost == D("0.05") * 3 * D("0.30")
    assert cm.recommend_usd(st, sw, D("0.02"), D("0.3")) > cm.recommend_usd(
        st, base, D("0.02"), D("0.3")
    )
    # the free allowance does not discount the bound; max_auto_switches is a setting
    st0 = cm.normalize({"failed_free_count": 100})
    assert cm.estimate(st0, sw, D("30"), D("0.02")).switch_cost == D("0.030")
    st3 = cm.normalize({"max_auto_switches": 3})
    assert cm.estimate(st3, sw, D("30"), D("0.02")).switch_cost == D("0.045")
    assert cm.normalize({})["max_auto_switches"] == 2
    assert cm.normalize({"max_auto_switches": -1})["max_auto_switches"] == 2  # invalid: default
    # re-audit M3: never below the vendor default (we send no switch cap)
    assert cm.normalize({"max_auto_switches": 1})["max_auto_switches"] == 2
    assert cm.normalize({"max_auto_switches": 0})["max_auto_switches"] == 2
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
    vendor.usage_busy.update({4242, 4243})
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    stranger, _ = await env.login("buyer")
    assert (await stranger.get(f"/api/v1/orders/{order['id']}/direct")).status_code == 404


async def test_no_card_number_in_logs_audit_rows_or_responses(env, dup, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    vendor = _two_cards()
    vendor.card_pool = [4243, 4242]
    vendor.direct_no_card_next = 1
    vendor.direct_no_card_code = "CARD_COOLING_DOWN"
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
    assert job.card_id in ("4242", "4243") and job.used_last4 == "4243"  # id + last four only


# ------------------------------------------------------------------ ADR-048: funding the card
RECHARGE = "/cards/recharge"


async def _funding(env, **settings) -> None:
    import json

    await _sql(
        env,
        "UPDATE tenants SET funding_settings = CAST(:s AS jsonb) WHERE id = :t",
        s=json.dumps(settings),
        t=env.tenant_id,
    )


async def _fundings(env):
    return await _rows(
        env,
        "SELECT state, amount, card_id, idem_key FROM procurement_fundings ORDER BY created_at",
    )


async def test_no_money_on_any_card_goes_to_staff_at_once(env, dup):  # noqa: F811
    """Owner 2026-11-19: no card can pay and top-ups are off (the default) -> needs_manual at
    once, never the 30-minute card wait; nothing is moved or sent."""
    vendor = _two_cards()
    vendor.cards[4242]["available_amount"] = 0.3
    vendor.cards[4243]["available_amount"] = 0.3
    await _zovo(dup, vendor, AUTO)
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    job = await _job(dup, order)
    # (no other source to fail over to: the failover's own reason prefix)
    assert job.state == "needs_manual" and job.error_code == "no_source:funds_insufficient"
    assert job.card_wait_since is None and not job.maybe_sent
    assert _posts(vendor, RECHARGE) == [] and _posts(vendor, ORDERS) == []
    assert await _fundings(env) == []


async def test_an_allowed_top_up_moves_the_shortfall_then_orders_once(env, dup):  # noqa: F811
    vendor = _vendor()  # one card, almost empty
    vendor.cards[4242]["available_amount"] = 1.0
    await _funding(env, auto_recharge=True)
    await _zovo(dup, vendor, AUTO)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    job = await _job(dup, order)
    assert job.state == "fulfilled" and vendor.direct_cards_sent == [4242]
    keys = _posts(vendor, RECHARGE)
    assert keys == [f"pj:{job.id.hex}:fund1"] and vendor.card_recharges == 1
    (row,) = await _fundings(env)
    assert row[0] == "done" and row[2] == "4242" and row[3] == keys[0]
    amount = vendor.recharges[4242][0]["amount"]
    assert amount == int(amount) and amount >= 10 and Decimal(str(amount)) == row[1]
    assert len(_posts(vendor, ORDERS)) == 1
    assert len(await _audit(env, "procurement.topup_done")) == 1


async def test_a_card_that_already_holds_the_money_wins_over_a_top_up(env, dup):  # noqa: F811
    vendor = _two_cards()
    vendor.cards[4242]["available_amount"] = 1.0  # would need a top-up
    await _funding(env, auto_recharge=True)
    await _zovo(dup, vendor, AUTO)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    assert vendor.direct_cards_sent == [4243] and _posts(vendor, RECHARGE) == []


async def test_a_wallet_refusal_moves_nothing_and_goes_to_staff(env, dup):  # noqa: F811
    vendor = _vendor()
    vendor.cards[4242]["available_amount"] = 1.0
    vendor.force(RECHARGE, "insufficient_balance")
    await _funding(env, auto_recharge=True)
    await _zovo(dup, vendor, AUTO)
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await _settle(dup, 3)
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "funds_insufficient"
    assert not job.maybe_sent and _posts(vendor, ORDERS) == []
    (row,) = await _fundings(env)
    assert row[0] == "failed"


async def test_the_daily_cap_is_checked_again_when_the_card_is_assigned(env, dup, monkeypatch):  # noqa: F811
    """Another top-up uses the cap between the pre-check and the assignment: nothing is moved,
    staff decide (never a wait)."""
    from app.procurement import engine

    vendor = _vendor()
    vendor.cards[4242]["available_amount"] = 1.0
    await _funding(env, auto_recharge=True, daily_cap_usd="30")
    sid, _oid = await _zovo(dup, vendor, AUTO)
    orig, raced = engine._assign_card, []

    async def racing(ctx, db, job, offer, now):
        if not raced:
            raced.append(1)
            await _sql(
                env,
                "INSERT INTO procurement_fundings (id, tenant_id, supplier_id, card_id, amount,"
                " state, idem_key) VALUES (:i, :t, :s, '9', 25, 'done', 'other-key')",
                i=uuid.uuid4(), t=env.tenant_id, s=sid,
            )  # fmt: skip
        return await orig(ctx, db, job, offer, now)

    monkeypatch.setattr(engine, "_assign_card", racing)
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await _settle(dup, 3)
    job = await _job(dup, order)
    assert raced and job.state == "needs_manual" and job.error_code == "funding_cap_reached"
    assert _posts(vendor, RECHARGE) == [] and _posts(vendor, ORDERS) == []


async def test_a_card_emptied_before_the_send_is_never_charged(env, dup, monkeypatch):  # noqa: F811
    """Owner: never send an order the card cannot pay (a decline costs a fee and this card type
    is deleted after three). The real-time card read right before the POST refuses it."""
    from app.procurement import engine

    vendor = _vendor()
    await _zovo(dup, vendor, AUTO)
    orig = engine._assign_card

    async def drain(ctx, db, job, offer, now):
        ok = await orig(ctx, db, job, offer, now)
        vendor.cards[4242]["available_amount"] = 0.5  # spent elsewhere meanwhile
        return ok

    monkeypatch.setattr(engine, "_assign_card", drain)
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await _settle(dup, 3)
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "funds_insufficient"
    assert _posts(vendor, ORDERS) == [] and not job.maybe_sent


async def test_stock_follows_the_money(env, dup):  # noqa: F811
    """The storefront check: sold out only when neither a card nor an allowed top-up can pay."""
    from app.procurement import service

    vendor = _vendor()
    vendor.cards[4242]["available_amount"] = 1.0
    _sid, oid = await _zovo(dup, vendor, AUTO)
    out = await service.check_offer(dup.ctx, env.tenant_id, oid)
    assert out["available"] is False and out["reason"] == "funds_insufficient"
    await _funding(env, auto_recharge=True)
    out = await service.check_offer(dup.ctx, env.tenant_id, oid)
    assert out["available"] is True
    await _funding(env, auto_recharge=True, per_topup_max_usd="5")  # the top-up is above the cap
    out = await service.check_offer(dup.ctx, env.tenant_id, oid)
    assert out["available"] is False and out["reason"] == "funds_insufficient"


async def test_an_unclear_top_up_is_never_followed_by_a_second_one(env, dup):  # noqa: F811
    """Payment audit H1: the vendor answers "being confirmed" (no code, not cached). The job keeps
    THAT top-up (row ``sent``, same card, same key); once the card shows the money it orders
    without resending, and exactly one top-up ever ran."""
    vendor = _vendor()
    vendor.cards[4242]["available_amount"] = 1.0
    vendor.recharge_pending_next = 1
    await _funding(env, auto_recharge=True)
    await _zovo(dup, vendor, AUTO)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _settle(dup, 2)
    (row,) = await _fundings(env)
    assert row[0] == "sent" and vendor.card_recharges == 1
    assert _posts(vendor, ORDERS) == []
    keys = set(_posts(vendor, RECHARGE))
    assert len(keys) == 1  # any resend used the SAME key
    # the vendor confirms the pending top-up: the card now holds the money
    pending = vendor.recharges[4242][0]
    pending["status"] = "success"
    vendor.cards[4242]["available_amount"] += pending["amount"]
    await _drive(env, dup, order, buyer)
    assert vendor.card_recharges == 1 and set(_posts(vendor, RECHARGE)) == keys
    assert len(_posts(vendor, ORDERS)) == 1
    rows = await _fundings(env)
    assert len(rows) == 1 and rows[0][0] == "sent"  # unclear stays counted (staff can settle)


async def test_an_unreadable_wallet_is_a_retry_not_sold_out_to_staff(env, dup):  # noqa: F811
    """Payment audit M1: "no money" must be known, not guessed from a failed wallet read."""
    from app.procurement import service

    vendor = _vendor()
    vendor.cards[4242]["available_amount"] = 1.0
    await _funding(env, auto_recharge=True)
    _sid, oid = await _zovo(dup, vendor, AUTO)
    vendor.force("/balance", "500", times=5)
    out = await service.check_offer(dup.ctx, env.tenant_id, oid)
    assert out["available"] is False and out["reason"] == "wallet_unreadable"


# ------------------------------------------------------------------ ADR-049: open + sweep
async def test_no_card_with_a_use_left_opens_one_and_sweeps_the_used_up_card(env, dup):  # noqa: F811
    """Owner 2026-11-21: every card's uses are spent but the wallet can pay -> the paid order's
    fresh check opens ONE new card (opening fee + the order amount, counted against the daily cap)
    and the order goes out on it; afterwards the used-up card's money goes back to the wallet."""
    vendor = _vendor()  # card 4242 (USD 500)
    vendor.usage_remaining[4242] = 0  # its uses for this tier are spent
    await _funding(env, auto_recharge=True)
    await _zovo(dup, vendor, AUTO)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    job = await _job(dup, order)
    assert vendor.card_opens == 1
    new_id = max(vendor.cards)
    assert job.state == "fulfilled" and vendor.direct_cards_sent == [new_id]
    keys = _posts(vendor, "/cards/open")
    assert len(keys) == 1 and keys[0].startswith("open:")
    rows = await _rows(
        env, "SELECT kind, state, card_id, amount FROM procurement_fundings ORDER BY created_at"
    )
    kinds = {r[0]: r for r in rows}
    assert kinds["open"][1] == "done" and kinds["open"][2] == str(new_id)
    assert kinds["open"][3] >= Decimal(10)  # first load (whole dollars) + opening fee
    # the used-up card keeps USD 1 (the vendor's minimum when it states none) and sends the rest
    assert vendor.card_refunds == [(4242, 499.0)]
    assert kinds["sweep"][1] == "done" and kinds["sweep"][3] == Decimal(499)
    assert len(await _audit(env, "procurement.card_opened")) == 1
    assert len(await _audit(env, "procurement.card_swept")) == 1


async def test_without_the_switch_a_used_up_pool_still_goes_to_staff(env, dup):  # noqa: F811
    vendor = _vendor()
    vendor.usage_remaining[4242] = 0
    await _funding(env, auto_recharge=True, auto_open_cards=False, sweep_leftover=False)
    await _zovo(dup, vendor, AUTO)
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await _settle(dup, 3)
    job = await _job(dup, order)
    assert vendor.card_opens == 0 and vendor.card_refunds == []
    assert job.state in ("needs_manual", "queued") and _posts(vendor, ORDERS) == []
