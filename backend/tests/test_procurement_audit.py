"""Payment-audit fixes on v12 M2 (2026-11-15): H1, H2, M1-M5 and the low findings."""

from __future__ import annotations

# ruff: noqa: F811  (fixtures are imported from test_procurement_m2)
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
from sqlalchemy import text

from app.suppliers.base import Outcome, Result
from app.suppliers.mock import MockSupplier
from tests.conftest import ALEMBIC_INI, OWNER_PW, _with_db
from tests.test_procurement_m2 import LosingMock, Up, up  # noqa: F401


async def _sql(env, sql: str, **params):
    async with env.owner_session() as db:
        res = await db.execute(text(sql), params)
        await db.commit()
        return res


# ------------------------------------------------------------------ migration 0027


async def test_migration_0027_up_and_down(services):
    from alembic import command
    from alembic.config import Config

    db_name = "g24_mig0027"
    dsn = os.environ["TEST_DATABASE_URL"]
    plain = dsn.replace("postgresql+asyncpg://", "postgresql://")
    admin = await asyncpg.connect(plain, timeout=5)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {db_name} OWNER app_owner")
    finally:
        await admin.close()
    owner_url = _with_db(dsn, "app_owner", OWNER_PW, db_name)

    async def alembic(target: str, down: bool = False) -> None:
        prev = os.environ.get("APP_MIGRATE_DATABASE_URL")
        os.environ["APP_MIGRATE_DATABASE_URL"] = owner_url
        try:
            fn = command.downgrade if down else command.upgrade
            await asyncio.to_thread(fn, Config(str(ALEMBIC_INI)), target)
        finally:
            if prev is None:
                os.environ.pop("APP_MIGRATE_DATABASE_URL", None)
            else:
                os.environ["APP_MIGRATE_DATABASE_URL"] = prev

    cols = ("maybe_sent", "recover", "lease_until", "first_sent_at", "cost_known")

    async def state() -> tuple[set[str], bool]:
        conn = await asyncpg.connect(owner_url.replace("+asyncpg", ""), timeout=5)
        try:
            have = {
                r["column_name"]
                for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name IN ('procurement_jobs', 'supplier_offers')"
                )
            }
            force = await conn.fetchval(
                "SELECT relforcerowsecurity FROM pg_class WHERE relname = 'procurement_jobs'"
            )
            return have & set(cols), bool(force)
        finally:
            await conn.close()

    async def priv(role: str, col: str, kind: str) -> bool:
        conn = await asyncpg.connect(owner_url.replace("+asyncpg", ""), timeout=5)
        try:
            return bool(
                await conn.fetchval(
                    "SELECT has_column_privilege($1, 'procurement_jobs', $2, $3)", role, col, kind
                )
            )
        finally:
            await conn.close()

    try:
        await alembic("head")
        have, force = await state()
        assert have == set(cols) and force
        assert await priv("app_api", "recover", "UPDATE")
        assert not await priv("app_api", "maybe_sent", "UPDATE")  # engine-owned
        assert not await priv("app_api", "lease_until", "UPDATE")
        assert await priv("app_delivery", "state", "SELECT")
        assert not await priv("app_delivery", "upstream_ref", "SELECT")
        assert not await priv("app_delivery", "state", "UPDATE")
        await alembic("0026_procurement_admin_actions", down=True)
        have, force = await state()
        assert have == set() and force
        assert not await priv("app_delivery", "state", "SELECT")
        await alembic("head")
        assert (await state())[0] == set(cols)
    finally:
        admin = await asyncpg.connect(plain, timeout=5)
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")
        finally:
            await admin.close()


# ------------------------------------------------------------------ helpers
async def _refund(env, order):
    admin, _ = await env.login("admin")
    return await admin.post(
        f"/api/v1/admin/orders/{order['id']}/refund",
        json={"reference": "REF-" + uuid.uuid4().hex[:8].upper()},
    )


async def _create_jobs_only(up: Up) -> None:
    """Drain the outbox only: jobs exist, nothing was sent to a supplier."""
    from app.procurement.engine import process_outbox_one

    while await process_outbox_one(up.ctx):
        pass


async def _job(up: Up, order):
    (job,) = await up.jobs(order["id"])
    return job


async def _order_status(env, order) -> str:
    return await env.scalar("SELECT status FROM orders WHERE id = :o", o=uuid.UUID(order["id"]))


async def _cards(env, order) -> int:
    return await env.scalar(
        "SELECT count(*) FROM card_stock WHERE order_id = :o", o=uuid.UUID(order["id"])
    )


# ------------------------------------------------------------------ H1
async def test_refund_during_reconcile_voids_the_code(env, up):
    mock = LosingMock("mock_a", lose=1)
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()
    job = await _job(up, order)
    assert job.state == "submitting" and job.step == "reconcile" and job.maybe_sent
    assert len(mock.charged) == 1  # the supplier issued a code whose answer was lost
    r = await _refund(env, order)
    assert r.status_code == 200, r.text
    job = await _job(up, order)
    assert job.state == "failed" and job.recover and job.error_code == "order_refunded"
    await up.make_due()
    for _ in range(3):
        await up.run()
    job = await _job(up, order)
    assert job.state == "voided" and not job.recover
    assert job.upstream_ref and job.upstream_status == "disabled"
    assert [op for op, _ in mock.calls] == ["submit", "query", "void"]
    assert not mock.charged  # disabled at the supplier
    assert await _cards(env, order) == 0 and await _order_status(env, order) == "refunded"
    actors = await env.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'procurement.job_state' "
        "AND actor_id IS NOT NULL AND after->>'state' = 'failed'"
    )
    assert actors == 1  # the refund's closing of the job is attributed to the staff member
    assert await env.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'procurement.job_recovered'"
    ) == 1  # fmt: skip


async def test_refund_before_any_send_fails_the_job_without_a_call(env, up):
    mock = MockSupplier("mock_a")
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await _create_jobs_only(up)
    job = await _job(up, order)
    assert job.state == "queued" and not job.maybe_sent
    assert (await _refund(env, order)).status_code == 200
    job = await _job(up, order)
    assert job.state == "failed" and not job.recover and job.error_code == "order_refunded"
    await up.make_due()
    await up.run()
    assert mock.calls == [] and await _cards(env, order) == 0


