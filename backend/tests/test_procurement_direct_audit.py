"""Payment-audit and security-review fixes on ``upstream_direct`` (H1, H2, M1-M8, S-M1, S-L1/2)."""

from __future__ import annotations

# ruff: noqa: F811, E501  (fixtures are imported from the direct tests; long SQL / URLs)
import base64
import urllib.parse
import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.devtools.mock_zovocard import MockZovo
from tests.helpers import buy, pay_order
from tests.test_procurement_direct import (
    CLAUDE,
    GPT,
    JOBS,
    DUp,
    _creds,
    _drive,
    _order_of,
    _settle,
    _vendor,
    claude_session,
    dup,  # noqa: F401
    gpt_session,
    new_marker,
)
from tests.test_procurement_m2 import _status
from tests.test_procurement_vcc import _job, _posts, _rows, _sql, _zovo


async def _paid(dup: DUp, params=CLAUDE, vendor: MockZovo | None = None, session=None):
    vendor = vendor or _vendor()
    await _zovo(dup, vendor, params)
    buyer, order = await dup.paid_direct(session or claude_session(new_marker()))
    return vendor, buyer, order


# ------------------------------------------------------------------ H1: the funding card
async def test_a_card_bought_for_a_buyer_is_never_a_funding_card(env, dup):
    vendor = _vendor()
    sid, _oid = await _zovo(dup, vendor, CLAUDE)
    # a vcc job of the SAME supplier holds vendor card 4242 (any state, even voided)
    async with env.owner_session() as db:
        order_id = uuid.uuid4()
        buyer = await env.user("buyer")
        p = await env.product(mode="upstream_vcc")
        await db.execute(
            text(
                "INSERT INTO orders (id, tenant_id, order_no, buyer_id, product_id, title, qty, "
                "amount_minor, subtotal_minor, currency, status, delivery_mode) VALUES "
                "(:i, :t, :n, :b, :p, 'x', 1, 100, 100, 'CNY', 'delivered', 'upstream_vcc')"
            ),
            {"i": order_id, "t": env.tenant_id, "n": "G" + uuid.uuid4().hex[:12],
             "b": buyer.id, "p": p.id},
        )  # fmt: skip
        jid = uuid.uuid4()
        await db.execute(
            text(
                "INSERT INTO procurement_jobs (id, tenant_id, order_id, unit_no, kind, state, "
                "step, idempotency_key, supplier_id, upstream_ref) VALUES (:j, :t, :o, 1, 'vcc', "
                "'voided', 'done', :k, :s, '4242')"
            ),
            {
                "j": jid,
                "t": env.tenant_id,
                "o": order_id,
                "k": "pj:" + jid.hex + ":submit",
                "s": sid,
            },
        )
        await db.commit()
    admin, _ = await env.login("admin")
    other = await _zovo(dup, _vendor(), CLAUDE, code="zovocard_b", priority=20)
    r = await admin.post(
        "/api/v1/admin/procurement/sources",
        json={"product_id": str(dup.product.id), "supplier_id": str(sid),
              "params": {**CLAUDE, "plan": "max_5x"}, "cost_estimate": "120", "enabled": False},
    )  # fmt: skip
    assert r.status_code == 422 and r.json()["error"]["code"] == "funding_card_invalid", r.text
    assert other  # (a source on a supplier that never bought that card is fine: no clash)
    # and the engine refuses before anything is sent, even if the offer was saved earlier
    buyer2, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    job = (await dup.jobs(order["id"]))[0]
    assert job.state == "needs_manual" and job.error_code == "funding_card_invalid"
    assert not job.maybe_sent and vendor.preflights == 0 and vendor.direct_executions == 0
    assert buyer2 is not None


async def test_funding_card_must_be_ours_active_and_have_capacity(env, dup):
    from app.suppliers.base import OfferSpec
    from tests.test_procurement_vcc_audit import _adapter

    vendor = _vendor()
    sup = _adapter(vendor)
    spec = OfferSpec(kind="direct", params=CLAUDE)
    assert (await sup.availability(spec)).available
    # a card that is not ours answers 404 on GET /cards/{id}
    av = await sup.availability(OfferSpec(kind="direct", params={**CLAUDE, "card_id": 9999}))
    assert not av.available and av.reason == "funding_card_invalid"
    vendor.cards[4242]["status"] = "FROZEN"
    av = await sup.availability(spec)
    assert not av.available and av.reason == "funding_card_invalid"
    vendor.cards[4242]["status"] = "ACTIVE"
    vendor.usage_remaining[4242] = 0  # the vendor's per-card usage cap is used up
    av = await sup.availability(spec)
    assert not av.available and av.reason == "funding_card_exhausted"
    vendor.usage_remaining.clear()
    vendor.cards[4242]["available_amount"] = 1.0  # less than the 21.15 the order costs
    av = await sup.availability(spec)
    assert not av.available and av.reason == "funding_card_low"


async def test_an_exhausted_funding_card_stops_the_offer_before_any_send(env, dup):
    vendor = _vendor()
    vendor.usage_remaining[4242] = 0
    await _zovo(dup, vendor, CLAUDE)
    _buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    job = (await dup.jobs(order["id"]))[0]
    # the pre-check said "no" (a definite refusal): no source is left, nothing was sent
    assert job.state == "needs_manual" and job.error_code == "no_source:plan_unavailable"
    assert not job.maybe_sent and vendor.preflights == 0
    ok = await env.scalar("SELECT last_check_ok FROM supplier_offers")
    assert ok is False  # the storefront hides the source


