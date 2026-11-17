"""Payment-audit and security-review fixes on v12 M3 ``upstream_vcc`` (2026-11-16)."""

from __future__ import annotations

# ruff: noqa: F811  (fixtures are imported from the M2 / vcc tests)
import uuid

import pytest  # noqa: F401

from app.devtools.mock_zovocard import MockZovo
from tests.test_aftersales import open_ticket
from tests.test_procurement_m2 import Up, _status, up  # noqa: F401
from tests.test_procurement_vcc import (
    OPEN,
    WITH_RECHARGE,
    _card,
    _deliver,
    _job,
    _posts,
    _rows,
    _sql,
    _zovo,
    vup,  # noqa: F401
)


async def _refund(env, order):
    admin, _ = await env.login("admin")
    return await admin.post(
        f"/api/v1/admin/orders/{order['id']}/refund",
        json={"reference": "REF-" + uuid.uuid4().hex[:8].upper()},
    )


async def _settle(vup: Up, rounds: int = 6) -> None:
    for _ in range(rounds):
        await vup.make_due()
        await vup.run()


# ------------------------------------------------------------------ F1


async def test_refund_of_a_delivered_card_order_deletes_the_card(env, vup):
    vendor = MockZovo()
    await _zovo(vup, vendor, OPEN)
    buyer, order = await vup.paid_order()
    await _deliver(env, vup, order, buyer)
    assert _card(vendor)["status"] == "ACTIVE"
    t = await open_ticket(buyer, order)
    assert t.status_code in (200, 201), t.text
    admin, _ = await env.login("admin")
    r = await admin.post(
        f"/api/v1/admin/after-sales/{t.json()['id']}/action",
        json={"action": "refund", "note": "refunded", "reference": "REF-" + uuid.uuid4().hex[:8]},
    )
    assert r.status_code == 200, r.text
    job = await _job(vup, order)
    assert job.state == "void_pending"
    await _settle(vup)
    job = await _job(vup, order)
    assert job.state == "voided" and job.upstream_status == "deleted"
    assert _card(vendor)["status"] == "DELETED"
    assert await env.scalar(
        "SELECT count(*) FROM card_stock WHERE order_id = :o AND status = 'void'",
        o=uuid.UUID(order["id"]),
    ) == 1  # fmt: skip
    assert await env.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'procurement.delivered_card_voided' "
        "AND severity = 'critical'"
    ) == 1  # fmt: skip
    # the buyer can no longer reveal the dead card
    assert (await buyer.post(f"/api/v1/orders/{order['id']}/reveal")).status_code in (409, 403)


# ------------------------------------------------------------------ adapter-level helpers


def _adapter(vendor: MockZovo):
    import httpx

    from app.devtools.mock_zovocard import OK_KEY, create_mock_zovocard
    from app.suppliers.zovocard import TokenBucket, ZovoCardSupplier, ZovoClient, ZovoConfig

    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_mock_zovocard(vendor)))
    return ZovoCardSupplier(
        "zovocard",
        ZovoClient(
            ZovoConfig(base_url="http://m/openapi/v1", app_secret=OK_KEY),
            http=http,
            bucket=TokenBucket(6000, 100),
        ),
    )


def _spec(params: dict, *, ref: str | None = None, step: str = "sent", key: str | None = None):
    from app.suppliers.base import JobSpec, OfferSpec

    jid = "ab" * 16
    return JobSpec(
        job_id=jid,
        step=step,
        idempotency_key=key or f"pj:{jid}:submit",
        offer=OfferSpec(kind="vcc", params=params),
        upstream_ref=ref,
    )


# ------------------------------------------------------------------ F2


async def test_open_is_posted_once_even_across_pending_polls(env, vup):
    vendor = MockZovo()
    vendor.recharge_pending_next = 1
    await _zovo(vup, vendor, WITH_RECHARGE)
    buyer, order = await vup.paid_order()
    await _deliver(env, vup, order, buyer)
    assert len(_posts(vendor, "/cards/open")) == 1  # the polls continued on the recorded card
    assert vendor.card_opens == 1


async def test_a_second_card_for_one_job_is_never_adopted(env, vup):
    """The supplier answers with another card id than the recorded one: nothing is sealed."""
    from app.suppliers.base import Outcome, Result
    from app.suppliers.mock import MockSupplier

    class TwoCards(MockSupplier):
        async def submit(self, job):
            return Result(Outcome.AMBIGUOUS, error_code="timeout_after_send", upstream_ref="111")

        async def query(self, job):
            return Result(
                Outcome.OK, upstream_ref="222", secrets=("4111111111111111----12/30----123",)
            )

    mock = TwoCards("mock_a")
    await vup.supplier(mock, params={"product_code": "MOCKCARD", "init_amount": "20"})
    _buyer, order = await vup.paid_order()
    for _ in range(4):
        await vup.run()
        await vup.make_due()
    job = await _job(vup, order)
    assert job.state == "needs_manual" and job.error_code == "card_mismatch"
    assert job.upstream_ref == "111" and job.maybe_sent and not job.card_stock_ids
    detail = await env.scalar(
        "SELECT detail::text FROM audit_log WHERE action = 'procurement.card_mismatch' "
        "AND severity = 'critical'"
    )
    assert '"recorded": "111"' in detail and '"answered": "222"' in detail
    assert await env.scalar("SELECT count(*) FROM card_stock WHERE order_id IS NOT NULL") == 0


