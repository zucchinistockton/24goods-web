"""review-v12 M2: void_pending in the engine (staff void), job grants of app_api."""

from __future__ import annotations

# ruff: noqa: F811  (fixtures are imported from test_procurement_m2)
from sqlalchemy import text

from app.suppliers.base import JobSpec, Outcome, Result
from app.suppliers.mock import MockSupplier
from tests.test_procurement_m2 import Up, up  # noqa: F401


async def _sql(env, sql: str, **params):
    async with env.owner_session() as db:
        res = await db.execute(text(sql), params)
        await db.commit()
        return res


async def _job(up: Up):
    (job,) = await up.jobs()
    return job


async def test_void_pending_disables_upstream_with_its_own_key(env, up):
    mock = MockSupplier("mock_a")
    await up.supplier(mock)
    await up.paid_order()
    await up.run()
    job = await _job(up)
    assert job.state == "fulfilled" and job.upstream_ref
    await _sql(
        env,
        "UPDATE procurement_jobs SET state = 'void_pending', step = 'void', next_poll_at = now()",
    )
    await up.run()
    job = await _job(up)
    assert job.state == "voided" and job.upstream_status == "disabled"
    voids = [k for op, k in mock.calls if op == "void"]
    assert voids == [f"pj:{job.id.hex}:void"] and voids[0] != job.idempotency_key
    # voided frees the unit slot (partial unique index)
    assert await env.scalar("SELECT count(*) FROM procurement_jobs WHERE state <> 'voided'") == 0


async def test_void_rejected_puts_the_job_back(env, up):
    class Rejecting(MockSupplier):
        async def void(self, job: JobSpec) -> Result:
            self.calls.append(("void", job.idempotency_key))
            return Result(Outcome.FATAL, error_code="CDK_DISABLE_REJECTED")

    await up.supplier(Rejecting("mock_a"))
    await up.paid_order()
    await up.run()
    await _sql(
        env,
        "UPDATE procurement_jobs SET state = 'void_pending', step = 'void', next_poll_at = now()",
    )
    await up.run()
    job = await _job(up)
    assert job.state == "fulfilled" and job.error_code == "void_rejected:CDK_DISABLE_REJECTED"
    assert job.card_stock_ids


async def test_void_retries_with_the_same_key_then_gives_up(env, up):
    class Flaky(MockSupplier):
        fail = 2

        async def void(self, job: JobSpec) -> Result:
            self.calls.append(("void", job.idempotency_key))
            if self.fail > 0:
                self.fail -= 1
                return Result(Outcome.AMBIGUOUS, error_code="timeout_after_send")
            return await super().void(job)

    mock = Flaky("mock_a")
    await up.supplier(mock)
    await up.paid_order()
    await up.run()
    await _sql(
        env,
        "UPDATE procurement_jobs SET state = 'void_pending', step = 'void', next_poll_at = now()",
    )
    for _ in range(3):
        await up.run()
        await up.make_due()
    job = await _job(up)
    assert job.state == "voided"
    keys = {k for op, k in mock.calls if op == "void"}
    assert len(keys) == 1 and mock.fail == 0


async def test_app_api_job_grants_are_narrow(env, up):
    import pytest

    await up.supplier(MockSupplier("mock_a"))
    await up.paid_order()
    await up.run()
    async with env.api_session(env.tenant_id) as db:
        await db.execute(text("UPDATE procurement_jobs SET state = 'fulfilled', step = 'done'"))
        await db.commit()
        for sql in (
            "UPDATE procurement_jobs SET cost = 1",
            "UPDATE procurement_jobs SET order_id = order_id, kind = kind",
            "DELETE FROM procurement_jobs",
            "UPDATE supplier_ledger SET amount = 0",
            "DELETE FROM supplier_events",
        ):
            with pytest.raises(Exception, match=r"permission denied"):
                await db.execute(text(sql))
            await db.rollback()