async def test_one_order_at_a_time_per_funding_card(env, dup):
    vendor = _vendor()
    vendor.direct_end = "running"
    await _zovo(dup, vendor, CLAUDE)
    _b1, o1 = await dup.paid_direct(claude_session(new_marker()))
    _b2, o2 = await dup.paid_direct(claude_session(new_marker()))
    for _ in range(2):
        await dup.run()
    jobs = {str(j.order_id): j for j in await dup.jobs()}
    states = sorted(j.state for j in jobs.values())
    assert states == ["accepted_pending", "queued"], states
    waiting = next(j for j in jobs.values() if j.state == "queued")
    assert waiting.error_code == "card_busy" and not waiting.maybe_sent
    assert vendor.direct_executions == 1  # the second waits for the card, it never fails over
    assert o1 and o2


async def test_repeated_declines_switch_the_source_off(env, dup):
    vendor = _vendor()
    vendor.direct_end = "declined"
    await _zovo(dup, vendor, CLAUDE)
    for _ in range(3):
        _b, order = await dup.paid_direct(claude_session(new_marker()))
        for _ in range(4):
            await _settle(dup, 1)
        assert (await _job(dup, order)).error_code == "order_declined"
        await env.drain()
    assert await env.scalar("SELECT enabled FROM supplier_offers") is False  # durable
    n = await env.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'procurement.offer_declining' "
        "AND severity = 'critical'"
    )
    assert n >= 1
    # routing has stopped: the next order finds no source and nothing reaches the vendor
    before = vendor.preflights
    await dup.run()  # (a precheck / refresh pass cannot switch the source back on)
    buyer, _u = await env.login("buyer")
    r = await buyer.post(
        "/api/v1/orders",
        json={"product_id": str(dup.product.id), "qty": 1, "session": claude_session("Z")},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "out_of_stock"  # not sold
    assert vendor.preflights == before and vendor.direct_executions == 3
    # staff are alerted (once per switch-off)
    from app.modules.payments.reconcile import alert_declining_offers

    async with env.api_session() as db:
        assert await alert_declining_offers(env.res, db, env.tenant_id, datetime.now(timezone.utc))


# ------------------------------------------------------------------ H2: no failover after a send
async def test_no_failover_and_no_unsent_after_a_possible_send(env, dup):
    a, b = _vendor(), _vendor()
    a.force("/gpt-direct/orders", "500")  # the first POST may have run (it did not)
    a.force("/gpt-direct/orders", "503", times=30)  # afterwards: a definite "unavailable"
    await _zovo(dup, a, CLAUDE, code="zovocard", priority=10)
    await _zovo(dup, b, CLAUDE, code="zovocard_b", priority=20)
    _buyer, order = await dup.paid_direct(claude_session(new_marker()))
    for _ in range(5):
        await _settle(dup, 1)
    job = await _job(dup, order)
    assert job.maybe_sent  # never cleared by a "no" that arrives after a possible send
    assert b.preflights == 0 and b.direct_executions == 0 and not job.tried_offer_ids
    assert job.state in ("submitting", "needs_manual")


async def test_order_list_search_pages_until_the_vendor_has_no_more(env):
    from app.suppliers.base import JobSpec, OfferSpec
    from app.suppliers.zovocard import direct as dr
    from tests.test_procurement_vcc_audit import _adapter

    vendor = _vendor()
    jid = "ab" * 16
    mine = dr.client_request_id(jid)
    vendor.direct_orders[801] = {  # the OLDEST order is ours: it is on the last page
        "id": 801, "client_request_id": mine, "plan": "pro", "product": "claude",
        "status": "queued", "polls": 0, "currency": "USD", "amount": 2000, "card_id": 4242,
        "cred": "x",
    }  # fmt: skip
    for i in range(802, 811):
        vendor.direct_orders[i] = {
            **vendor.direct_orders[801],
            "id": i,
            "client_request_id": f"o{i}",
        }
    vendor.list_cap = 2  # 5 pages of 2
    sup = _adapter(vendor)
    spec = JobSpec(
        job_id=jid, step="reconcile", idempotency_key=f"pj:{jid}:submit",
        offer=OfferSpec(kind="direct", params=CLAUDE),
    )  # fmt: skip
    res = await sup.query(spec)
    assert res.upstream_ref == "801" and res.outcome.value == "accepted_pending"
    del vendor.direct_orders[801]
    res = await sup.query(spec)  # every page read, nothing found: a definite "not found"
    assert res.outcome.value == "unavailable" and res.error_code == "order_not_found"
    import app.suppliers.zovocard.supplier as supmod

    old = supmod.ORDER_LIST_MAX_PAGES
    supmod.ORDER_LIST_MAX_PAGES = 2  # longer than we read: "not found" would be a guess
    try:
        res = await sup.query(spec)
    finally:
        supmod.ORDER_LIST_MAX_PAGES = old
    assert res.outcome.value == "ambiguous" and res.error_code == "order_list_too_long"


# ------------------------------------------------------------------ M1: the quote
async def test_a_quote_above_the_budget_is_refused_before_any_order(env, dup):
    vendor = _vendor()
    vendor.direct_quote_factor = 2.0  # USD 40 against a budget of 25 x 1.1
    await _zovo(dup, vendor, CLAUDE)
    _buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "quote_over_bound"
    assert vendor.direct_executions == 0 and not job.maybe_sent  # nothing sent, not charged


async def test_a_local_currency_quote_is_held_to_the_plan_ceiling(env, dup):
    vendor = _vendor()
    vendor.direct_quote_factor = 1.5  # PHP 165000 against maxAmountMinor 121000
    await _zovo(dup, vendor, GPT)
    _buyer, order = await dup.paid_direct(gpt_session(new_marker()))
    await dup.run()
    job = await _job(dup, order)
    assert job.error_code == "quote_over_bound" and vendor.direct_executions == 0


async def test_a_session_of_another_account_is_refused_by_the_preflight(env, dup):
    from app.suppliers.base import JobSpec, OfferSpec
    from tests.test_procurement_vcc_audit import _adapter

    vendor = _vendor()
    sup = _adapter(vendor)
    jid = "cd" * 16
    spec = JobSpec(
        job_id=jid, step="sent", idempotency_key=f"pj:{jid}:submit",
        offer=OfferSpec(kind="direct", params=CLAUDE), credential=claude_session("A"),
        account_mask="z***@other.test",
    )  # fmt: skip
    res = await sup.submit(spec)
    assert res.error_code == "session_other_account" and vendor.direct_executions == 0


# ------------------------------------------------------------------ M3: identity on completion
@pytest.mark.parametrize(
    "quirk",
    [{"omit": {"client_request_id"}}, {"omit": {"plan"}}, {"over": {"plan": "max_5x"}},
     {"over": {"account_email": "z***@other.test"}}],
)  # fmt: skip
async def test_a_completed_order_must_be_ours_or_nothing_is_delivered(env, dup, quirk):
    vendor = _vendor()
    await _zovo(dup, vendor, CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()  # accepted_pending, the account mask is recorded
    vendor.direct_omit = set(quirk.get("omit", ()))
    vendor.direct_order_over = dict(quirk.get("over", {}))
    for _ in range(4):
        await _settle(dup, 1)
        await env.drain()
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code in ("order_mismatch", "plan_mismatch")
    assert not job.card_stock_ids and await _status(buyer, order) == "manual_review"
    if job.error_code == "order_mismatch":
        n = await env.scalar(
            "SELECT count(*) FROM audit_log WHERE action = 'procurement.order_mismatch' "
            "AND severity = 'critical'"
        )
        assert n == 1


# ------------------------------------------------------------------ M7: what "nothing to undo" means
async def test_void_only_treats_a_released_cancel_as_nothing_to_undo(env):
    from app.suppliers.base import JobSpec, OfferSpec
    from tests.test_procurement_vcc_audit import _adapter

    vendor = _vendor()
    sup = _adapter(vendor)
    jid = "ef" * 16
    vendor.direct_orders[801] = {
        "id": 801, "client_request_id": "x", "plan": "pro", "product": "claude",
        "status": "cancelled", "polls": 9, "currency": "USD", "amount": 2000, "card_id": 4242,
        "cred": "x",
    }  # fmt: skip
    spec = JobSpec(
        job_id=jid, step="void", idempotency_key=f"pj:{jid}:void",
        offer=OfferSpec(kind="direct", params=CLAUDE), upstream_ref="801",
    )  # fmt: skip
    vendor.direct_fee_status = "released"
    assert (await sup.void(spec)).outcome.value == "ok"
    vendor.direct_fee_status = "held"  # a fee is still held: staff must look
    res = await sup.void(spec)
    assert res.outcome.value == "fatal" and res.error_code == "order_failure_unconfirmed"
    assert res.detail == "cancelled:held"
    vendor.direct_orders[801]["status"] = "declined"  # declined is never silently "voided"
    vendor.direct_fee_status = "released"
    assert (await sup.void(spec)).error_code == "order_failure_unconfirmed"


async def test_declined_order_void_stays_visible_with_its_fee_status(env, dup):
    vendor = _vendor()
    vendor.direct_end = "declined"
    await _zovo(dup, vendor, CLAUDE)
    _buyer, order = await dup.paid_direct(claude_session(new_marker()))
    for _ in range(4):
        await _settle(dup, 1)
    job = await _job(dup, order)
    assert job.upstream_status == "declined:held"  # the console shows status and fee status
    admin, _ = await env.login("admin")
    r = await admin.post(f"{JOBS}/{job.id}/void", json={"reason": "decline, give the fee back"})
    assert r.status_code == 200, r.text
    await _settle(dup, 4)
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "void_failed"
    assert (await admin.get(f"{JOBS}/{job.id}")).json()["upstream_status"] == "declined:held"


# ------------------------------------------------------------------ M8: a late completion is seen
async def test_a_held_order_that_completes_later_is_delivered(env, dup):
    vendor = _vendor()
    vendor.direct_end = "running"
    await _zovo(dup, vendor, CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    await _sql(env, "UPDATE procurement_jobs SET first_sent_at = now() - interval '3 hours'")
    await _settle(dup, 2)
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "pending_too_long"
    assert job.next_poll_at is not None  # slow watch (15 min), not abandoned
    await env.drain()
    assert await _status(buyer, order) == "manual_review"
    vendor.direct_end = "completed"  # the vendor finishes after all
    for _ in range(3):
        await _sql(env, "UPDATE procurement_jobs SET next_poll_at = now() - interval '1 second' "
                        "WHERE state = 'needs_manual'")  # fmt: skip
        await dup.run()
    job = await _job(dup, order)
    assert job.state == "fulfilled" and len(job.card_stock_ids) == 1
    await env.drain()
    assert await _status(buyer, order) == "delivered"
    assert vendor.direct_executions == 1 and len(_posts(vendor, "/gpt-direct/orders")) == 1


# ------------------------------------------------------------------ M4 / M5 / M6
async def test_hand_fulfil_without_a_known_order_searches_the_vendor_first(env, dup):
    vendor = _vendor()
    vendor.force("/gpt-direct/orders", "lost_response")  # executed, answer lost, no reference yet
    await _zovo(dup, vendor, CLAUDE)
    _buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()  # ambiguous: no reference recorded yet
    job = await _job(dup, order)
    assert job.upstream_ref is None and job.maybe_sent
    admin, _ = await env.login("admin")
    r = await admin.post(f"{JOBS}/{job.id}/fulfil", json={"reason": "I did this by hand now"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "order_in_flight"
    assert vendor.direct_executions == 1  # found by the search, never re-created


async def test_ledger_rows_link_only_to_a_job_of_their_own_kind(env, dup):
    from app.core.db import tenant_session
    from app.procurement import ledger
    from app.suppliers.base import LedgerRow

    vendor = _vendor()
    sid, _ = await _zovo(dup, vendor, CLAUDE)
    jobs = {}
    async with env.owner_session() as db:
        buyer = await env.user("buyer")
        p = await env.product(mode="upstream_cdk")
        for kind in ("cdk", "direct"):
            oid, jid = uuid.uuid4(), uuid.uuid4()
            await db.execute(
                text(
                    "INSERT INTO orders (id, tenant_id, order_no, buyer_id, product_id, title, "
                    "qty, amount_minor, subtotal_minor, currency, status, delivery_mode) VALUES "
                    "(:i, :t, :n, :b, :p, 'x', 1, 100, 100, 'CNY', 'delivered', 'upstream_cdk')"
                ),
                {"i": oid, "t": env.tenant_id, "n": "G" + uuid.uuid4().hex[:12],
                 "b": buyer.id, "p": p.id},
            )  # fmt: skip
            await db.execute(
                text(
                    "INSERT INTO procurement_jobs (id, tenant_id, order_id, unit_no, kind, state, "
                    "step, idempotency_key, supplier_id, upstream_ref) VALUES (:j, :t, :o, 1, :k, "
                    "'fulfilled', 'done', :ik, :s, '777')"
                ),
                {"j": jid, "t": env.tenant_id, "o": oid, "k": kind,
                 "ik": "pj:" + jid.hex + ":submit", "s": sid},
            )  # fmt: skip
            jobs[kind] = jid
        await db.commit()
    rows = [
        LedgerRow("b1", "gpt_direct_cdk", -1, "USD", None, ref_id="777"),
        LedgerRow("b2", "gpt_direct_order", -1, "USD", None, ref_id="777"),
    ]
    rows = [replace(r, amount=__import__("decimal").Decimal(str(r.amount))) for r in rows]
    async with tenant_session(dup.ctx.maker, env.tenant_id) as db:
        await ledger.record_rows(
            db, tenant_id=env.tenant_id, supplier_id=sid, default_currency="USD", rows=rows
        )
        await db.commit()
    got = {r[0]: r[1] for r in await _rows(env, "SELECT external_id, job_id FROM supplier_ledger")}
    assert got["b1"] == jobs["cdk"] and got["b2"] == jobs["direct"]


async def test_a_non_usd_order_is_marked_cost_incomplete_not_given_a_margin(env, dup):
    vendor = _vendor()
    await _zovo(dup, vendor, GPT)  # a PHP plan: the order cost is not in the wallet's currency
    buyer, order = await dup.paid_direct(gpt_session(new_marker()))
    await _drive(env, dup, order, buyer)
    admin, _ = await env.login("admin")
    body = (await admin.get("/api/v1/admin/procurement/cost/margin")).json()
    row = next(i for i in body["items"] if i["order_id"] == order["id"])
    assert row["cost_incomplete"] is True and row["margin"] is None


# ------------------------------------------------------------------ the low findings
async def test_direct_refund_after_success_needs_an_explicit_acknowledgement(env, dup):
    from tests.test_aftersales import open_ticket

    vendor = _vendor()
    await _zovo(dup, vendor, CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    t = await open_ticket(buyer, order)
    admin, _ = await env.login("admin")
    body = {"action": "refund", "note": "r", "reference": "REF-" + uuid.uuid4().hex[:8]}
    r = await admin.post(f"/api/v1/admin/after-sales/{t.json()['id']}/action", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "direct_refund_ack_required"
    r = await admin.post(
        f"/api/v1/admin/after-sales/{t.json()['id']}/action",
        json={**body, "acknowledge_irreversible": True},
    )
    assert r.status_code == 200, r.text


async def test_resupply_must_be_the_same_account(env, dup):
    vendor = _vendor()
    vendor.direct_current_plan = "plus"  # plan_already_active: a held job with a recorded mask
    await _zovo(dup, vendor, GPT)
    buyer, order = await dup.paid_direct(gpt_session(new_marker()))
    await dup.run()
    await env.drain()
    job = await _job(dup, order)
    assert job.account_mask == "b***@example.test"
    await _sql(env, "DELETE FROM procurement_credentials")
    other = gpt_session(new_marker(), email="someone.else@example.org")  # masks to s***@...
    r = await buyer.post(f"/api/v1/orders/{order['id']}/direct-session", json={"session": other})
    assert r.status_code == 409 and r.json()["error"]["code"] == "session_other_account"
    assert await _creds(env, order) == 0


async def test_a_direct_product_cannot_be_bought_on_a_reseller_storefront(env, dup):
    from types import SimpleNamespace

    from app.core.errors import Validation
    from app.modules.orders import service

    await _zovo(dup, _vendor(), CLAUDE)
    buyer = await env.user("buyer")
    async with env.api_session() as db:
        user = await db.merge(buyer)
        with pytest.raises(Validation) as exc:
            await service.buy_now(
                db, env.res.settings, user, dup.product.id, 1, None,
                storefront=SimpleNamespace(id=uuid.uuid4()), session=claude_session("A"),
                cred_sealer=env.res.cred_sealer,
            )  # fmt: skip
    assert exc.value.code == "direct_not_on_storefront"
    assert await env.scalar("SELECT count(*) FROM orders") == 0


# ------------------------------------------------------------------ S-M1: deletion without procurement
async def test_the_sweep_runs_first_so_a_failing_pass_cannot_skip_it(env, dup, monkeypatch):
    from app.procurement import engine

    await _zovo(dup, _vendor(), CLAUDE)
    _buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _sql(env, "UPDATE procurement_credentials SET created_at = now() - interval '7 hours'")

    async def boom(_ctx):
        raise RuntimeError("a job pass failed")

    monkeypatch.setattr(engine, "process_outbox_one", boom)
    with pytest.raises(RuntimeError):
        await engine.run_once(dup.ctx)
    assert await _creds(env, order) == 0  # the sweep ran BEFORE the failing pass


async def test_the_api_backstop_deletes_overdue_sessions_alerts_and_pauses_checkout(env, dup):
    from app.modules.payments.reconcile import purge_overdue_sessions

    await _zovo(dup, _vendor(), CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _sql(env, "UPDATE procurement_credentials SET created_at = now() - interval '7 hours'")
    async with env.api_session() as db:
        n = await purge_overdue_sessions(env.res, db, env.tenant_id)
    assert n == 1 and await _creds(env, order) == 0
    assert await env.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'procurement.sessions_overdue' "
        "AND severity = 'critical'"
    ) == 1  # fmt: skip
    # procurement is evidently not cleaning up: no new session is taken for an hour
    r = await buyer.post(
        "/api/v1/orders",
        json={
            "product_id": str(dup.product.id),
            "qty": 1,
            "session": claude_session("A" + new_marker()),
        },
    )
    assert r.status_code == 503 and r.json()["error"]["code"] == "direct_unavailable"
    await env.res.redis.delete("proc:sessions-overdue")
    # a row older than ceiling + margin pauses checkout even without the marker
    _b, o2 = await dup.paid_direct(claude_session(new_marker()))
    await _sql(
        env, "UPDATE procurement_credentials SET created_at = now() - interval '6 hours 40 minutes'"
    )
    r = await buyer.post(
        "/api/v1/orders",
        json={
            "product_id": str(dup.product.id),
            "qty": 1,
            "session": claude_session("B" + new_marker()),
        },
    )
    assert r.status_code == 503 and o2


def test_the_credential_max_age_cannot_exceed_the_database_ceiling():
    from app.procurement.config import ProcurementSettings

    with pytest.raises(ValueError):
        ProcurementSettings.from_env({"APP_CRED_MAX_AGE_S": "30000"})
    assert ProcurementSettings.from_env({"APP_CRED_MAX_AGE_S": "600"}).cred_max_age_s == 600.0


# ------------------------------------------------------------------ S-L1: no sandbox in production
async def test_a_sandbox_supplier_never_takes_a_real_session_in_production(env, dup):
    vendor = _vendor()
    sid, _ = await _zovo(dup, vendor, CLAUDE)  # an env='sandbox' supplier
    prod = env.res.settings.model_copy(update={"env": "prod"})
    old = env.res.settings
    env.res.settings = prod
    try:
        admin, _ = await env.login("admin")
        r = await admin.post(
            "/api/v1/admin/procurement/sources",
            json={"product_id": str(dup.product.id), "supplier_id": str(sid),
                  "params": {**CLAUDE, "plan": "max_5x"}, "cost_estimate": "120", "enabled": True},
        )  # fmt: skip
        assert r.status_code == 422 and r.json()["error"]["code"] == "sandbox_in_prod", r.text
        buyer, _ = await env.login("buyer")  # checkout: no usable (non-sandbox) source
        r = await buyer.post(
            "/api/v1/orders",
            json={"product_id": str(dup.product.id), "qty": 1, "session": claude_session("A")},
        )
        assert r.status_code == 409 and r.json()["error"]["code"] == "out_of_stock"
    finally:
        env.res.settings = old
    # the engine: the same refusal at the lease, nothing sent
    buyer2, order = await dup.paid_direct(claude_session(new_marker()))
    dup.ctx.settings = replace(dup.ctx.settings, env="prod")
    await dup.run()
    job = await _job(dup, order)
    assert job.state in ("needs_manual", "queued")
    assert vendor.preflights == 0 and vendor.direct_executions == 0 and not job.maybe_sent
    assert buyer2 is not None


# ------------------------------------------------------------------ S-L2: the stronger sweep
def _variants(marker: str, email: str, token: str) -> list[str]:
    b64 = lambda s: base64.b64encode(s.encode()).decode()  # noqa: E731
    return [
        marker, email, token, b64(marker), b64(token), b64(marker).rstrip("="),
        base64.urlsafe_b64encode(token.encode()).decode().rstrip("="),
        urllib.parse.quote(email, safe=""), urllib.parse.quote(token, safe=""),
    ]  # fmt: skip


async def test_session_never_appears_anywhere_every_tenant_every_encoding(env, dup, caplog):
    import logging

    import asyncpg

    caplog.set_level(logging.DEBUG)
    marker = new_marker()
    local = f"real.person.{marker.lower()}"
    token = f"{marker}-session-token"
    needles = _variants(marker, f"{local}@example.com", token)
    vendor = _vendor()
    vendor.force("/gpt-direct/orders", "lost_response")
    await _zovo(dup, vendor, GPT)
    buyer, _u = await env.login("buyer")
    session = gpt_session(marker, email=f"{local}@example.com")
    order = await buy(buyer, dup.product, 1, session=session)
    await pay_order(env, buyer, order)
    await env.drain()
    await _drive(env, dup, order, buyer)
    # negative requests: wrong JSON type, malformed JSON, oversize; and the re-supply path
    bodies = []
    for payload in (
        {"product_id": str(dup.product.id), "qty": 1, "session": 12345},
        {"product_id": str(dup.product.id), "qty": 1, "session": {"x": session}},
        {"product_id": str(dup.product.id), "qty": 1, "session": session + "x" * 70_000},
    ):
        bodies.append((await buyer.post("/api/v1/orders", json=payload)).text)
    raw = (
        '{"product_id": "' + str(dup.product.id) + '", "qty": 1, "session": "' + session
    ).encode()
    bodies.append(
        (await buyer.post("/api/v1/orders", content=raw,
                          headers={"content-type": "application/json"})).text
    )  # fmt: skip
    r = await buyer.post(
        f"/api/v1/orders/{order['id']}/direct-session", json={"session": session}
    )  # not needed: the order is delivered
    bodies.append(r.text)
    for i, body in enumerate(bodies):
        assert not [n for n in needles if n in body], f"negative request {i}"
    # EVERY row of EVERY tenant, NULL-tenant rows included: a superuser connection (RLS bypassed)
    import os

    dsn = os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    from tests.conftest import TEST_DB

    base, _, _db = dsn.rpartition("/")
    conn = await asyncpg.connect(f"{base}/{TEST_DB}", timeout=5)
    try:
        names = [
            r["table_name"]
            for r in await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        ]
        for name in names:
            dump = await conn.fetchval(
                f"SELECT coalesce(string_agg(row_to_json(t)::text, '|'), '') FROM \"{name}\" t"  # noqa: S608
            )
            assert not [n for n in needles if n in dump], f"table {name}"
        nulls = await conn.fetchval("SELECT count(*) FROM audit_log WHERE tenant_id IS NULL")
        assert nulls is not None  # (those rows were part of the dump above)
    finally:
        await conn.close()
    redis = env.s.redis
    blob = []
    async for key in redis.scan_iter(match="*", count=500):
        blob.append(key.decode() if isinstance(key, bytes) else key)
        t = await redis.type(key)
        t = t.decode() if isinstance(t, bytes) else t
        if t == "string":
            blob.append(str(await redis.get(key)))
        elif t == "hash":
            blob.append(str(await redis.hgetall(key)))
        elif t == "list":
            blob.append(str(await redis.lrange(key, 0, -1)))
    text_all = "\n".join(blob) + "\n" + caplog.text
    assert not [n for n in needles if n in text_all]
    assert _order_of(vendor)["cred"]  # the vendor, and only the vendor, got it


async def test_resupply_path_leaks_nothing(env, dup, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    marker = new_marker()
    vendor = _vendor()
    await _zovo(dup, vendor, CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _sql(env, "DELETE FROM procurement_credentials")
    await dup.run()
    fresh = claude_session(marker)
    r = await buyer.post(f"/api/v1/orders/{order['id']}/direct-session", json={"session": fresh})
    assert r.status_code == 200 and marker not in r.text
    admin, _ = await env.login("admin")
    job = await _job(dup, order)
    await admin.post(f"{JOBS}/{job.id}/retry", json={})
    await _drive(env, dup, order, buyer)
    assert marker not in caplog.text
    for name in ("audit_log", "outbox", "procurement_jobs", "orders"):
        dump = await env.scalar(
            f"SELECT coalesce(string_agg(row_to_json(t)::text, '|'), '') FROM {name} t"  # noqa: S608
        )
        assert marker not in dump, name


async def test_scenarios_mock_seeds_the_funding_card_the_smoke_uses(env):
    """e2e/smoke.py pays its direct order with card_id 1 of the simulator (scenarios mode)."""
    from app.suppliers.base import OfferSpec
    from tests.test_procurement_vcc_audit import _adapter

    vendor = MockZovo(scenarios=True)
    sup = _adapter(vendor)
    params = {"product": "claude", "plan": "pro", "card_id": 1}
    av = await sup.availability(OfferSpec(kind="direct", params=params))
    assert av.available, av.reason
    assert not MockZovo().cards  # only scenarios mode seeds it


async def test_a_hand_pasted_card_id_and_a_matching_mask_both_block_a_funding_card(env, dup):
    from app.procurement.funding import funding_card_clash
    from app.suppliers.zovocard.webhook import mask_pan
    from tests.test_procurement_vcc import vup  # noqa: F401

    vendor = _vendor()
    sid, _ = await _zovo(dup, vendor, CLAUDE)
    # (1) a card job pasted by hand WITH the vendor card id records it, whatever its supplier
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
            {"i": oid, "t": env.tenant_id, "n": "G" + uuid.uuid4().hex[:12],
             "b": buyer0.id, "p": p.id},
        )  # fmt: skip
        await db.execute(
            text(
                "INSERT INTO procurement_jobs (id, tenant_id, order_id, unit_no, kind, state, "
                "step, idempotency_key) VALUES (:j, :t, :o, 1, 'vcc', 'needs_manual', 'submit', :k)"
            ),
            {"j": vid, "t": env.tenant_id, "o": oid, "k": "pj:" + vid.hex + ":submit"},
        )
        await db.commit()
    admin, _ = await env.login("admin")
    r = await admin.post(
        f"{JOBS}/{vid}/fulfil",
        json={"code": "4242424242424242----09/32----456", "card_id": "4242",
              "reason": "pasted by hand"},
    )  # fmt: skip
    assert r.status_code == 200, r.text
    ref = await env.scalar("SELECT upstream_ref FROM procurement_jobs WHERE id = :j", j=vid)
    assert ref == "4242"  # a hand-pasted card with no supplier still records its vendor id
    async with env.api_session() as db:
        assert await funding_card_clash(db, env.tenant_id, sid, 4242)
    # the source is refused at save too
    r = await admin.post(
        "/api/v1/admin/procurement/sources",
        json={"product_id": str(dup.product.id), "supplier_id": str(sid),
              "params": {**CLAUDE, "plan": "max_5x"}, "cost_estimate": "120", "enabled": False},
    )  # fmt: skip
    assert r.status_code == 422 and r.json()["error"]["code"] == "funding_card_invalid"
    # (2) a card whose NUMBER matches a live card job's mask is never a funding card either,
    # even when its vendor id was never recorded
    mask = mask_pan(vendor.cards[4242]["card_number"])
    await _sql(
        env,
        "UPDATE procurement_jobs SET upstream_ref = NULL, card_mask = :m WHERE kind = 'vcc'",
        m=mask,
    )
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "no_source:plan_unavailable"
    assert not job.maybe_sent and vendor.direct_executions == 0
    assert buyer is not None


# ------------------------------------------------------------------ [V] real sandbox preflight shapes
def _adapter_env(vendor, env_name="sandbox"):
    import httpx

    from app.devtools.mock_zovocard import OK_KEY, create_mock_zovocard
    from app.suppliers.zovocard import TokenBucket, ZovoCardSupplier, ZovoClient, ZovoConfig

    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_mock_zovocard(vendor)))
    return ZovoCardSupplier(
        "zovocard",
        ZovoClient(
            ZovoConfig(base_url="http://m/openapi/v1", app_secret=OK_KEY, env=env_name,
                       allow_production_writes=True),
            http=http, bucket=TokenBucket(6000, 100),
        ),
    )  # fmt: skip


def _direct_spec(params=CLAUDE):
    from app.suppliers.base import JobSpec, OfferSpec

    jid = "aa" * 16
    return JobSpec(
        job_id=jid, step="sent", idempotency_key=f"pj:{jid}:submit",
        offer=OfferSpec(kind="direct", params=params), credential=claude_session("A")
        if params is CLAUDE else gpt_session(new_marker()),
    )  # fmt: skip


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"blocking_reason": "account_locked"}, "preflight_blocked"),
        ({"can_purchase_at": "2999-01-01T00:00:00Z"}, "purchase_not_yet"),
        ({"subscription_recovery_required": True}, "subscription_recovery_required"),
        ({"subscription_has_active": True}, "subscription_active"),
    ],
)
async def test_the_accounts_own_preflight_state_blocks_the_order(over, code):
    vendor = _vendor()
    vendor.preflight_over = over
    res = await _adapter_env(vendor).submit(_direct_spec())
    assert res.outcome.value == "fatal" and res.error_code == code
    assert vendor.direct_executions == 0 and not _posts(vendor, "/gpt-direct/orders")


async def test_a_sandbox_that_never_quotes_does_not_fail_every_order():
    vendor = _vendor()
    vendor.direct_quotes_null = True  # [V] ``quotes: null``
    sandbox = await _adapter_env(vendor, "sandbox").submit(_direct_spec(GPT))
    assert sandbox.outcome.value == "accepted_pending" and vendor.direct_executions == 1
    # production: no quote means no bound check, so no order
    vendor2 = _vendor()
    vendor2.direct_quotes_null = True
    prod = await _adapter_env(vendor2, "production").submit(_direct_spec(GPT))
    assert prod.outcome.value == "fatal" and prod.error_code == "quote_missing"
    assert vendor2.direct_executions == 0
    # Claude / Grok have a constant catalogue price: still bound-checked without a quote
    vendor3 = _vendor()
    vendor3.direct_quotes_null = True
    ok = await _adapter_env(vendor3, "production").submit(_direct_spec(CLAUDE))
    assert ok.outcome.value == "accepted_pending"


async def test_a_busy_cooling_or_deleted_funding_card_is_not_used_now():
    from app.suppliers.base import OfferSpec

    vendor = _vendor()
    sup = _adapter_env(vendor)
    spec = OfferSpec(kind="direct", params=CLAUDE)
    vendor.usage_busy.add(4242)
    av = await sup.availability(spec)
    assert not av.available and av.reason == "funding_card_busy"
    vendor.usage_busy.clear()
    vendor.cards[4242]["status"] = "DELETED"
    av = await sup.availability(spec)
    assert not av.available and av.reason == "funding_card_invalid"


async def test_a_watched_held_order_still_occupies_its_funding_card(env, dup):
    vendor = _vendor()
    vendor.direct_end = "running"
    await _zovo(dup, vendor, CLAUDE)
    _b1, o1 = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    await _sql(env, "UPDATE procurement_jobs SET first_sent_at = now() - interval '3 hours'")
    await _settle(dup, 2)
    j1 = await _job(dup, o1)
    assert j1.state == "needs_manual" and j1.next_poll_at is not None  # watched
    _b2, o2 = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    j2 = next(j for j in await dup.jobs(o2["id"]))
    assert j2.state == "queued" and j2.error_code == "card_busy" and not j2.maybe_sent
    assert vendor.direct_executions == 1
    # a park is a stuck job after 30 minutes, like the balance park
    await _sql(env, "UPDATE procurement_jobs SET created_at = now() - interval '2 hours' "
                    "WHERE id = :j", j=j2.id)  # fmt: skip
    await _sql(env, "UPDATE procurement_jobs SET next_poll_at = now() - interval '1 second' "
                    "WHERE id = :j", j=j2.id)  # fmt: skip
    await dup.run()
    assert (await _job_by_id(dup, j2.id)).alerted_at is not None


async def _job_by_id(dup, jid):
    return next(j for j in await dup.jobs() if j.id == jid)


async def test_refund_of_an_order_with_a_live_supplier_order_needs_the_acknowledgement(env, dup):
    vendor = _vendor()
    await _zovo(dup, vendor, CLAUDE)
    _b, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()  # accepted_pending: the supplier holds an order that may complete
    admin, _ = await env.login("admin")
    body = {"reference": "REF-" + uuid.uuid4().hex[:8].upper()}
    r = await admin.post(f"/api/v1/admin/orders/{order['id']}/refund", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "direct_refund_ack_required"
    r = await admin.post(
        f"/api/v1/admin/orders/{order['id']}/refund",
        json={**body, "acknowledge_irreversible": True},
    )
    assert r.status_code == 200, r.text
    # a job that never reached the supplier needs none
    _b2, o2 = await dup.paid_direct(claude_session(new_marker()))
    r = await admin.post(
        f"/api/v1/admin/orders/{o2['id']}/refund",
        json={"reference": "REF-" + uuid.uuid4().hex[:8].upper()},
    )
    assert r.status_code == 200, r.text


def test_the_engine_sweep_default_is_inside_the_database_backstop():
    from app.procurement.config import ProcurementSettings

    assert ProcurementSettings().cred_max_age_s == 18000.0 < 21600.0


async def test_a_completed_order_dearer_than_its_budget_is_a_critical_row(env, dup):
    vendor = _vendor()
    await _zovo(dup, vendor, CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    # the order is within budget when sent (USD 20 <= 27.50) but is booked dearer when it completes
    vendor.direct_orders[next(iter(vendor.direct_orders))]["amount"] = 3000  # USD 30
    await _drive(env, dup, order, buyer)
    row = (
        await _rows(
            env,
            "SELECT detail->>'cost', detail->>'bound' FROM audit_log "
            "WHERE action = 'procurement.cost_over_bound' AND severity = 'critical'",
        )
    )[0]
    assert row[0] == "30.00" and row[1].startswith("27.5")
    from app.modules.payments.reconcile import alert_declining_offers

    async with env.api_session() as db:
        assert await alert_declining_offers(env.res, db, env.tenant_id, datetime.now(timezone.utc))


async def test_a_late_completion_after_the_order_was_refunded_is_flagged_not_delivered(env, dup):
    vendor = _vendor()
    vendor.direct_end = "running"
    await _zovo(dup, vendor, CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    await _sql(env, "UPDATE procurement_jobs SET first_sent_at = now() - interval '3 hours'")
    await _settle(dup, 2)
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.next_poll_at is not None  # watched
    # the order leaves the open states while the job is still watched (e.g. closed by staff)
    await _sql(env, "UPDATE orders SET status = 'refunded' WHERE id = :o", o=uuid.UUID(order["id"]))
    vendor.direct_end = "completed"
    for _ in range(3):
        await _sql(env, "UPDATE procurement_jobs SET next_poll_at = now() - interval '1 second' "
                        "WHERE state = 'needs_manual'")  # fmt: skip
        await dup.run()
    job = await _job(dup, order)
    assert job.state == "fulfilled" and job.error_code == "refunded_after_success"
    assert not job.card_stock_ids  # nothing was sealed or delivered
    row = (
        await _rows(
            env,
            "SELECT severity, (detail->>'late') FROM audit_log "
            "WHERE action = 'procurement.direct_refunded_after_success'",
        )
    )[0]
    assert tuple(row) == ("critical", "true")
    assert await _creds(env, order) == 0 and buyer is not None


def test_only_the_vendors_real_sandbox_gets_orders_without_a_preflight_token():
    """Verified 2026-11-16: sandbox.zovocard.com refuses an order carrying a preflight token."""
    from app.suppliers.zovocard import direct as dr

    assert dr.omit_preflight_token("https://sandbox.zovocard.com/openapi/v1", "sandbox")
    assert not dr.omit_preflight_token("https://zovocard.com/openapi/v1", "production")
    assert not dr.omit_preflight_token("https://sandbox.zovocard.com/openapi/v1", "production")
    assert not dr.omit_preflight_token("http://mockzovo:8005/openapi/v1", "sandbox")
