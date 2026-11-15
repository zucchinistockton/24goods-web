"""review-v12 M2 exit: the mock supplier's scenario plans (the ones the E2E smoke drives) through
the real ZovoCard adapter and the engine: timeout after send, 409, 202, 503 failover. Each ends
with exactly ONE code issued upstream and the order delivered."""

from __future__ import annotations

# ruff: noqa: F811  (fixtures are imported from test_procurement_m2)
import httpx
import pytest

from app.devtools.mock_zovocard import OK_KEY, MockZovo, create_mock_zovocard
from app.suppliers.zovocard import TokenBucket, ZovoCardSupplier, ZovoClient, ZovoConfig
from tests.test_procurement_m2 import Up, _status, up  # noqa: F401


async def _zovo(up: Up, vendor: MockZovo, code: str, plan: str, priority: int = 10):
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_mock_zovocard(vendor)))
    supplier = ZovoCardSupplier(
        "zovocard",
        ZovoClient(
            ZovoConfig(base_url="http://m/openapi/v1", app_secret=OK_KEY),
            http=http,
            bucket=TokenBucket(6000, 100),
        ),
    )
    up.vendor_http = http
    return await up.supplier(
        supplier, code=code, adapter="zovocard", params={"plan": plan}, priority=priority
    )


async def _deliver(env, up: Up, order, buyer, rounds: int = 6) -> None:
    for _ in range(rounds):
        await up.run()
        await env.drain()
        if await _status(buyer, order) == "delivered":
            return
        await up.make_due()
    raise AssertionError(f"order stuck in {await _status(buyer, order)}")


@pytest.mark.parametrize("plan", ["scn_lost", "scn_409", "scn_202"])
async def test_ambiguous_first_answer_ends_with_one_code(env, up, plan):
    vendor = MockZovo(scenarios=True)
    await _zovo(up, vendor, "zovocard", plan)
    buyer, order = await up.paid_order()
    await _deliver(env, up, order, buyer)
    assert vendor.executions == 1  # the same-key replay never issued a second code
    (job,) = await up.jobs(order["id"])
    assert job.state == "fulfilled" and len(job.card_stock_ids) == 1
    assert job.idempotency_key.endswith(":submit")
    sent = [k for m, p, h in vendor.requests if m == "POST" for k in [h["idempotency_key"]]]
    assert len(set(sent)) == 1 and len(sent) >= 2  # retried, always under one key


async def test_channel_unavailable_fails_over_to_the_next_source(env, up):
    vendor_a, vendor_b = MockZovo(scenarios=True), MockZovo(scenarios=True)
    sid_a, _ = await _zovo(up, vendor_a, "zovocard", "scn_503", priority=10)
    sid_b, oid_b = await _zovo(up, vendor_b, "zovocard_b", "plus", priority=20)
    buyer, order = await up.paid_order()
    await _deliver(env, up, order, buyer)
    assert vendor_a.executions == 0 and vendor_b.executions == 1  # nothing charged at A
    (job,) = await up.jobs(order["id"])
    assert job.state == "fulfilled" and job.supplier_id == sid_b and job.offer_id == oid_b
    assert job.idempotency_key.endswith(":s2")  # a fresh key for the supplier that never saw it
    assert job.tried_offer_ids and sid_a != sid_b


async def test_scenario_plans_exist_only_when_enabled():
    plain = MockZovo()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_mock_zovocard(plain))
    ) as http:
        s = ZovoCardSupplier(
            "zovocard",
            ZovoClient(
                ZovoConfig(base_url="http://m/openapi/v1", app_secret=OK_KEY),
                http=http,
                bucket=TokenBucket(6000, 100),
            ),
        )
        cat = await s.plans("gpt", fresh=True)
        assert not any(k.startswith("scn_") for k in cat.registry)