async def test_adapter_never_opens_when_the_card_is_known():
    vendor = MockZovo()
    sup = _adapter(vendor)
    first = await sup.submit(_spec(OPEN))
    ref = first.upstream_ref
    assert vendor.card_opens == 1
    # a replay under a NEW key with the card id recorded must not buy another card
    again = await sup.query(_spec(OPEN, ref=ref, key="pj:" + "cd" * 16 + ":submit"))
    assert again.upstream_ref == ref and vendor.card_opens == 1
    assert len(_posts(vendor, "/cards/open")) == 1


# ------------------------------------------------------------------ F5 / F7 / F8


async def test_balance_witness_active_only_and_short_cards():
    vendor = MockZovo()
    sup = _adapter(vendor)
    res = await sup.submit(_spec(OPEN))
    ref = res.upstream_ref
    card = vendor.cards[int(ref)]
    card["available_amount"] = 5.0  # the card holds less than the offer promises
    short = await sup.query(_spec(OPEN, ref=ref))
    assert short.outcome.value == "fatal" and short.error_code == "balance_short"
    assert not short.secrets
    card["available_amount"] = 20.0
    card["status"] = "FROZEN"
    frozen = await sup.query(_spec(OPEN, ref=ref))
    assert frozen.error_code == "card_not_active" and not frozen.secrets
    card["status"] = "ACTIVE"
    ok = await sup.query(_spec(OPEN, ref=ref))
    assert ok.outcome.value == "ok" and len(ok.secrets) == 1


async def test_topup_is_skipped_when_the_card_already_holds_it():
    vendor = MockZovo()
    sup = _adapter(vendor)
    res = await sup.submit(_spec(WITH_RECHARGE))
    assert (
        vendor.card_recharges == 1 and vendor.cards[int(res.upstream_ref)]["available_amount"] == 30
    )
    # the recharge list is lost / empty but the balance proves it ran: no second POST, ever
    vendor.recharges[int(res.upstream_ref)].clear()
    again = await sup.query(_spec(WITH_RECHARGE, ref=res.upstream_ref))
    assert again.outcome.value == "ok" and vendor.card_recharges == 1


async def test_void_waits_while_a_recharge_is_in_flight_then_deletes():
    vendor = MockZovo()
    vendor.recharge_pending_next = 1
    sup = _adapter(vendor)
    res = await sup.submit(_spec(WITH_RECHARGE))
    assert res.outcome.value == "accepted_pending"
    ref = res.upstream_ref
    first = await sup.void(
        _spec(WITH_RECHARGE, ref=ref, step="void", key="pj:" + "ab" * 16 + ":void")
    )
    assert first.outcome.value == "accepted_pending" and first.error_code == "recharge_pending"
    assert not [1 for m, p, _h in vendor.requests if m == "DELETE"]
    # the list read above counted as a poll: the recharge settles, then the delete goes out
    done = await sup.void(
        _spec(WITH_RECHARGE, ref=ref, step="void", key="pj:" + "ab" * 16 + ":void")
    )
    assert done.outcome.value == "ok" and vendor.cards[int(ref)]["status"] == "DELETED"


async def test_a_coded_400_on_recharge_is_never_pending(env, vup):
    vendor = MockZovo()
    await _zovo(vup, vendor, {**OPEN, "recharge_amount": "5"})  # amount_below_min (coded 400)
    _buyer, order = await vup.paid_order()
    await vup.run()
    job = await _job(vup, order)
    assert job.state == "needs_manual" and job.error_code == "amount_below_min"
    assert vendor.card_recharges == 0


# ------------------------------------------------------------------ F3 / F4


async def test_pending_delete_is_sent_once_and_then_only_read(env, vup):
    vendor = MockZovo()
    vendor.delete_pending_next = 1
    await _zovo(vup, vendor, OPEN)
    _buyer, order = await vup.paid_order()
    await vup.run()
    admin, _ = await env.login("admin")
    r = await admin.post(
        f"/api/v1/admin/orders/{order['id']}/refund",
        json={"reference": "REF-" + uuid.uuid4().hex[:8]},
    )
    assert r.status_code == 200
    await _settle(vup, 8)
    job = await _job(vup, order)
    assert job.state == "voided" and _card(vendor)["status"] == "DELETED"
    assert len([1 for m, p, _h in vendor.requests if m == "DELETE"]) == 1


