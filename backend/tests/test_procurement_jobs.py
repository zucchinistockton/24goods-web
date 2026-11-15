"""review-v12 M2: staff view of procurement jobs and the actions on them."""

from __future__ import annotations

# ruff: noqa: F811  (fixtures are imported from test_procurement_m2)
import json
import uuid

from sqlalchemy import text

from app.suppliers.base import Outcome, Result
from app.suppliers.mock import MockSupplier
from tests.test_procurement_m2 import LosingMock, Up, _status, up  # noqa: F401

JOBS = "/api/v1/admin/procurement/jobs"
CODE = "STAFF-PASTED-CODE-8841"


async def _sql(env, sql: str, **params):
    async with env.owner_session() as db:
        res = await db.execute(text(sql), params)
        await db.commit()
        return res


async def _needs_manual(env, up: Up, *, outcomes=(Outcome.FATAL,), supplier=None, qty: int = 1):
    """A paid order whose first (only) supplier answer is FATAL -> needs_manual, order held."""
    mock = supplier or MockSupplier("mock_a", outcomes=outcomes)
    if supplier is None:
        await up.supplier(mock)
    buyer, order = await up.paid_order(qty)
    await up.run()
    assert await env.drain() >= 1  # procure.failed -> manual_review
    assert await _status(buyer, order) == "manual_review"
    return mock, buyer, order


async def _one_job(up: Up, order):
    (job,) = await up.jobs(order["id"])
    return job


async def _staff(env, role="agent"):
    c, u = await env.login(role)
    return c, u


async def _foreign_job(env) -> uuid.UUID:
    buyer = await env.user("buyer", tenant_id=env.other_tenant_id)
    oid, jid = uuid.uuid4(), uuid.uuid4()
    async with env.owner_session(env.other_tenant_id) as db:
        p = await env.product(mode="upstream_cdk", tenant_id=env.other_tenant_id)
        await db.execute(
            text(
                "INSERT INTO orders (id, tenant_id, order_no, buyer_id, product_id, title, qty, "
                "amount_minor, subtotal_minor, currency, status, delivery_mode) VALUES "
                "(:i, :t, :n, :b, :p, 'x', 1, 100, 100, 'CNY', 'manual_review', 'upstream_cdk')"
            ),
            {"i": oid, "t": env.other_tenant_id, "n": "G" + uuid.uuid4().hex[:12],
             "b": buyer.id, "p": p.id},
        )  # fmt: skip
        await db.execute(
            text(
                "INSERT INTO procurement_jobs (id, tenant_id, order_id, unit_no, kind, state, step,"
                " idempotency_key, error_code) VALUES (:j, :t, :o, 1, 'cdk', 'needs_manual', "
                "'sent', :k, 'mock_fatal')"
            ),
            {"j": jid, "t": env.other_tenant_id, "o": oid, "k": "pj:" + jid.hex + ":submit"},
        )
        await db.commit()
    return jid


# ------------------------------------------------------------------ list / detail