async def test_engine_never_sends_or_replays_for_an_order_that_left_paid(env, up):
    mock = LosingMock("mock_a", lose=1)
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()
    assert [op for op, _ in mock.calls] == ["submit"]
    # the order leaves `paid` behind the engine's back (any path that does not touch jobs)
    await _sql(env, "UPDATE orders SET status = 'refunded'")
    await up.make_due()
    for _ in range(3):
        await up.run()
    job = await _job(up, order)
    # the reconcile (which could still buy) never ran as such: only the recovery that voids
    assert job.state == "voided" and not mock.charged
    assert [op for op, _ in mock.calls] == ["submit", "query", "void"]
    assert await _cards(env, order) == 0
    # a never-sent job of a closed order is failed without any call
    mock2 = MockSupplier("mock_a")
    up.mocks = {k: mock2 for k in up.mocks}
    _b, order2 = await up.paid_order()
    await _create_jobs_only(up)
    await _sql(
        env, "UPDATE orders SET status = 'cancelled' WHERE id = :o", o=uuid.UUID(order2["id"])
    )
    await up.make_due()
    await up.run()
    job2 = await _job(up, order2)
    assert job2.state == "failed" and job2.error_code == "order_not_paid" and mock2.calls == []


async def test_held_order_parks_the_job_instead_of_sending(env, up):
    mock = MockSupplier("mock_a")
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await _create_jobs_only(up)
    await _sql(env, "UPDATE orders SET status = 'manual_review'")
    await up.run()
    job = await _job(up, order)
    assert job.state == "needs_manual" and job.error_code == "order_not_paid"
    assert mock.calls == []


async def test_answer_after_refund_is_not_bound_and_the_code_is_voided(env, up):
    class Racing(MockSupplier):
        async def submit(self, job):
            res = await super().submit(job)  # the supplier charges
            async with self.owner() as db:  # ... and the order is refunded meanwhile
                await db.execute(text("UPDATE orders SET status = 'refunded'"))
                await db.commit()
            return res

    mock = Racing("mock_a")
    mock.owner = env.owner_session
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    for _ in range(3):
        await up.run()
    job = await _job(up, order)
    assert job.state == "voided" and job.upstream_ref and job.cost is not None
    assert job.upstream_status == "disabled"
    assert await _cards(env, order) == 0  # never bound to the refunded order
    assert not mock.charged and [op for op, _ in mock.calls] == ["submit", "void"]


async def test_unfinished_recovery_ends_in_needs_manual_flagged(env, up):
    class Dumb(LosingMock):
        async def query(self, job):
            self.calls.append(("query", job.idempotency_key))
            return Result(Outcome.AMBIGUOUS, error_code="timeout_after_send")

    mock = Dumb("mock_a", lose=1)
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()
    assert (await _refund(env, order)).status_code == 200
    await _sql(env, "UPDATE procurement_jobs SET attempts = 41")
    await up.make_due()
    await up.run()
    job = await _job(up, order)
    assert job.state == "needs_manual" and job.error_code == "unresolved" and job.maybe_sent
    assert not job.recover


async def test_refund_does_not_wait_for_a_job_the_engine_holds(env, up):
    mock = MockSupplier("mock_a")
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await _create_jobs_only(up)
    job = await _job(up, order)
    async with env.owner_session() as holder:  # the engine (or staff) holds the job row
        await holder.execute(
            text("SELECT id FROM procurement_jobs WHERE id = :j FOR UPDATE"), {"j": job.id}
        )
        r = await asyncio.wait_for(_refund(env, order), timeout=15)
        assert r.status_code == 409 and r.json()["error"]["code"] == "procurement_busy"
        await holder.rollback()
    assert await _order_status(env, order) == "paid"  # nothing half-done
    assert (await _refund(env, order)).status_code == 200


# ------------------------------------------------------------------ H2
async def _offer_check(env, oid) -> bool | None:
    return await env.scalar("SELECT last_check_ok FROM supplier_offers WHERE id = :o", o=oid)


async def test_unavailable_plan_fails_over_before_anything_is_sent(env, up):
    a = MockSupplier("mock_a", purchasable=())  # the plan is paused at the first supplier
    b = MockSupplier("mock_b")
    sid_a, offer_a = await up.supplier(a, code="mock_a", priority=10)
    await up.supplier(b, code="mock_b", priority=20)
    _buyer, order = await up.paid_order()
    for _ in range(3):
        await up.run()
    job = await _job(up, order)
    assert job.state == "fulfilled" and job.tried_offer_ids == [offer_a]
    assert a.calls == [] and len(b.charged) == 1  # nothing was ever sent to the paused plan
    assert await _offer_check(env, offer_a) is False
    assert await env.scalar("SELECT breaker_open FROM suppliers WHERE id = :s", s=sid_a) is False


async def test_unavailable_plan_without_alternative_needs_manual_not_charged(env, up):
    a = MockSupplier("mock_a", purchasable=())
    sid, offer = await up.supplier(a)
    _buyer, order = await up.paid_order()
    for _ in range(2):
        await up.run()
    job = await _job(up, order)
    assert job.state == "needs_manual" and job.error_code == "no_source:plan_unavailable"
    assert not job.maybe_sent and a.calls == []
    assert await _offer_check(env, offer) is False
    assert await env.scalar("SELECT breaker_open FROM suppliers WHERE id = :s", s=sid) is False


async def test_failed_availability_lookup_is_retried_not_treated_as_a_refusal(env, up):
    class Flaky(MockSupplier):
        broken = 1

        async def availability(self, offer):
            from app.suppliers.base import Availability

            if self.broken:
                self.broken -= 1
                return Availability(False, "http_503")
            return await super().availability(offer)

    mock = Flaky("mock_a")
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()
    job = await _job(up, order)
    assert job.state == "queued" and job.error_code == "http_503" and mock.calls == []
    await up.make_due()
    await up.run()
    assert (await _job(up, order)).state == "fulfilled"


async def test_ambiguous_request_failed_does_not_open_the_supplier_breaker(env, up):
    from app.suppliers.zovocard.errors import classify

    rule = classify(400, "request_failed", write=True)
    assert rule.outcome is Outcome.AMBIGUOUS and rule.supplier_stop is False

    class Paused(MockSupplier):
        async def submit(self, job):
            self.calls.append(("submit", job.idempotency_key))
            return Result(
                rule.outcome, error_code="request_failed", supplier_stop=rule.supplier_stop
            )

        async def query(self, job):
            return await self.submit(job)

    mock = Paused("mock_a")
    sid, offer = await up.supplier(mock)
    _buyer, order = await up.paid_order()
    for _ in range(3):
        await up.run()
        await up.make_due()
    job = await _job(up, order)
    assert job.state == "submitting" and job.step == "reconcile" and job.maybe_sent
    assert await env.scalar("SELECT breaker_open FROM suppliers WHERE id = :s", s=sid) is False
    assert await _offer_check(env, offer) is False  # per-offer stop: the storefront hides it
    keys = {k for _, k in mock.calls}
    assert keys == {job.idempotency_key}  # still the same key, still AMBIGUOUS for money