async def test_a_refused_void_needs_staff_instead_of_going_back_to_fulfilled(env, vup):
    vendor = MockZovo()
    await _zovo(vup, vendor, OPEN)
    _buyer, order = await vup.paid_order()
    await vup.run()
    job = await _job(vup, order)
    vendor.force(f"/cards/{job.upstream_ref}", "delete_disabled", times=1)  # a FATAL code
    admin, _ = await env.login("admin")
    await admin.post(
        f"/api/v1/admin/orders/{order['id']}/refund",
        json={"reference": "REF-" + uuid.uuid4().hex[:8]},
    )
    await _settle(vup, 4)
    job = await _job(vup, order)
    assert job.state == "needs_manual" and job.error_code == "void_failed"
    assert await env.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'procurement.void_failed' "
        "AND severity = 'critical'"
    ) == 1  # fmt: skip
    # staff can retry the void from there
    assert "void" in (await admin.get(f"/api/v1/admin/procurement/jobs/{job.id}")).json()["actions"]


async def test_cdk_void_failure_also_ends_in_needs_manual(env, up):
    from app.suppliers.base import Outcome, Result
    from app.suppliers.mock import MockSupplier

    class NoVoid(MockSupplier):
        async def void(self, job):
            return Result(Outcome.FATAL, error_code="CDK_DISABLE_REJECTED")

    mock = NoVoid("mock_a")
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()
    r = await _refund(env, order)
    assert r.status_code == 200
    await _settle(up, 3)
    (job,) = await up.jobs(order["id"])
    assert job.state == "needs_manual" and job.error_code == "void_failed"


# ------------------------------------------------------------------ S1 / S2 / F6

JOBS = "/api/v1/admin/procurement/jobs"
PASTED = "4242424242424242----09/32----456"


async def test_card_fingerprint_does_not_derive_from_the_secret(env, vup):
    vendor = MockZovo()
    await _zovo(vup, vendor, OPEN)
    buyer, order = await vup.paid_order()
    await _deliver(env, vup, order, buyer)
    job = await _job(vup, order)
    row = (
        await _rows(
            env,
            "SELECT fingerprint, lookup_fp FROM card_stock WHERE order_id = :o",
            o=uuid.UUID(order["id"]),
        )
    )[0]
    sealer, tid = vup.ctx.sealer, str(env.tenant_id)
    card = _card(vendor)
    secret = "----".join((card["card_number"], card["expire"], card["cvv"]))
    assert row.lookup_fp is None  # no PAN-derived search key at all
    assert row.fingerprint == sealer.card_fingerprint(tid, job.upstream_ref)  # the card id
    # none of the secret-derived keys an API holder could compute matches
    assert row.fingerprint not in {
        sealer.fingerprint(tid, secret),
        sealer.fingerprint(tid, card["card_number"]),
        sealer.lookup_fingerprint(tid, card["card_number"]),
        sealer.lookup_fingerprint(tid, secret),
    }
    # the staff "find by content" cannot locate a card by its secret
    staff, _ = await env.login("admin")
    for by, lines in (("content", [secret]), ("first_field", [card["card_number"]])):
        r = await staff.post("/api/v1/admin/cards/find", json={"lines": lines, "by": by})
        assert r.status_code == 200, r.text
        assert r.json()["items"] == [] and r.json()["missing"] == 1


async def test_hand_pasted_card_fingerprint_and_admin_only(env, vup):
    vendor = MockZovo()
    vendor.force("/cards/open", "401", times=3)
    await _zovo(vup, vendor, OPEN)
    _buyer, order = await vup.paid_order()
    await vup.run()
    job = await _job(vup, order)
    assert job.state == "needs_manual"
    agent, _ = await env.login("agent")
    assert (await agent.post(f"{JOBS}/{job.id}/fulfil", json={"code": PASTED})).status_code == 403
    admin, _ = await env.login("admin")
    # a reason may not carry a card number, for ANY job kind and action
    for path, body in (
        ("fulfil", {"code": PASTED, "reason": "paid with 4242 4242 4242 4242"}),
        ("fail", {"reason": "card 4242424242424242 broken"}),
        ("void", {"reason": "card 4242-4242-4242-4242"}),
    ):
        r = await admin.post(f"{JOBS}/{job.id}/{path}", json=body)
        assert r.status_code == 422, path
    r = await admin.post(f"{JOBS}/{job.id}/fulfil", json={"code": PASTED, "reason": "ticket 8841"})
    assert r.status_code == 200, r.text
    row = (
        await _rows(
            env,
            "SELECT fingerprint, lookup_fp FROM card_stock WHERE order_id = :o",
            o=uuid.UUID(order["id"]),
        )
    )[0]
    sealer, tid = vup.ctx.sealer, str(env.tenant_id)
    assert row.lookup_fp is None
    assert row.fingerprint == sealer.card_fingerprint(tid, f"manual:{order['id']}:1:4242")
    assert row.fingerprint != sealer.fingerprint(tid, PASTED)
    assert await env.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'procurement.card_pasted' "
        "AND actor_id IS NOT NULL AND row_to_json(audit_log)::text NOT LIKE '%4242424242424242%'"
    ) == 1  # fmt: skip


