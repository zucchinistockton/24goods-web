"""Payment-audit fixes on v12 M2 (2026-11-15): H1, H2, M1-M5 and the low findings."""

from __future__ import annotations

# ruff: noqa: F811  (fixtures are imported from test_procurement_m2)
import asyncio
import os
import uuid

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

    cols = ("maybe_sent", "recover", "lease_until", "first_sent_at")

    async def state() -> tuple[set[str], bool]:
        conn = await asyncpg.connect(owner_url.replace("+asyncpg", ""), timeout=5)
        try:
            have = {
                r["column_name"]
                for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'procurement_jobs'"
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