async def test_list_filter_pagination_and_detail_timeline(env, up):
    staff, _ = await _staff(env)
    mock = MockSupplier("mock_a")
    await up.supplier(mock)
    _buyer, order = await up.paid_order(2)
    await up.run()
    r = await staff.get(JOBS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2 and body["counts"]["fulfilled"] == 2 and body["counts"]["queued"] == 0
    assert {i["unit_no"] for i in body["items"]} == {1, 2}
    item = body["items"][0]
    assert item["supplier_code"] == "mock_a" and item["state"] == "fulfilled"
    assert item["order_no"] and item["actions"] == ["void"]
    page = (await staff.get(JOBS, params={"page": 2, "page_size": 1})).json()
    assert page["total"] == 2 and len(page["items"]) == 1 and page["page"] == 2
    assert (await staff.get(JOBS, params={"state": "needs_manual"})).json()["total"] == 0
    assert (await staff.get(JOBS, params={"state": "fulfilled"})).json()["total"] == 2
    assert (await staff.get(JOBS, params={"order_id": order["id"]})).json()["total"] == 2
    assert (await staff.get(JOBS, params={"state": "bogus"})).status_code == 422
    detail = (await staff.get(f"{JOBS}/{item['id']}")).json()
    assert detail["has_code"] is True and detail["order"]["order_no"] == item["order_no"]
    actions = [t["action"] for t in detail["timeline"]]
    assert actions[0] == "procurement.job_created" and "procurement.job_state" in actions
    assert any(t["reason"] == "supplier_ok" for t in detail["timeline"])
    # a code never appears in any job response
    blob = json.dumps([body, detail])
    assert "MOCK-" not in blob and "blob" not in blob


async def test_roles_and_cross_tenant_access(env, up):
    admin, _ = await _staff(env, "admin")
    agent, _ = await _staff(env)
    buyer, _ = await env.login("buyer")
    _mock, _b, order = await _needs_manual(env, up)
    job = await _one_job(up, order)
    jid = str(job.id)
    for path, body in (
        ("", None), (f"/{jid}", None),
        (f"/{jid}/retry", {}), (f"/{jid}/requery", {}), (f"/{jid}/switch", {}),
        (f"/{jid}/fulfil", {"code": CODE}), (f"/{jid}/void", {"reason": "abc"}),
        (f"/{jid}/fail", {"reason": "abc"}),
    ):  # fmt: skip
        r = await (buyer.get(JOBS + path) if body is None else buyer.post(JOBS + path, json=body))
        assert r.status_code == 403, path
    assert (await agent.post(f"{JOBS}/{jid}/void", json={"reason": "abc"})).status_code == 403
    # a foreign tenant's job is invisible: list, detail and every action answer 404
    fj = await _foreign_job(env)
    for c in (admin, agent):
        assert (await c.get(JOBS)).json()["total"] == 1
        assert (await c.get(f"{JOBS}/{fj}")).status_code == 404
        for act, body in (("retry", {}), ("requery", {}), ("switch", {}),
                          ("fulfil", {"code": CODE}), ("fail", {"reason": "abc"})):  # fmt: skip
            assert (await c.post(f"{JOBS}/{fj}/{act}", json=body)).status_code == 404, act
    assert (await admin.post(f"{JOBS}/{fj}/void", json={"reason": "abc"})).status_code == 404
    async with env.owner_session(env.other_tenant_id) as db:
        row = (
            await db.execute(
                text("SELECT state, manual_by, card_stock_ids FROM procurement_jobs WHERE id = :j"),
                {"j": fj},
            )
        ).one()
    assert row.state == "needs_manual" and row.manual_by is None and not row.card_stock_ids
    assert await env.scalar("SELECT count(*) FROM card_stock WHERE order_id IS NOT NULL") == 0


# ------------------------------------------------------------------ retry / requery


async def test_retry_keeps_the_key_and_resumes_the_order(env, up):
    staff, agent = await _staff(env)
    mock, buyer, order = await _needs_manual(env, up)
    job = await _one_job(up, order)
    key = job.idempotency_key
    assert "retry" in (await staff.get(f"{JOBS}/{job.id}")).json()["actions"]
    r = await staff.post(f"{JOBS}/{job.id}/retry")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "queued" and r.json()["error_code"] is None
    assert await _status(buyer, order) == "paid"  # the hold procurement caused is lifted
    # retry of a job that is not needs_manual is refused
    assert (await staff.post(f"{JOBS}/{job.id}/retry")).status_code == 409
    await up.run()
    assert await env.drain() >= 1
    assert await _status(buyer, order) == "delivered"
    after = await _one_job(up, order)
    assert after.idempotency_key == key and after.state == "fulfilled"
    assert {k for op, k in mock.calls if op == "submit"} == {key}  # never a new key
    act = await env.scalar(
        "SELECT actor_id FROM audit_log WHERE action = 'procurement.job_state' "
        "AND detail->>'reason' = 'manual:retry'"
    )
    assert act == agent.id


async def test_retry_of_a_maybe_charged_job_reconciles_first(env, up):
    staff, _ = await _staff(env)
    _mock, _buyer, order = await _needs_manual(env, up)
    job = await _one_job(up, order)
    # the engine gave up on an unresolved answer: the supplier may have charged
    await _sql(
        env,
        "UPDATE procurement_jobs SET error_code = 'unresolved', step = 'reconcile', "
        "maybe_sent = true",
    )
    detail = (await staff.get(f"{JOBS}/{job.id}")).json()
    assert detail["charged_possible"] is True and "switch" not in detail["actions"]
    r = await staff.post(f"{JOBS}/{job.id}/retry")
    assert r.status_code == 200
    assert r.json()["state"] == "submitting" and r.json()["step"] == "reconcile"
    assert (await _one_job(up, order)).idempotency_key == job.idempotency_key


async def test_requery_accepted_pending_job_and_needs_manual(env, up):
    staff, _ = await _staff(env)
    mock = LosingMock("mock_a", lose=0, pending=True)
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()
    job = await _one_job(up, order)
    assert job.state == "accepted_pending"
    assert "requery" in (await staff.get(f"{JOBS}/{job.id}")).json()["actions"]
    r = await staff.post(f"{JOBS}/{job.id}/requery")
    assert r.status_code == 200 and r.json()["state"] == "accepted_pending"
    await up.run()  # due now: the engine queries under the same key
    assert (await _one_job(up, order)).state == "fulfilled"
    assert (await staff.post(f"{JOBS}/{job.id}/requery")).status_code == 409  # fulfilled
    # a needs_manual job goes back to reconcile and the order is resumed
    mock.outcomes.append(Outcome.FATAL)
    _mock2, buyer2, order2 = await _needs_manual(env, up, supplier=mock)
    j2 = (await up.jobs(order2["id"]))[0]
    r = await staff.post(f"{JOBS}/{j2.id}/requery")
    assert r.status_code == 200 and r.json()["state"] == "submitting"
    assert r.json()["step"] == "reconcile"
    assert await _status(buyer2, order2) == "paid"


# ------------------------------------------------------------------ switch


async def test_switch_to_next_source_only_when_not_charged(env, up):
    staff, _ = await _staff(env)
    a = MockSupplier("mock_a", outcomes=[Outcome.FATAL])
    b = MockSupplier("mock_b")
    await up.supplier(a, code="mock_a", priority=10)
    sid_b, oid_b = await up.supplier(b, code="mock_b", priority=20)
    buyer, order = await up.paid_order()
    await up.run()
    assert await env.drain() >= 1
    job = await _one_job(up, order)
    assert job.state == "needs_manual" and job.supplier_id != sid_b
    old_key = job.idempotency_key
    # may already be charged -> refused, nothing changes
    await _sql(env, "UPDATE procurement_jobs SET upstream_ref = 'u1'")
    r = await staff.post(f"{JOBS}/{job.id}/switch")
    assert r.status_code == 409 and r.json()["error"]["code"] == "maybe_charged"
    await _sql(env, "UPDATE procurement_jobs SET upstream_ref = NULL")
    r = await staff.post(f"{JOBS}/{job.id}/switch")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "queued" and r.json()["supplier_code"] == "mock_b"
    moved = await _one_job(up, order)
    assert moved.idempotency_key != old_key and moved.offer_id == oid_b
    assert moved.tried_offer_ids and await _status(buyer, order) == "paid"
    await up.run()
    assert await env.drain() >= 1
    assert await _status(buyer, order) == "delivered"
    assert not a.charged and len(b.charged) == 1


async def test_switch_without_another_source_is_refused(env, up):
    staff, _ = await _staff(env)
    _m, buyer, order = await _needs_manual(env, up)
    job = await _one_job(up, order)
    r = await staff.post(f"{JOBS}/{job.id}/switch")
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_other_source"
    assert await _status(buyer, order) == "manual_review"  # nothing was resumed


# ------------------------------------------------------------------ manual fulfil


async def test_manual_fulfil_seals_delivers_and_never_leaks_the_code(env, up):
    staff, agent = await _staff(env)
    _mock, buyer, order = await _needs_manual(env, up)
    job = await _one_job(up, order)
    for bad in ("abc", "x" * 300, "line1\nline2", ""):
        r = await staff.post(f"{JOBS}/{job.id}/fulfil", json={"code": bad})
        assert r.status_code == 422, bad
        assert "line1" not in r.text
    r = await staff.post(f"{JOBS}/{job.id}/fulfil", json={"code": f"  {CODE}  "})
    assert r.status_code == 200, r.text
    assert CODE not in r.text and r.json()["state"] == "fulfilled" and r.json()["manual_by"]
    assert r.json()["actions"] == ["void"]
    # sealed, sold to the order, nothing in clear anywhere we can look
    async with env.owner_session() as db:
        row = (
            await db.execute(
                text("SELECT status, order_id, blob, created_by FROM card_stock "
                     "WHERE order_id = :o"),
                {"o": uuid.UUID(order["id"])},
            )
        ).one()  # fmt: skip
        assert row.status == "sold" and bytes(row.blob) != CODE.encode()
        assert CODE.encode() not in bytes(row.blob) and row.created_by == agent.id
        dump = ""
        for sql in (
            "SELECT string_agg(payload::text, '|') FROM outbox",
            "SELECT string_agg(coalesce(detail::text, '') || coalesce(after::text, '') || "
            "coalesce(before::text, ''), '|') FROM audit_log",
        ):
            dump += str(await db.scalar(text(sql)))
    assert CODE not in dump
    topics = await env.scalar(
        "SELECT count(*) FROM outbox WHERE topic = 'procure.fulfilled' AND key = :k",
        k=f"fulfilled:{order['id']}",
    )
    assert topics == 1
    assert await env.drain() >= 1
    assert await _status(buyer, order) == "delivered"
    # the same job can never be fulfilled twice, by hand or by anything else
    again = await staff.post(f"{JOBS}/{job.id}/fulfil", json={"code": "ANOTHER-CODE-7777"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_fulfilled"
    assert await up.run() == 0
    assert await env.scalar("SELECT count(*) FROM card_stock WHERE order_id IS NOT NULL") == 1
    assert await env.scalar("SELECT count(*) FROM outbox WHERE topic = 'procure.fulfilled'") == 1


async def test_manual_fulfil_duplicate_code_is_refused(env, up):
    staff, _ = await _staff(env)
    mock = MockSupplier("mock_a", outcomes=[Outcome.FATAL, Outcome.FATAL])
    await up.supplier(mock)
    _m, _b1, o1 = await _needs_manual(env, up, supplier=mock)
    _m, _b2, o2 = await _needs_manual(env, up, supplier=mock)
    j1, j2 = (await _one_job(up, o1)), (await _one_job(up, o2))
    assert (await staff.post(f"{JOBS}/{j1.id}/fulfil", json={"code": CODE})).status_code == 200
    r = await staff.post(f"{JOBS}/{j2.id}/fulfil", json={"code": CODE})
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_code"
    assert CODE not in r.text
    assert (await _one_job(up, o2)).state == "needs_manual"


async def test_engine_result_after_staff_fulfil_cannot_double_fulfil(env, up):
    """A supplier answer that arrives after staff fulfilled the job is dropped by the engine."""
    from app.procurement import engine

    staff, _ = await _staff(env)
    mock = MockSupplier("mock_a")
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    from app.procurement.engine import process_outbox_one

    while await process_outbox_one(up.ctx):
        pass
    job = await _one_job(up, order)
    assert job.state == "queued"
    lease = await engine._lease(up.ctx, job.id, job.tenant_id)  # the engine is mid-call
    assert lease is not None
    r = await staff.post(f"{JOBS}/{job.id}/fulfil", json={"code": CODE})
    assert r.status_code == 200, r.text
    late = Result(Outcome.OK, upstream_ref="late", secrets=("LATE-VENDOR-CODE-1",))
    await engine._apply(up.ctx, lease, late)
    assert await env.scalar("SELECT count(*) FROM card_stock WHERE order_id IS NOT NULL") == 1
    assert await env.scalar("SELECT count(*) FROM outbox WHERE topic = 'procure.fulfilled'") == 1
    assert (await _one_job(up, order)).upstream_ref is None


async def test_webhook_for_a_manually_fulfilled_job_changes_nothing(env, up):
    from app.models import SupplierWebhookEvent

    staff, _ = await _staff(env)
    mock = LosingMock("mock_a", lose=0, pending=True)
    sid, _ = await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()
    job = await _one_job(up, order)
    assert job.state == "accepted_pending"
    assert (await staff.post(f"{JOBS}/{job.id}/fulfil", json={"code": CODE})).status_code == 200
    ev_id = uuid.uuid4()
    async with env.owner_session() as db:
        db.add(
            SupplierWebhookEvent(
                id=ev_id, tenant_id=env.tenant_id, supplier_id=sid, dedup_key="cdk.issued:1",
                kind="cdk.issued", payload={"cdk_id": job.upstream_ref, "cdk_status": "unused"},
            )
        )  # fmt: skip
        await db.execute(
            text("INSERT INTO outbox (tenant_id, topic, key, payload) VALUES (:t, "
                 "'supplier.event', 'e1', :p)"),
            {"t": env.tenant_id, "p": json.dumps({"event_id": str(ev_id)})},
        )  # fmt: skip
        await db.commit()
    await up.run()
    assert (await _one_job(up, order)).state == "fulfilled"
    assert await env.scalar("SELECT count(*) FROM card_stock WHERE order_id IS NOT NULL") == 1
    assert await env.scalar("SELECT count(*) FROM outbox WHERE topic = 'procure.fulfilled'") == 1


# ------------------------------------------------------------------ void / fail


async def test_void_disables_upstream_and_needs_an_admin(env, up):
    admin, _ = await _staff(env, "admin")
    agent, _ = await _staff(env)
    mock = MockSupplier("mock_a")
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()
    job = await _one_job(up, order)
    assert job.state == "fulfilled"
    assert (await agent.post(f"{JOBS}/{job.id}/void", json={"reason": "refund"})).status_code == 403
    assert (await admin.post(f"{JOBS}/{job.id}/void", json={"reason": "x"})).status_code == 422
    r = await admin.post(f"{JOBS}/{job.id}/void", json={"reason": "buyer refunded"})
    assert r.status_code == 200 and r.json()["state"] == "void_pending"
    assert (await admin.post(f"{JOBS}/{job.id}/void", json={"reason": "again"})).status_code == 409
    await up.run()
    assert (await _one_job(up, order)).state == "voided"
    assert [k for op, k in mock.calls if op == "void"] == [f"pj:{job.id.hex}:void"]


async def test_void_without_upstream_reference_is_local(env, up):
    admin, _ = await _staff(env, "admin")
    _m, buyer, order = await _needs_manual(env, up)
    job = await _one_job(up, order)
    r = await admin.post(f"{JOBS}/{job.id}/void", json={"reason": "never sent"})
    assert r.status_code == 200 and r.json()["state"] == "voided"
    assert await _status(buyer, order) == "manual_review"  # the refund is after-sales' business


async def test_mark_failed_closes_the_job_and_keeps_the_order_for_refund(env, up):
    staff, _ = await _staff(env)
    _m, buyer, order = await _needs_manual(env, up)
    job = await _one_job(up, order)
    assert (await staff.post(f"{JOBS}/{job.id}/fail", json={"reason": "x"})).status_code == 422
    r = await staff.post(f"{JOBS}/{job.id}/fail", json={"reason": "vendor confirmed no code"})
    assert r.status_code == 200 and r.json()["state"] == "failed"
    assert r.json()["actions"] == ["void"]
    assert (await staff.post(f"{JOBS}/{job.id}/fail", json={"reason": "again"})).status_code == 409
    assert (await staff.post(f"{JOBS}/{job.id}/retry")).status_code == 409
    assert await _status(buyer, order) == "manual_review"
    detail = (await staff.get(f"{JOBS}/{job.id}")).json()
    assert detail["timeline"][-1]["after"]["state"] == "failed"
    assert detail["timeline"][-1]["actor_role"] == "agent"


async def test_actions_are_rate_limited(env, up):
    staff, _ = await _staff(env)
    _m, _buyer, order = await _needs_manual(env, up)
    job = await _one_job(up, order)
    codes = set()
    for _i in range(125):
        codes.add((await staff.post(f"{JOBS}/{job.id}/switch")).status_code)
        if 429 in codes:
            break
    assert 429 in codes