async def test_pasting_another_card_than_the_vendors_is_flagged(env, vup):
    vendor = MockZovo()
    vendor.recharge_pending_next = 99  # the job parks with a known vendor card
    await _zovo(vup, vendor, WITH_RECHARGE)
    _buyer, order = await vup.paid_order()
    await vup.run()
    job = await _job(vup, order)
    assert job.state == "accepted_pending" and job.card_mask
    vendor_mask = job.card_mask
    await _sql(env, "UPDATE procurement_jobs SET lease_until = NULL")
    admin, _ = await env.login("admin")
    r = await admin.post(f"{JOBS}/{job.id}/fulfil", json={"code": PASTED})
    assert r.status_code == 200, r.text
    detail = await env.scalar(
        "SELECT detail::text FROM audit_log WHERE action = 'procurement.manual_card_differs' "
        "AND severity = 'critical'"
    )
    assert vendor_mask in detail and "424242****4242" in detail and "4242424242424242" not in detail
    assert r.json()["card_mask"] == "424242****4242"


# ------------------------------------------------------------------ F9 / F10 / F11 / S4


async def test_any_ledger_row_naming_a_card_links_to_its_job(env, vup):
    from app.procurement import ledger
    from app.procurement.service import SupplierRef

    vendor = MockZovo()
    sid, _ = await _zovo(vup, vendor, OPEN)
    buyer, order = await vup.paid_order()
    await _deliver(env, vup, order, buyer)
    job = await _job(vup, order)
    vendor.balance_logs.insert(
        0,
        {"id": 7777, "created_at": "2026-11-16T10:00:00Z", "amount": -0.5, "type": "card_penalty",
         "ref_id": 5, "card_id": int(job.upstream_ref), "card_number": "4111111111111111"},
    )  # fmt: skip
    ref = SupplierRef(sid, env.tenant_id, "zovocard", "zovocard", "sandbox", "ok")
    await ledger.backfill(vup.ctx, ref, "USD")
    rows = await _rows(env, "SELECT type, job_id FROM supplier_ledger WHERE external_id = '7777'")
    assert [(r.type, r.job_id) for r in rows] == [("card_penalty", job.id)]


def test_card_operation_dedup_keeps_cards_apart_and_otp_fields_agree():
    from app.modules.orders.card_otp import extract_otp
    from app.suppliers.zovocard.webhook import normalise

    sec = "s" * 32
    one = {"event": "card_operation", "operation": "delete", "status": "COMPLETE", "card_id": 1}
    two = {**one, "card_id": 2}
    assert normalise(one, sec).dedup_key != normalise(two, sec).dedup_key
    assert normalise(one, sec).dedup_key == normalise(dict(one), sec).dedup_key
    for field in ("otp", "code", "verification_code"):
        ev = {"event": "card_3ds_otp", "card_id": 9, field: "Ab12Cd"}
        assert extract_otp(ev) == ("9", "Ab12Cd")  # the forwarder reads what the MAC reads
        assert normalise(ev, sec).dedup_key.startswith("card_3ds_otp:")
        assert "Ab12Cd" not in normalise(ev, sec).dedup_key


async def test_card_otp_endpoint_is_always_empty(env, vup):
    vendor = MockZovo()
    await _zovo(vup, vendor, OPEN)
    buyer, order = await vup.paid_order()
    await vup.run()  # bought and sealed; delivery has not run: the order is still paid
    await env.s.redis.set(  # even a code planted in Redis is never served (owner decision)
        f"otp3ds:{order['id']}:1", '{"otp": "Zz9", "card": "x", "unit": 1}', ex=60
    )
    r = await buyer.get(f"/api/v1/orders/{order['id']}/card-otp")
    assert r.status_code == 200 and r.json()["items"] == []


# ------------------------------------------------------------------ card webhooks: forged / foreign


async def _post_op(env, body: dict, secret: str | None = None):
    from tests.test_procurement_m2 import WEBHOOK_SECRET, _signed

    env.res.settings = env.res.settings.model_copy(
        update={"zovocard_sandbox_webhook_secret": WEBHOOK_SECRET}
    )
    raw, h = _signed(body, secret or WEBHOOK_SECRET)
    return await env.client(csrf=False).post("/api/v1/webhooks/zovocard", content=raw, headers=h)


def _op(card_id: str, status: str = "COMPLETE") -> dict:
    return {"event": "card_operation", "operation": "recharge", "operation_id": "R9",
            "card_id": int(card_id), "status": status}  # fmt: skip


async def test_forged_card_operation_is_refused_and_stores_nothing(env, vup):
    await _zovo(vup, MockZovo(), OPEN)
    r = await _post_op(env, _op("5"), secret="whsec_wrong_" + "b" * 32)
    assert r.status_code == 401 and r.content == b""
    assert await env.scalar("SELECT count(*) FROM supplier_events") == 0