# ------------------------------------------------------------------ M4 / M5
async def _detail(env, job):
    staff, _ = await env.login("agent")
    return (await staff.get(f"/api/v1/admin/procurement/jobs/{job.id}")).json()


async def test_edited_offer_is_never_replayed_under_the_old_key(env, up):
    mock = LosingMock("mock_a", lose=1)
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()
    assert [op for op, _ in mock.calls] == ["submit"]
    await _sql(env, """UPDATE supplier_offers SET params = '{"plan": "plus", "region": "XX"}'""")
    await up.make_due()
    await up.run()
    job = await _job(up, order)
    assert job.state == "needs_manual" and job.error_code == "offer_changed" and job.maybe_sent
    assert [op for op, _ in mock.calls] == ["submit"]  # no query / resubmit with another body
    detail = await _detail(env, job)
    assert detail["charged_possible"] is True and "switch" not in detail["actions"]


async def test_edited_offer_stops_polling_of_an_accepted_job(env, up):
    mock = LosingMock("mock_a", lose=0, pending=True)
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()
    assert (await _job(up, order)).state == "accepted_pending"
    await _sql(env, """UPDATE supplier_offers SET params = '{"plan": "plus", "region": "XX"}'""")
    await up.make_due()
    await up.run()
    job = await _job(up, order)
    assert job.state == "needs_manual" and job.error_code == "offer_changed"
    assert [op for op, _ in mock.calls] == ["submit"]


async def test_recovery_does_not_replay_an_edited_request(env, up):
    mock = LosingMock("mock_a", lose=1)
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()
    assert (await _refund(env, order)).status_code == 200
    await _sql(env, """UPDATE supplier_offers SET params = '{"plan": "plus", "region": "XX"}'""")
    await up.make_due()
    await up.run()
    job = await _job(up, order)
    assert job.state == "needs_manual" and job.error_code == "offer_changed" and not job.recover
    assert [op for op, _ in mock.calls] == ["submit"]


async def test_replay_beyond_the_maximum_age_goes_to_manual(env, up):
    mock = LosingMock("mock_a", lose=1)
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()
    await _sql(env, "UPDATE procurement_jobs SET first_sent_at = now() - interval '7 hours'")
    await up.make_due()
    await up.run()
    job = await _job(up, order)
    assert job.state == "needs_manual" and job.error_code == "replay_expired" and job.maybe_sent
    assert [op for op, _ in mock.calls] == ["submit"]
    # a replay inside the window is still allowed (the setting is documented in config.py)
    from app.procurement.config import ProcurementSettings

    assert ProcurementSettings().replay_max_age_s == 21600.0
    assert ProcurementSettings.from_env({"APP_REPLAY_MAX_AGE_S": "60"}).replay_max_age_s == 60.0


async def test_offer_disabled_after_a_possible_send_is_not_failed_over(env, up):
    a = MockSupplier("mock_a", outcomes=[Outcome.AMBIGUOUS])
    b = MockSupplier("mock_b")
    _, offer_a = await up.supplier(a, code="mock_a", priority=10)
    await up.supplier(b, code="mock_b", priority=20)
    _buyer, order = await up.paid_order()
    await up.run()  # submit AMBIGUOUS -> reconcile is next
    await _sql(env, "UPDATE supplier_offers SET enabled = false WHERE id = :o", o=offer_a)
    await up.make_due()
    await up.run()  # the query finds nothing, the same-key resubmit follows: no failover
    job = await _job(up, order)
    assert job.state == "fulfilled" and job.tried_offer_ids == []  # never moved to supplier b
    assert not b.calls and len(a.charged) == 1
    assert [op for op, _ in a.calls] == ["submit", "query", "submit"]
    assert {k for _, k in a.calls} == {job.idempotency_key}


async def test_failover_refuses_a_job_that_may_have_been_sent(env, up):
    from types import SimpleNamespace

    from app.core.db import tenant_session
    from app.procurement import engine

    await up.supplier(MockSupplier("mock_a"), code="mock_a", priority=10)
    await up.supplier(MockSupplier("mock_b"), code="mock_b", priority=20)
    await up.paid_order()
    await _create_jobs_only(up)
    await _sql(env, "UPDATE procurement_jobs SET maybe_sent = true")
    async with tenant_session(up.ctx.maker, env.tenant_id) as db:
        job = (await db.scalars(_all_jobs())).one()
        row = await engine.load_order(db, job.order_id)
        await engine._failover(SimpleNamespace(redis=env.res.redis), db, job, row, "x")
        assert job.state == "needs_manual" and job.error_code == "maybe_sent:x"
        await db.rollback()


def _all_jobs():
    from sqlalchemy import select

    from app.models import ProcurementJob

    return select(ProcurementJob)


# ------------------------------------------------------------------ M1
JOBS = "/api/v1/admin/procurement/jobs"


async def test_staff_action_racing_an_in_flight_call_is_refused(env, up):
    staff, _ = await env.login("agent")
    seen: dict[str, tuple[int, str]] = {}

    class Racing(MockSupplier):
        async def submit(self, job):
            async with env.owner_session() as db:
                jid = await db.scalar(text("SELECT id FROM procurement_jobs"))
            res = await super().submit(job)  # the supplier has charged; the answer is on its way
            f = await staff.post(f"{JOBS}/{jid}/fulfil", json={"code": "HAND-PASTED-1234"})
            n = await staff.post(f"{JOBS}/{jid}/fail", json={"reason": "gave up waiting"})
            seen["fulfil"] = (f.status_code, f.json()["error"]["code"])
            seen["fail"] = (n.status_code, n.json()["error"]["code"])
            return res

    mock = Racing("mock_a")
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()
    assert seen == {"fulfil": (409, "job_in_flight"), "fail": (409, "job_in_flight")}
    job = await _job(up, order)
    assert job.state == "fulfilled" and job.upstream_ref and job.manual_by is None
    assert await _cards(env, order) == 1  # the engine's code, not a second one
    assert await env.drain() >= 1
    assert await _order_status(env, order) == "delivered"


