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