async def test_card_operation_for_another_suppliers_or_tenants_card_changes_nothing(env, vup):
    from tests.test_procurement_v12 import _insert_supplier

    vendor = MockZovo()
    vendor.recharge_pending_next = 99
    await _zovo(vup, vendor, WITH_RECHARGE, code="zovocard_b")  # the job's supplier
    # the webhook is attributed to the account coded "zovocard", which holds no such card
    await _zovo(vup, MockZovo(), OPEN, code="zovocard", priority=50)
    _buyer, order = await vup.paid_order()
    await vup.run()
    job = await _job(vup, order)
    assert job.state == "accepted_pending"
    before = (job.upstream_status, job.next_poll_at)
    # a foreign tenant's job that happens to hold the same card id
    foreign = await _insert_supplier(env, env.other_tenant_id)
    from tests.test_procurement_jobs import _foreign_job

    fj = await _foreign_job(env)
    await _sql(
        env,
        "UPDATE procurement_jobs SET kind = 'vcc', upstream_ref = :r, supplier_id = :s, "
        "state = 'accepted_pending' WHERE id = :j",
        r=job.upstream_ref,
        s=foreign,
        j=fj,
    )
    assert (await _post_op(env, _op(job.upstream_ref))).status_code == 200
    await vup.run()
    job = await _job(vup, order)
    assert (job.upstream_status, job.next_poll_at) == before  # not this supplier's card
    status = await env.scalar("SELECT upstream_status FROM procurement_jobs WHERE id = :j", j=fj)
    assert status is None  # nor the other tenant's
    assert await env.scalar("SELECT count(*) FROM supplier_events WHERE processed_at IS NULL") == 0


# ---------------------------------------------------------------- L6 and staff actions on card jobs


async def test_cdk_webhook_under_a_live_lease_does_not_pull_the_poll_forward(env, up):
    from app.procurement.engine import process_outbox_one
    from tests.test_procurement_m2 import LosingMock

    mock = LosingMock("zovocard", pending=True)
    await up.supplier(mock, code="zovocard")
    _buyer, order = await up.paid_order()
    await up.run()
    (job,) = await up.jobs(order["id"])
    assert job.state == "accepted_pending" and job.upstream_ref
    hour = "now() + interval '1 hour'"
    await _sql(
        env,
        f"UPDATE procurement_jobs SET lease_until = {hour}, next_poll_at = {hour}",  # noqa: S608
    )
    (job,) = await up.jobs(order["id"])
    due = job.next_poll_at
    ev = {"type": "cdk.consumed", "event_id": "cdk.consumed:42", "cdk_id": int(
        job.upstream_ref.split(":")[-1] if job.upstream_ref.isdigit() else 0
    ) or job.upstream_ref, "cdk_status": "consumed"}  # fmt: skip
    ev["cdk_id"] = job.upstream_ref
    assert (await _post_op(env, ev)).status_code == 200
    while await process_outbox_one(up.ctx):
        pass
    (job,) = await up.jobs(order["id"])
    assert job.next_poll_at == due  # a live lease is never overtaken (L6)
    assert job.upstream_status == "consumed"
    # without a lease the same kind of hint makes the job due now
    await _sql(env, "UPDATE procurement_jobs SET lease_until = NULL")
    ev2 = {**ev, "event_id": "cdk.reserved:43", "type": "cdk.reserved", "cdk_status": "reserved"}
    assert (await _post_op(env, ev2)).status_code == 200
    while await process_outbox_one(up.ctx):
        pass
    (job,) = await up.jobs(order["id"])
    assert job.next_poll_at < due