async def test_live_lease_blocks_fulfil_and_fail_until_it_passes(env, up):
    staff, _ = await env.login("agent")
    await up.supplier(MockSupplier("mock_a", outcomes=[Outcome.FATAL]))
    _buyer, order = await up.paid_order()
    await up.run()
    job = await _job(up, order)
    await _sql(env, "UPDATE procurement_jobs SET lease_until = now() + interval '1 minute'")
    for path, body in (("fulfil", {"code": "HAND-PASTED-1234"}), ("fail", {"reason": "no luck"})):
        r = await staff.post(f"{JOBS}/{job.id}/{path}", json=body)
        assert r.status_code == 409 and r.json()["error"]["code"] == "job_in_flight"
    await _sql(env, "UPDATE procurement_jobs SET lease_until = now() - interval '1 second'")
    r = await staff.post(f"{JOBS}/{job.id}/fail", json={"reason": "no luck"})
    assert r.status_code == 200 and r.json()["state"] == "failed"
    assert (await _job(up, order)).recover is False  # FATAL: nothing was sent, nothing to recover


async def test_fail_of_a_possibly_sent_job_recovers_and_voids_the_code(env, up):
    staff, _ = await env.login("agent")
    mock = LosingMock("mock_a", lose=1)
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()  # charged at the supplier, the answer was lost
    job = await _job(up, order)
    r = await staff.post(f"{JOBS}/{job.id}/fail", json={"reason": "buyer gave up"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "failed" and r.json()["recovering"] is True
    r = await staff.post(f"{JOBS}/{job.id}/fail", json={"reason": "again"})
    assert r.status_code == 409  # no longer open
    await up.make_due()
    for _ in range(3):
        await up.run()
    job = await _job(up, order)
    assert job.state == "voided" and job.upstream_ref and not job.recover
    assert [op for op, _ in mock.calls] == ["submit", "query", "void"]
    assert not mock.charged and await _cards(env, order) == 0  # exactly one reconcile, no code kept


async def test_hand_fulfil_of_a_possibly_sent_job_records_the_ref_and_keeps_the_code(env, up):
    staff, _ = await env.login("agent")
    mock = LosingMock("mock_a", lose=1)
    await up.supplier(mock)
    buyer, order = await up.paid_order()
    await up.run()
    job = await _job(up, order)
    r = await staff.post(f"{JOBS}/{job.id}/fulfil", json={"code": "HAND-PASTED-1234"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "fulfilled" and r.json()["recovering"] is True
    await up.make_due()
    for _ in range(3):
        await up.run()
    job = await _job(up, order)
    # N2: the reference is recorded, the code is NOT disabled (staff may have pasted exactly it)
    assert job.state == "fulfilled" and job.upstream_status == "unused" and not job.recover
    assert job.upstream_ref and len(job.card_stock_ids) == 1  # still the staff member's code
    assert [op for op, _ in mock.calls] == ["submit", "query"] and len(mock.charged) == 1
    assert (
        await env.scalar(
            "SELECT count(*) FROM audit_log WHERE action = "
            "'procurement.recovered_after_manual_fulfil' AND severity = 'critical'"
        )
        == 1
    )
    assert await env.drain() >= 1
    assert await _order_status(env, order) == "delivered"
    cards = (await buyer.post(f"/api/v1/orders/{order['id']}/reveal")).json()["cards"]
    assert [c["code"] for c in cards] == ["HAND-PASTED-1234"]
    # the closed job cannot be voided by hand while the recovery is pending
    admin, _ = await env.login("admin")
    await _sql(env, "UPDATE procurement_jobs SET recover = true")  # as if it were still pending
    voided = await admin.post(f"{JOBS}/{job.id}/void", json={"reason": "check"})
    assert voided.status_code == 409 and voided.json()["error"]["code"] == "recover_pending"


# ------------------------------------------------------------------ M2
async def test_stale_procure_failed_does_not_hold_a_resolved_order(env, up):
    staff, _ = await env.login("agent")
    mock = MockSupplier("mock_a", outcomes=[Outcome.FATAL])
    await up.supplier(mock)
    buyer, order = await up.paid_order()
    await up.run()
    job = await _job(up, order)
    assert job.state == "needs_manual"  # procure.failed is queued, delivery has not seen it
    r = await staff.post(f"{JOBS}/{job.id}/retry")
    assert r.status_code == 200, r.text
    await up.run()
    assert (await _job(up, order)).state == "fulfilled"
    assert await env.drain() >= 2  # the stale procure.failed, then procure.fulfilled
    assert await _order_status(env, order) == "delivered"
    assert await env.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'delivery.procurement_failed_stale'"
    ) == 1  # fmt: skip
    assert await env.scalar(
        "SELECT count(*) FROM order_events WHERE to_status = 'manual_review'"
    ) == 0  # fmt: skip
    cards = (await buyer.post(f"/api/v1/orders/{order['id']}/reveal")).json()["cards"]
    assert len(cards) == 1


async def test_failed_event_still_holds_an_order_with_an_unresolved_job(env, up):
    await up.supplier(MockSupplier("mock_a", outcomes=[Outcome.FATAL]))
    _buyer, order = await up.paid_order()
    await up.run()
    assert await env.drain() >= 1
    assert await _order_status(env, order) == "manual_review"


async def test_order_held_for_procurement_is_delivered_once_every_unit_is_bound(env, up):
    await up.supplier(MockSupplier("mock_a"))
    buyer, order = await up.paid_order()
    await up.run()
    assert (await _job(up, order)).state == "fulfilled"  # procure.fulfilled is queued
    # delivery processed a (stale) procure.failed first: the order sits in manual_review
    await _sql(env, "UPDATE orders SET status = 'manual_review'")
    await _sql(
        env,
        "INSERT INTO order_events (tenant_id, order_id, from_status, to_status, actor_kind, "
        "reason) SELECT tenant_id, id, 'paid', 'manual_review', 'system', 'procurement_failed' "
        "FROM orders",
    )
    assert await env.drain() >= 1  # procure.fulfilled for the held order
    assert await _order_status(env, order) == "delivered"
    cards = (await buyer.post(f"/api/v1/orders/{order['id']}/reveal")).json()["cards"]
    assert len(cards) == 1
    assert await env.scalar(
        "SELECT count(*) FROM order_events WHERE from_status = 'manual_review' "
        "AND to_status = 'delivered'"
    ) == 1  # fmt: skip
    assert await env.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'delivery.procured_after_hold'"
    ) == 1  # fmt: skip


async def test_order_held_for_another_reason_is_not_released_by_procurement(env, up):
    mock = MockSupplier("mock_a")
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    for _ in range(2):
        await up.run()
    # a payment-anomaly hold (reason is not procurement_failed) placed before delivery ran
    await _sql(env, "UPDATE orders SET status = 'manual_review'")
    await _sql(
        env,
        "INSERT INTO order_events (tenant_id, order_id, from_status, to_status, actor_kind, "
        "reason) SELECT tenant_id, id, 'paid', 'manual_review', 'system', 'amount_mismatch' "
        "FROM orders",
    )
    await env.drain()
    assert await _order_status(env, order) == "manual_review"


# ------------------------------------------------------------------ M3
async def test_offer_cannot_be_enabled_without_a_usable_cost(env):
    from tests.test_procurement_sources import SRC, _body, _create

    admin, _ = await env.login("admin")
    product = await env.product(mode="upstream_cdk")
    sup = (await _create(admin))["id"]
    r = await admin.post(SRC, json=_body(product, sup, cost_estimate=None, enabled=True))
    assert r.status_code == 422 and r.json()["error"]["code"] == "cost_required"
    r = await admin.post(SRC, json=_body(product, sup, cost_estimate=None))  # disabled is fine
    assert r.status_code == 201, r.text
    oid = r.json()["id"]
    r = await admin.patch(f"{SRC}/{oid}", json={"enabled": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "cost_required"
    r = await admin.patch(f"{SRC}/{oid}", json={"enabled": True, "cost_estimate": "2.5"})
    assert r.status_code == 200 and r.json()["enabled"] is True
    # an enabled source never loses its cost, nor keeps one in the wrong currency
    for patch in ({"clear_cost": True}, {"currency": "PHP"}):
        r = await admin.patch(f"{SRC}/{oid}", json=patch)
        assert r.status_code == 422 and r.json()["error"]["code"] == "cost_required"
    # a cost the supplier named itself (set by the availability check) is enough
    await _sql(env, "UPDATE supplier_offers SET cost_estimate = NULL, enabled = false")
    await _sql(env, "UPDATE supplier_offers SET cost_known = true")
    r = await admin.patch(f"{SRC}/{oid}", json={"enabled": True})
    assert r.status_code == 200, r.text
    # editing the plan drops that knowledge again
    await _sql(env, "UPDATE supplier_offers SET enabled = false")
    r = await admin.patch(f"{SRC}/{oid}", json={"params": {"plan": "go"}})
    assert r.status_code == 200
    r = await admin.patch(f"{SRC}/{oid}", json={"enabled": True})
    assert r.status_code == 422


async def test_availability_check_records_a_supplier_named_cost(env, up):
    from decimal import Decimal

    from app.procurement import service
    from app.suppliers.base import Money

    named = MockSupplier("mock_a", redemption=Money(Decimal("2.00"), "USD"))
    _, offer_a = await up.supplier(named, code="mock_a", priority=10)
    silent = MockSupplier("mock_b")
    _, offer_b = await up.supplier(silent, code="mock_b", priority=20)
    await _sql(env, "UPDATE supplier_offers SET cost_estimate = NULL")
    for oid in (offer_a, offer_b):
        await service.check_offer(up.ctx, env.tenant_id, oid)
    known = {
        r[0]: r[1] for r in (await _sql(env, "SELECT id, cost_known FROM supplier_offers")).all()
    }
    assert known == {offer_a: True, offer_b: False}


async def test_no_new_purchase_while_the_balance_reading_is_stale(env, up):
    from tests.test_procurement_breaker import _refresh

    mock = MockSupplier("mock_a")
    sid, _ = await up.supplier(mock)
    await _sql(env, "UPDATE suppliers SET last_balance_at = now() - interval '1 hour'")
    _buyer, order = await up.paid_order()
    for _ in range(2):
        await up.run()
        await up.make_due()
    job = await _job(up, order)
    assert job.state == "queued" and job.error_code == "balance_stale" and not job.maybe_sent
    assert mock.calls == []  # nothing leased, nothing sent
    await _refresh(up, sid)  # a fresh, good reading
    await up.make_due()
    await up.run()
    assert (await _job(up, order)).state == "fulfilled"
    # a missing reading (never read, or the last read failed long ago) blocks the same way
    await _sql(env, "UPDATE suppliers SET last_balance_at = NULL")
    _b2, order2 = await up.paid_order()
    await up.run()
    job2 = await _job(up, order2)
    assert job2.state == "queued" and job2.error_code == "balance_stale"


async def test_balance_breaker_parks_queued_jobs_and_they_resume_by_themselves(env, up):
    mock = MockSupplier("mock_a")
    await up.supplier(mock)
    _buyer, order = await up.paid_order()  # bought while the supplier was fine
    await _create_jobs_only(up)
    await _sql(
        env,
        "UPDATE suppliers SET breaker_open = true, breaker_kind = 'balance', "
        "breaker_reason = 'balance_low'",
    )
    for _ in range(2):
        await up.run()
        await up.make_due()
    job = await _job(up, order)
    assert job.state == "queued" and job.error_code == "balance_breaker"  # not needs_manual
    assert not job.maybe_sent and mock.calls == []
    assert await env.scalar("SELECT count(*) FROM outbox WHERE topic = 'procure.failed'") == 0
    await _sql(env, "UPDATE suppliers SET breaker_open = false, breaker_kind = NULL")  # recovered
    await up.make_due()
    await up.run()
    assert (await _job(up, order)).state == "fulfilled"
    # the same wait applies to a job created while the supplier is already paused
    _b2, order2 = await up.paid_order()
    await _sql(
        env,
        "UPDATE suppliers SET breaker_open = true, breaker_kind = 'balance', "
        "breaker_reason = 'balance_low'",
    )
    await up.run()
    job2 = await _job(up, order2)
    assert job2.state == "queued" and job2.error_code == "balance_breaker"


async def test_hard_breaker_still_fails_a_never_sent_job_over(env, up):
    a = MockSupplier("mock_a")
    b = MockSupplier("mock_b")
    await up.supplier(a, code="mock_a", priority=10)
    await up.supplier(b, code="mock_b", priority=20)
    await _sql(
        env,
        "UPDATE suppliers SET breaker_open = true, breaker_kind = 'hard', breaker_reason = 'x' "
        "WHERE code = 'mock_a'",
    )
    _buyer, order = await up.paid_order()
    for _ in range(3):
        await up.run()
    assert (await _job(up, order)).state == "fulfilled" and a.calls == [] and len(b.charged) == 1


async def test_healthy_source_is_preferred_over_one_paused_by_balance(env, up):
    a = MockSupplier("mock_a")
    b = MockSupplier("mock_b")
    await up.supplier(a, code="mock_a", priority=10)
    await up.supplier(b, code="mock_b", priority=20)
    await _sql(
        env,
        "UPDATE suppliers SET breaker_open = true, breaker_kind = 'balance', "
        "breaker_reason = 'balance_low' WHERE code = 'mock_a'",
    )
    _buyer, order = await up.paid_order()
    for _ in range(3):
        await up.run()
    assert (await _job(up, order)).state == "fulfilled" and a.calls == [] and len(b.charged) == 1


# ------------------------------------------------------------------ lows
async def test_l1_reference_is_stored_before_a_refused_seal_and_the_loop_is_capped(env, up):
    mock = MockSupplier("mock_a")
    await up.supplier(mock)
    up.ctx.sealer = None  # nothing can be sealed
    _buyer, order = await up.paid_order()
    await up.run()
    job = await _job(up, order)
    assert job.state == "submitting" and job.error_code == "seal_unavailable"
    assert job.upstream_ref and job.cost is not None  # findable, so it can be disabled
    assert await _cards(env, order) == 0
    await _sql(env, "UPDATE procurement_jobs SET attempts = 11")
    await up.make_due()
    await up.run()
    job = await _job(up, order)
    assert job.state == "needs_manual" and job.error_code == "seal_unavailable"
    assert job.upstream_ref  # staff can void the code the engine could not keep
    staff, _ = await env.login("admin")
    detail = (await staff.get(f"{JOBS}/{job.id}")).json()
    assert detail["charged_possible"] is True and "void" in detail["actions"]


async def test_l2_void_makes_the_sealed_code_undeliverable(env, up):
    admin, _ = await env.login("admin")
    mock = MockSupplier("mock_a")
    await up.supplier(mock)
    buyer, order = await up.paid_order()
    await up.run()  # fulfilled, procure.fulfilled queued but not delivered yet
    job = await _job(up, order)
    r = await admin.post(f"{JOBS}/{job.id}/void", json={"reason": "supplier recalled the code"})
    assert r.status_code == 200 and r.json()["state"] == "void_pending", r.text
    row = (
        await _sql(env, "SELECT status, void_reason, voided_by FROM card_stock WHERE order_id = :o"
                   .replace(":o", f"'{order['id']}'"))
    ).one()  # fmt: skip
    assert row.status == "void" and row.void_reason.startswith("procurement_void")
    assert row.voided_by is not None
    await up.run()
    assert (await _job(up, order)).state == "voided" and not mock.charged
    await env.drain()  # delivery: the voided code is never delivered
    assert await _order_status(env, order) != "delivered"
    cards = (await buyer.post(f"/api/v1/orders/{order['id']}/reveal")).json().get("cards", [])
    assert cards == []


async def test_l2_void_of_a_possibly_sent_job_goes_through_the_supplier(env, up):
    admin, _ = await env.login("admin")
    mock = LosingMock("mock_a", lose=1)
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()  # charged, answer lost
    await _sql(env, "UPDATE procurement_jobs SET state = 'needs_manual', error_code = 'unresolved'")
    job = await _job(up, order)
    r = await admin.post(f"{JOBS}/{job.id}/void", json={"reason": "check supplier"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "failed" and r.json()["recovering"] is True  # not voided yet
    await up.make_due()
    for _ in range(3):
        await up.run()
    job = await _job(up, order)
    assert job.state == "voided" and job.upstream_status == "disabled"
    assert [op for op, _ in mock.calls] == ["submit", "query", "void"] and not mock.charged


async def test_l4_actions_on_two_jobs_of_one_order_do_not_deadlock(env, up):
    staff, _ = await env.login("agent")
    await up.supplier(MockSupplier("mock_a", outcomes=[Outcome.FATAL, Outcome.FATAL]))
    _buyer, order = await up.paid_order(qty=2)
    await up.run()
    j1, j2 = await up.jobs(order["id"])
    assert j1.state == j2.state == "needs_manual"
    await _sql(env, "UPDATE procurement_jobs SET maybe_sent = false")
    rs = await asyncio.wait_for(
        asyncio.gather(
            staff.post(f"{JOBS}/{j1.id}/fail", json={"reason": "no stock"}),
            staff.post(f"{JOBS}/{j2.id}/fail", json={"reason": "no stock"}),
            _refund(env, order),
        ),
        timeout=30,
    )
    assert [r.status_code for r in rs[:2]] == [200, 200]
    assert rs[2].status_code in (200, 409)  # busy answers 409 instead of waiting


async def test_l5_backfill_continues_past_a_page_without_new_rows(env, up):
    from app.devtools.mock_zovocard import MockZovo
    from app.procurement import ledger
    from app.procurement.service import backfill_ledgers
    from tests.test_procurement_ledger import _rows, _zovo

    vendor = MockZovo()
    now = datetime.now(timezone.utc)
    vendor.balance_logs = [
        {"id": i, "created_at": (now - timedelta(minutes=150 - i)).isoformat(), "amount": -1,
         "type": "recharge"}
        for i in range(150, 0, -1)
    ]  # fmt: skip
    sid, _ = await _zovo(up, vendor)
    async with env.owner_session() as db:  # webhooks booked the newest 100 rows
        for entry in vendor.balance_logs[:100]:
            await ledger.handle_balance_event(
                db,
                tenant_id=env.tenant_id,
                supplier_id=sid,
                payload={
                    "balance_log_id": entry["id"], "type": "recharge", "amount": "-1",
                    "occurred_at": entry["created_at"],
                },
                received_at=None,
            )  # fmt: skip
        await db.commit()
    assert len(await _rows(env)) == 100
    assert await backfill_ledgers(up.ctx) == 50  # the older 50 sit on page 2: not given up
    assert len(await _rows(env)) == 150
    assert await backfill_ledgers(up.ctx) == 0


async def test_l5_margin_excludes_refunded_orders(env, up):
    from tests.test_procurement_cost import COST, _setup

    admin, _ = await env.login("admin")
    _sid, order, _unused = await _setup(env, up)
    today = datetime.now(timezone.utc).date()
    window = {"date_from": f"{today.year}-01-01", "date_to": today.isoformat()}
    assert len((await admin.get(f"{COST}/margin", params=window)).json()["items"]) == 1
    await _sql(env, "UPDATE orders SET status = 'refunded' WHERE id = :o", o=uuid.UUID(order["id"]))
    body = (await admin.get(f"{COST}/margin", params=window)).json()
    assert body["items"] == [] and body["total"] == 0


async def test_l7_idempotency_conflict_is_fatal_and_stays_maybe_charged(env, up):
    from app.suppliers.zovocard.errors import classify

    rule = classify(409, "IDEMPOTENCY_CONFLICT", write=True)
    assert rule.outcome is Outcome.FATAL and rule.supplier_stop is False
    assert classify(409, None, write=True).outcome is Outcome.AMBIGUOUS  # still "running"

    class Conflict(MockSupplier):
        async def submit(self, job):
            self.calls.append(("submit", job.idempotency_key))
            return Result(Outcome.FATAL, error_code="IDEMPOTENCY_CONFLICT")

    mock = Conflict("mock_a")
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()
    job = await _job(up, order)
    assert job.state == "needs_manual" and job.error_code == "IDEMPOTENCY_CONFLICT"
    assert job.maybe_sent  # the first request under that key may have executed
    staff, _ = await env.login("agent")
    detail = (await staff.get(f"{JOBS}/{job.id}")).json()
    assert detail["charged_possible"] is True and "switch" not in detail["actions"]
    # a plain FATAL (nothing executed) is still not flagged
    assert [op for op, _ in mock.calls] == ["submit"]


async def test_l2_api_may_void_a_sold_code_only_for_a_job_being_voided(env, up):
    """The card_stock guard keeps its rule: only the narrow procurement exception is added."""
    import pytest

    await up.supplier(MockSupplier("mock_a"))
    _buyer, order = await up.paid_order()
    await up.run()
    job = await _job(up, order)
    (card_id,) = job.card_stock_ids
    q = "UPDATE card_stock SET status = 'void', void_reason = :r, voided_at = now() WHERE id = :c"
    async with env.api_session(env.tenant_id) as db:
        for reason in ("procurement_void:x", "something_else"):  # job is `fulfilled`: refused
            with pytest.raises(Exception, match="may only void"):
                await db.execute(text(q), {"c": card_id, "r": reason})
            await db.rollback()
        await db.execute(text("UPDATE procurement_jobs SET state = 'void_pending', step = 'void'"))
        with pytest.raises(Exception, match="may only void"):  # wrong reason prefix
            await db.execute(text(q), {"c": card_id, "r": "something_else"})
        await db.rollback()
        await db.execute(text("UPDATE procurement_jobs SET state = 'void_pending', step = 'void'"))
        await db.execute(text(q), {"c": card_id, "r": "procurement_void:ok"})
        with pytest.raises(Exception, match="may only void"):  # a void card cannot move again
            await db.execute(
                text("UPDATE card_stock SET status = 'sold' WHERE id = :c"), {"c": card_id}
            )
        await db.rollback()


# ------------------------------------------------------------------ re-audit (2026-11-15)
async def test_n4_failed_evaluation_leaves_the_freshness_gate_stale(env, up, monkeypatch):
    from app.procurement import breaker
    from tests.test_procurement_breaker import _refresh

    mock = MockSupplier("mock_a")
    sid, _ = await up.supplier(mock)
    await _sql(env, "UPDATE suppliers SET last_balance_at = now() - interval '1 hour'")

    async def boom(*a, **k):
        raise RuntimeError("evaluation failed")

    monkeypatch.setattr(breaker, "evaluate", boom)
    await _refresh(up, sid)  # a good balance read, but the breaker could not evaluate it
    stale = await env.scalar(
        "SELECT last_balance_at < now() - interval '30 minutes' FROM suppliers WHERE id = :s",
        s=sid,
    )
    assert stale is True
    _buyer, order = await up.paid_order()
    await up.run()
    job = await _job(up, order)
    assert job.state == "queued" and job.error_code == "balance_stale" and mock.calls == []
    monkeypatch.undo()
    await _refresh(up, sid)
    await up.make_due()
    await up.run()
    assert (await _job(up, order)).state == "fulfilled"


async def _due_recovery(env) -> None:
    await _sql(
        env,
        "UPDATE procurement_jobs SET next_poll_at = now() - interval '1 second' WHERE recover",
    )


async def test_n1_recovery_honours_the_kill_switches_then_needs_manual(env, up):
    for kill in (
        "UPDATE suppliers SET enabled = false WHERE id = (SELECT supplier_id FROM procurement_jobs "
        "WHERE id = :j)",
        "UPDATE suppliers SET breaker_open = true, breaker_kind = 'hard', breaker_reason = 'x' "
        "WHERE id = (SELECT supplier_id FROM procurement_jobs WHERE id = :j)",
    ):
        mock = LosingMock("mock_a", lose=1)
        await up.supplier(mock, code=f"m{uuid.uuid4().hex[:6]}")
        staff, _ = await env.login("agent")
        _buyer, order = await up.paid_order()
        await up.run()  # charged at the supplier, the answer was lost
        job = await _job(up, order)
        r = await staff.post(f"{JOBS}/{job.id}/fail", json={"reason": "gave up"})
        assert r.status_code == 200 and r.json()["recovering"] is True
        await _sql(
            env,
            kill,
            j=job.id,
        )
        for _ in range(2):
            await _due_recovery(env)
            await up.run()
        job = await _job(up, order)
        assert job.state == "failed" and job.recover and job.error_code == "recover_blocked"
        assert [op for op, _ in mock.calls] == ["submit"]  # no recovery call while blocked
        await _sql(env, "UPDATE procurement_jobs SET attempts = 20 WHERE id = :j", j=job.id)
        await _due_recovery(env)
        await up.run()
        job = await _job(up, order)
        assert job.state == "needs_manual" and job.error_code == "recover_blocked"
        assert job.maybe_sent and not job.recover
        assert [op for op, _ in mock.calls] == ["submit"]
        assert len(mock.charged) == 1  # maybe charged, never asked again
        await _sql(env, "DELETE FROM procurement_jobs")


async def test_n6_recovery_idempotency_conflict_keeps_the_job_maybe_charged(env, up):
    class Conflict(LosingMock):
        async def query(self, job):
            self.calls.append(("query", job.idempotency_key))
            return Result(Outcome.FATAL, error_code="IDEMPOTENCY_CONFLICT")

    staff, _ = await env.login("agent")
    mock = Conflict("mock_a", lose=1)
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()
    job = await _job(up, order)
    r = await staff.post(f"{JOBS}/{job.id}/fail", json={"reason": "gave up"})
    assert r.status_code == 200 and r.json()["recovering"] is True
    await _due_recovery(env)
    await up.run()
    job = await _job(up, order)
    assert job.state == "needs_manual" and job.maybe_sent and not job.recover
    assert job.error_code == "IDEMPOTENCY_CONFLICT"


async def test_n7_a_retryable_submit_is_capped_after_a_passed_precheck(env, up):
    class Busy(MockSupplier):
        async def submit(self, job):
            self.calls.append(("submit", job.idempotency_key))
            return Result(Outcome.RETRYABLE, error_code="busy")

    mock = Busy("mock_a")
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    for _ in range(14):
        await up.run()
        await up.make_due()
    job = await _job(up, order)
    assert job.state == "needs_manual" and job.error_code == "retry_exhausted"
    assert not job.maybe_sent and len([c for c in mock.calls if c[0] == "submit"]) <= 11


async def test_m5_missing_request_hash(env, up):
    mock = LosingMock("mock_a", lose=1)
    await up.supplier(mock)
    _b, sent = await up.paid_order()
    await up.run()  # sent, the answer was lost: reconcile pending
    assert (await _job(up, sent)).maybe_sent
    await _sql(env, "UPDATE procurement_jobs SET request_hash = NULL")
    await up.make_due()
    await up.run()
    job = await _job(up, sent)
    assert job.state == "needs_manual" and job.error_code == "request_hash_missing"
    assert job.maybe_sent and [op for op, _ in mock.calls] == ["submit"]  # never replayed
    # a never-sent job gets its hash at the first lease
    _b2, fresh = await up.paid_order()
    await _create_jobs_only(up)
    await _sql(
        env, "UPDATE procurement_jobs SET request_hash = NULL WHERE order_id = :o",
        o=uuid.UUID(fresh["id"]),
    )  # fmt: skip
    await up.run()
    job = await _job(up, fresh)
    assert job.state == "fulfilled" and job.request_hash


async def test_n5_refund_voids_a_fulfilled_but_undelivered_unit(env, up):
    mock = MockSupplier("mock_a")
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()  # fulfilled; delivery has not run yet
    job = await _job(up, order)
    assert job.state == "fulfilled" and len(mock.charged) == 1
    assert await _order_status(env, order) == "paid" and await _cards(env, order) == 1
    r = await _refund(env, order)
    assert r.status_code == 200, r.text
    job = await _job(up, order)
    assert job.state == "void_pending" and job.step == "void"
    assert await env.scalar(
        "SELECT count(*) FROM card_stock WHERE order_id = :o AND status = 'void'",
        o=uuid.UUID(order["id"]),
    ) == 1  # fmt: skip
    await up.make_due()
    for _ in range(2):
        await up.run()
    job = await _job(up, order)
    assert job.state == "voided" and job.upstream_status == "disabled" and not mock.charged
    assert await env.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'procurement.cards_voided'"
    ) == 1  # fmt: skip
    await env.drain()
    assert await _order_status(env, order) == "refunded"


async def test_n5_refund_leaves_a_delivered_unit_alone(env, up):
    mock = MockSupplier("mock_a")
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()
    assert await env.drain() >= 1
    assert await _order_status(env, order) == "delivered"
    r = await _refund(env, order)
    assert r.status_code in (200, 409, 422), r.text
    job = await _job(up, order)
    assert job.state == "fulfilled" and len(mock.charged) == 1


async def test_n8_parked_job_fails_over_to_a_healthy_source_and_alerts_when_stuck(env, up):
    a = MockSupplier("mock_a")
    await up.supplier(a, code="mock_a", priority=10)
    _buyer, order = await up.paid_order()
    await _create_jobs_only(up)  # queued on A, nothing sent
    pause = (
        "UPDATE suppliers SET breaker_open = true, breaker_kind = 'balance', "
        "breaker_reason = 'balance_low' WHERE code = 'mock_a'"
    )
    await _sql(env, pause)
    await up.run()
    job = await _job(up, order)
    assert job.state == "queued" and job.error_code == "balance_breaker"  # no alternative: parked
    # a long park alerts like any stuck job
    await _sql(env, "UPDATE procurement_jobs SET created_at = now() - interval '2 hours'")
    await up.make_due()
    await up.run()
    assert await env.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'procurement.job_stuck'"
    ) == 1  # fmt: skip
    # a healthy alternative appears: the parked job moves there at once (nothing was sent)
    b = MockSupplier("mock_b")
    await up.supplier(b, code="mock_b", priority=20)
    await up.make_due()
    for _ in range(3):
        await up.run()
    job = await _job(up, order)
    assert job.state == "fulfilled" and a.calls == [] and len(b.charged) == 1


async def test_n3_precheck_reads_a_fresh_catalog_not_the_cache(monkeypatch):
    from app.devtools import mock_zovocard
    from app.suppliers.base import OfferSpec
    from tests.test_suppliers_v12 import _supplier

    s = _supplier()
    offer = OfferSpec("cdk", {"product": "gpt", "plan": "plus"})
    assert (await s.availability(offer)).available  # fills the 60 s cache
    real = mock_zovocard.plans_fixture

    def paused(product, scenarios=False, full=False):
        data = real(product, scenarios, full)
        for row in data["registry"]:
            if row["key"] == "plus":
                row["purchasable"] = False
        return data

    monkeypatch.setattr(mock_zovocard, "plans_fixture", paused)
    assert (await s.availability(offer)).available  # the cache still says yes
    fresh = await s.availability_fresh(offer)
    assert not fresh.available and fresh.reason == "not_purchasable"
    assert not (await s.availability(offer)).available  # and the cache was refreshed


class _PausesOnSubmit(MockSupplier):
    """The write fails ambiguously (``request_failed``) and the plan is paused meanwhile."""

    async def submit(self, job):
        self.calls.append(("submit", job.idempotency_key))
        self.purchasable = set()
        return Result(Outcome.AMBIGUOUS, error_code="request_failed")


async def test_failed_write_stays_maybe_sent_even_when_the_plan_is_gone_afterwards(env, up):
    """Re-audit F1: the plan may be gone BECAUSE the write ran, so there is no failover."""
    a = _PausesOnSubmit("mock_a")
    b = MockSupplier("mock_b")
    _sid, offer_a = await up.supplier(a, code="mock_a", priority=10)
    await up.supplier(b, code="mock_b", priority=20)
    _buyer, order = await up.paid_order()
    for _ in range(4):
        await up.run()
    job = await _job(up, order)
    assert job.maybe_sent and job.state != "fulfilled" and not job.tried_offer_ids
    assert b.calls == [] and len(b.charged) == 0  # the second supplier was never asked
    assert await _offer_check(env, offer_a) is False  # the offer stops taking new jobs