async def test_staff_actions_on_card_jobs_never_buy_a_second_card(env, vup):
    vendor = MockZovo()
    await _zovo(vup, vendor, {**OPEN, "recharge_amount": "5"})  # amount_below_min: needs staff
    _buyer, order = await vup.paid_order()
    await vup.run()
    job = await _job(vup, order)
    assert job.state == "needs_manual" and job.upstream_ref and vendor.card_opens == 1
    admin, _ = await env.login("admin")
    url = f"{JOBS}/{job.id}"
    detail = (await admin.get(url)).json()
    assert {"requery", "retry", "void"} <= set(detail["actions"]) and "switch" not in detail[
        "actions"
    ]
    r = await admin.post(f"{url}/switch", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "maybe_charged"
    for action in ("retry", "requery"):
        r = await admin.post(f"{url}/{action}", json={})
        assert r.status_code == 200, r.text
        await vup.run()
        job = await _job(vup, order)
        assert job.state == "needs_manual" and vendor.card_opens == 1  # same card, never a new one
    assert len(_posts(vendor, "/cards/open")) == 1


async def test_staff_actions_on_a_pending_card_job(env, vup):
    vendor = MockZovo()
    vendor.recharge_pending_next = 99
    await _zovo(vup, vendor, WITH_RECHARGE)
    _buyer, order = await vup.paid_order()
    await vup.run()
    job = await _job(vup, order)
    assert job.state == "accepted_pending"
    admin, _ = await env.login("admin")
    url = f"{JOBS}/{job.id}"
    assert "switch" not in (await admin.get(url)).json()["actions"]
    assert (await admin.post(f"{url}/switch", json={})).status_code == 409
    assert (await admin.post(f"{url}/retry", json={})).status_code == 409  # only from needs_manual
    await _sql(env, "UPDATE procurement_jobs SET lease_until = NULL")
    assert (await admin.post(f"{url}/requery", json={})).status_code == 200
    await vup.run()
    assert vendor.card_opens == 1 and vendor.card_recharges == 1


async def test_a_queued_card_job_can_be_switched_but_never_opened_by_the_action(env, vup):
    from app.procurement.engine import process_outbox_one

    vendor = MockZovo()
    await _zovo(vup, vendor, OPEN)
    _buyer, order = await vup.paid_order()
    while await process_outbox_one(vup.ctx):
        pass
    job = await _job(vup, order)
    assert job.state == "queued"
    admin, _ = await env.login("admin")
    detail = (await admin.get(f"{JOBS}/{job.id}")).json()
    assert "switch" in detail["actions"] and detail["card_mask"] is None
    r = await admin.post(f"{JOBS}/{job.id}/switch", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_other_source"
    assert vendor.card_opens == 0


# ------------------------------------------------------------------ S3: the strict sweep


async def _all_dumps(env) -> dict[str, str]:
    """Every public table as ``row_to_json`` text (the tenant-pinned owner view)."""
    names = [
        r[0]
        for r in await _rows(
            env,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'",
        )
    ]
    out: dict[str, str] = {}
    for name in names:
        sql = f"SELECT coalesce(string_agg(row_to_json(t)::text, '|'), '') FROM \"{name}\" t"  # noqa: S608
        out[name] = await env.scalar(sql)
    return out


def _leaks(text: str, pan: str, exp: str, cvv: str) -> list[str]:
    import re

    found = []
    if pan in text:
        found.append("pan")
    if exp in text:
        found.append("expiry")
    if re.search(rf"(?<![0-9]){re.escape(cvv)}(?![0-9])", text):
        found.append("cvv")
    if f"{pan}----{exp}----{cvv}" in text:
        found.append("secret")
    return found


async def test_card_secret_never_leaves_the_vault_strict_sweep(env, vup, caplog):
    import datetime as dt
    import logging

    from tests.test_procurement_m2 import WEBHOOK_SECRET, _signed

    caplog.set_level(logging.DEBUG)
    env.res.settings = env.res.settings.model_copy(
        update={"zovocard_sandbox_webhook_secret": WEBHOOK_SECRET}
    )
    vendor = MockZovo()
    vendor.card_cvv = "7391"  # four digits + a MM/YY with a slash: no ordinary row holds either
    vendor.card_expire = f"11/{(dt.date.today().year + 5) % 100:02d}"
    vendor.force("/cards/open", "lost_response")  # failure path 1: ambiguous after the open
    vendor.malformed_detail = 1  # failure path 2: a card read with the secrets but a broken shape
    sid, _ = await _zovo(vup, vendor, WITH_RECHARGE)
    from tests.helpers import buy, pay_order

    buyer, _user = await env.login("buyer")
    order = await buy(buyer, vup.product, 1)
    pubsub = env.s.redis.pubsub()
    await pubsub.psubscribe("rt:*")
    await pay_order(env, buyer, order)
    await env.drain()
    await _deliver(env, vup, order, buyer)
    job = await _job(vup, order)
    assert job.state == "fulfilled"
    assert [e for e in caplog.records if "malformed" in e.getMessage()] == []  # nothing echoed
    # owner decision 2026-11-17: the reveal is REFUSED; no HTTP exit shows the card
    rev = await buyer.post(f"/api/v1/orders/{order['id']}/reveal")
    assert rev.status_code == 409
    http_bodies = [rev.text, (await buyer.get(f"/api/v1/orders/{order['id']}/card-otp")).text]
    # an OTP forwarded for this card goes through pub/sub as well
    raw, h = _signed({"event": "card_3ds_otp", "otp": "Qw8Er5Ty", "matrix_id": "M1",
                      "card_id": int(job.upstream_ref)})  # fmt: skip
    await env.client(csrf=False).post("/api/v1/webhooks/zovocard", content=raw, headers=h)
    admin, _ = await env.login("admin")
    for url in (
        f"{JOBS}",
        f"{JOBS}/{job.id}",
        f"/api/v1/admin/procurement/sources?product_id={vup.product.id}",
        f"/api/v1/admin/orders/{order['id']}",
        f"/api/v1/orders/{order['id']}",
    ):
        http_bodies.append((await admin.get(url)).text)
    card = _card(vendor)
    pan, exp, cvv = card["card_number"], card["expire"], card["cvv"]
    assert pan not in rev.text and cvv not in rev.text  # refused: the reveal shows nothing
    for i, body in enumerate(http_bodies):
        assert _leaks(body, pan, exp, cvv) == [], f"http response {i}"
    # every public table, row by row: only the sealed blob holds the card
    from app.procurement import ledger
    from app.procurement.service import SupplierRef

    await ledger.backfill(
        vup.ctx, SupplierRef(sid, env.tenant_id, "zovocard", "zovocard", "sandbox", "ok"), "USD"
    )
    for name, text_ in (await _all_dumps(env)).items():
        assert _leaks(text_, pan, exp, cvv) == [], f"table {name}"
    # pub/sub: nothing published carries the card, and the 3DS code is not published at all
    seen: list[str] = []
    while (m := await pubsub.get_message(timeout=0.3)) is not None:
        if m["type"] == "pmessage":
            seen.append(m["data"].decode())
    assert seen and all(_leaks(x, pan, exp, cvv) == [] for x in seen)
    assert not any("card_otp" in x or "Qw8Er5Ty" in x for x in seen)
    await pubsub.aclose()
    assert _leaks(caplog.text, pan, exp, cvv) == []  # logs at DEBUG, incl. both failure paths
    assert "Qw8Er5Ty" not in caplog.text


# ------------------------------------------------------------------ round 3: re-audit notes


async def test_balance_witness_tolerates_fees_the_vendor_reports():
    vendor = MockZovo()
    vendor.fee_from_card = True  # fees come out of the card balance
    vendor.recharge_fee = 0.5  # reported on every recharge row
    sup = _adapter(vendor)
    res = await sup.submit(_spec(WITH_RECHARGE))
    card = vendor.cards[int(res.upstream_ref)]
    # 20 - 1.5 (open fee) + 10 - 0.5 (recharge fee) = 28 = 30 - (1.5 + 0.5): enough, not short
    assert card["available_amount"] == 28.0
    assert res.outcome.value == "ok" and len(res.secrets) == 1
    # one unit less than what the reported fees explain is still short
    card["available_amount"] = 27.0
    short = await sup.query(_spec(WITH_RECHARGE, ref=res.upstream_ref))
    assert short.outcome.value == "fatal" and short.error_code == "balance_short"
    # the floor is init - open_fee: a card that does not hold that is short whatever the fees
    card["available_amount"] = 18.0
    assert (await sup.query(_spec(OPEN, ref=res.upstream_ref))).error_code == "balance_short"
    card["available_amount"] = 18.5
    assert (await sup.query(_spec(OPEN, ref=res.upstream_ref))).outcome.value == "ok"


async def test_balance_short_job_tells_staff_the_card_exists_and_void_deletes_it(env, vup):
    vendor = MockZovo()
    vendor.short_open = 5.0  # the card opens with 15 instead of 20
    await _zovo(vup, vendor, OPEN)
    _buyer, order = await vup.paid_order()
    await vup.run()
    job = await _job(vup, order)
    assert job.state == "needs_manual" and job.error_code == "balance_short"
    admin, _ = await env.login("admin")
    view = (await admin.get(f"{JOBS}/{job.id}")).json()
    card = _card(vendor)
    assert view["charged_possible"] is True and view["maybe_sent"] is True
    assert view["card_id"] == str(card["id"]) == job.upstream_ref
    assert view["card_mask"] == f"{card['card_number'][:6]}****{card['card_number'][-4:]}"
    assert "void" in view["actions"] and "switch" not in view["actions"]
    r = await admin.post(f"{JOBS}/{job.id}/void", json={"reason": "card short, delete it"})
    assert r.status_code == 200, r.text
    await _settle(vup, 3)
    assert _card(vendor)["status"] == "DELETED"
    assert (await _job(vup, order)).state == "voided"


async def test_frozen_card_job_is_flagged_and_voidable(env, vup):
    vendor = MockZovo()
    await _zovo(vup, vendor, OPEN)
    _buyer, order = await vup.paid_order()
    from app.procurement.engine import process_outbox_one

    while await process_outbox_one(vup.ctx):
        pass
    sup = vup.mocks[next(iter(vup.mocks))]
    first = await sup.submit(_spec(OPEN, key=(await _job(vup, order)).idempotency_key))
    vendor.cards[int(first.upstream_ref)]["status"] = "FROZEN"
    await _sql(env, "UPDATE procurement_jobs SET upstream_ref = :r", r=first.upstream_ref)
    await vup.run()
    job = await _job(vup, order)
    assert job.state == "needs_manual" and job.error_code == "card_not_active"
    admin, _ = await env.login("admin")
    view = (await admin.get(f"{JOBS}/{job.id}")).json()
    assert view["charged_possible"] and view["card_id"] == first.upstream_ref and view["card_mask"]


async def _two_orders(env, vup, vendor):
    """Order 1 is bought normally (card A); order 2's open is refused, so it needs staff."""
    _b1, order1 = await vup.paid_order()
    await vup.run()
    vendor.force("/cards/open", "401", times=3)
    _b2, order2 = await vup.paid_order()
    await vup.run()
    return order1, order2, await _job(vup, order1), await _job(vup, order2)


async def test_hand_pasted_card_cannot_duplicate_a_card_another_job_holds(env, vup):
    vendor = MockZovo()
    await _zovo(vup, vendor, OPEN)
    _o1, o2, j1, j2 = await _two_orders(env, vup, vendor)
    assert j1.state == "fulfilled" and j2.state == "needs_manual"
    pan1 = next(iter(vendor.cards.values()))["card_number"]
    admin, _ = await env.login("admin")
    # the same card (same first 6 + last 4) pasted into the other job: refused
    twin = f"{pan1[:6]}123456{pan1[-4:]}----08/31----123"
    r = await admin.post(f"{JOBS}/{j2.id}/fulfil", json={"code": twin})
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_code"
    assert not (await _job(vup, o2)).card_stock_ids
    # an unrelated card is accepted; and the vendor card id of card A as the fingerprint input
    # collides with the engine's own row (UNIQUE), whatever the pasted number looks like
    r = await admin.post(
        f"{JOBS}/{j2.id}/fulfil",
        json={"code": "5555555555554444----08/31----123", "card_id": j1.upstream_ref},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_code"
    r = await admin.post(
        f"{JOBS}/{j2.id}/fulfil",
        json={"code": "5555555555554444----08/31----123", "card_id": "999999"},
    )
    assert r.status_code == 200, r.text
    # pasting a card into the job that already holds its mask (the vendor's own) is fine
    assert (await _job(vup, o2)).state == "fulfilled"


async def test_void_failed_raises_one_staff_alert(env, vup, monkeypatch):
    from datetime import datetime, timezone

    from app.core import notify_tg
    from app.modules.payments import reconcile

    vendor = MockZovo()
    await _zovo(vup, vendor, OPEN)
    _buyer, order = await vup.paid_order()
    await vup.run()
    job = await _job(vup, order)
    vendor.force(f"/cards/{job.upstream_ref}", "delete_disabled", times=1)
    r = await _refund(env, order)
    assert r.status_code == 200
    await _settle(vup, 4)
    assert (await _job(vup, order)).error_code == "void_failed"
    sent: list[dict] = []
    monkeypatch.setattr(
        notify_tg,
        "notify_bg",
        lambda res, tid, kind, lines, **kw: sent.append({"lines": lines, **kw}),
    )
    async with env.api_session() as db:
        n = await reconcile.alert_procurement_jobs(
            env.res, db, env.tenant_id, datetime.now(timezone.utc)
        )
    assert n == 1 and len(sent) == 1
    assert sent[0]["once"][0] == f"procurement_job:{job.id}:void_failed"
    assert "proc.void_failed" in repr(sent[0]["lines"])


async def test_admin_void_keeps_a_pending_delete_and_restarts_a_failed_one(env, vup):
    vendor = MockZovo()
    vendor.delete_pending_next = 1
    await _zovo(vup, vendor, OPEN)
    _buyer, order = await vup.paid_order()
    await vup.run()
    job = await _job(vup, order)
    admin, _ = await env.login("admin")
    assert (
        await admin.post(f"{JOBS}/{job.id}/void", json={"reason": "first try"})
    ).status_code == 200
    await vup.make_due()
    await vup.run()  # the DELETE goes out once and answers 202
    job = await _job(vup, order)
    assert job.state == "void_pending" and job.step == "delete_pending"
    deletes = [1 for m, p, _h in vendor.requests if m == "DELETE"]
    assert len(deletes) == 1
    # staff see it as stuck and press void again: the pending delete is NOT restarted
    await _sql(env, "UPDATE procurement_jobs SET state = 'needs_manual', lease_until = NULL")
    r = await admin.post(f"{JOBS}/{job.id}/void", json={"reason": "second try"})
    assert r.status_code == 200, r.text
    job = await _job(vup, order)
    assert job.state == "void_pending" and job.step == "delete_pending"
    await _settle(vup, 4)
    assert (await _job(vup, order)).state == "voided" and _card(vendor)["status"] == "DELETED"
    assert len([1 for m, p, _h in vendor.requests if m == "DELETE"]) == 1
    # a job in void_failed (step done) starts again from the beginning
    await _sql(
        env,
        "UPDATE procurement_jobs SET state = 'needs_manual', step = 'done', "
        "error_code = 'void_failed'",
    )
    r = await admin.post(f"{JOBS}/{job.id}/void", json={"reason": "after void_failed"})
    assert r.status_code == 200, r.text
    assert (await _job(vup, order)).step == "void"
