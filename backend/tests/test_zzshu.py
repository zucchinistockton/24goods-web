"""ADR-054: card.zzshu.pro as the standby provider of ChatGPT direct top-ups.

Real ZovoCard adapter against ``mock_zovocard`` (the wallet and the dedicated cards), real zzshu
adapter against ``mock_zzshu``, the real engine. Covers the pre-charge fallback (and its absence
when anything may have charged), the automatic primary switch with its cool-down, the primary
switch by priority, the dedicated card (margin, sweep, audit), the lost create answer, the points
gate, unsupported plans / regions and the secrets hygiene (PAN / CVV / session never in logs, audit
rows or the database)."""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal

import httpx
import pytest

from app.core import funding as fs
from app.devtools.mock_zovocard import MockZovo
from app.devtools.mock_zzshu import OK_KEY, MockZzshu, create_mock_zzshu
from app.suppliers.base import OfferSpec
from app.suppliers.zzshu import ZzshuClient, ZzshuConfig, ZzshuSupplier, zzshu_spec
from tests.test_procurement_card_modes import _audit, _funding
from tests.test_procurement_direct import (
    _drive,
    _job,
    _settle,
    _sql,
    dup,  # noqa: F401  (fixture)
    gpt_session,
    new_marker,
)
from tests.test_procurement_direct import _vendor as _zovo_vendor
from tests.test_procurement_vcc import _rows, _zovo

GPT = {"product": "gpt", "plan": "plus", "card_id": 4242}
ZZ = {"product": "gpt", "plan": "plus", "payment_country": "PH"}


def _vendor() -> MockZovo:
    v = _zovo_vendor()
    v.card_cvv = "7319"
    v.rate_per_min = 100_000  # the mock's own rate limit (100 / min) is not under test
    return v


async def _both(env, dup, vendor, zz, *, zz_priority=20, zovo_priority=10, zovo=True):  # noqa: F811
    """ZovoCard (fixed funding card 4242, GPT plus) and zzshu (plus / PH) as two sources of the
    one product; the funding switches on, as production has them."""
    await _funding(env, auto_recharge=True)
    zsid = None
    if zovo:
        zsid, _o = await _zovo(dup, vendor, GPT, priority=zovo_priority)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_mock_zzshu(zz)))
    client = ZzshuClient(
        ZzshuConfig(base_url="http://mockzz/api/v1/third-party", api_key=OK_KEY, env="sandbox"),
        http=http,
    )
    if zsid is None:  # zzshu alone: it still needs the ZovoCard wallet to open its cards
        zsid, _o = await _zovo(dup, vendor, GPT, priority=zovo_priority)
        await _sql(env, "UPDATE supplier_offers SET enabled = false WHERE supplier_id = :s", s=zsid)
    sup = ZzshuSupplier("zzshu", client, dup.mocks[zsid])
    sup.status_recheck_s = 0
    dup.zz_http = http
    sid, oid = await dup.supplier(
        sup, code="zzshu", adapter="zzshu", params=ZZ, priority=zz_priority
    )
    return zsid, sid, oid


async def _order(dup, marker=None):  # noqa: F811
    return await dup.paid_direct(gpt_session(marker or new_marker()))


def _zovo_failing(vendor: MockZovo, end="failed_precharge", fee="released") -> None:
    vendor.direct_end, vendor.direct_fee_status = end, fee


async def _card_rows(env):
    """The dedicated card's audit rows: ``(opened, swept)`` details, oldest first."""
    rows = [
        r[0]
        for r in await _rows(
            env,
            "SELECT detail FROM audit_log WHERE action = 'procurement.zzshu_card' ORDER BY id",
        )
    ]
    assert len(rows) == 2, rows
    assert rows[0]["swept"] is False and rows[0]["loaded"] and rows[1]["swept"] is True
    return rows[0], rows[1]


async def _wait(env, dup, order, buyer, want="delivered"):  # noqa: F811
    try:
        await _drive(env, dup, order, buyer, want=want, rounds=14)
    except AssertionError as exc:
        jobs = await dup.jobs(order["id"])
        raise AssertionError(
            f"{exc}; jobs={[(j.state, j.step, j.error_code, j.upstream_ref) for j in jobs]}"
        ) from None


# ------------------------------------------------------------------ pure parts
def test_plan_and_region_mapping_and_unsupported_values():
    def spec(plan, region="PH", product="gpt", **extra):
        return zzshu_spec(
            OfferSpec(
                kind="direct",
                params={"product": product, "plan": plan, "payment_country": region, **extra},
            )
        )

    for ours, theirs in (("go", "go"), ("plus", "plus"), ("pro_5x", "pro5"), ("pro_20x", "pro20")):
        assert spec(ours).plan_type == theirs
    for region in ("PH", "US", "EG", "CL"):
        assert spec("plus", region).region == region
    # unsupported: JP, plus_half, pro_50x, the upgrade types, Claude / Grok, a card parameter
    assert spec("plus", "JP") is None
    assert spec("plus_half") is None and spec("pro_50x") is None
    assert spec("plus_to_5x") is None and spec("to_20x") is None
    assert spec("pro", product="claude") is None and spec("premium", product="grok") is None
    assert spec("plus", card_id=4242) is None and spec("plus", card_mode="auto_existing") is None
    assert zzshu_spec(OfferSpec(kind="cdk", params=ZZ)) is None
    assert zzshu_spec(OfferSpec(kind="direct", params={"product": "gpt", "plan": "plus"})) is None


def test_margin_setting_is_validated_and_defaults_high():
    assert fs.normalize(None)["zzshu_card_margin_pct"] == "20"
    assert fs.validate({"zzshu_card_margin_pct": "35"})["zzshu_card_margin_pct"] == "35.00"
    assert fs.validate({"zzshu_card_margin_pct": 0})["zzshu_card_margin_pct"] == "0.00"
    for bad in (-1, 100.01, "x", None, True):
        with pytest.raises(ValueError):
            fs.validate({"zzshu_card_margin_pct": bad})
    # a stored bad value falls back to the default, never to something unvalidated
    assert fs.normalize({"zzshu_card_margin_pct": "500"})["zzshu_card_margin_pct"] == "20"


async def test_admin_creates_a_zzshu_source_and_refuses_card_parameters(env, dup):  # noqa: F811
    admin, _ = await env.login("admin")
    r = await admin.post(
        "/api/v1/admin/suppliers",
        json={"code": "zzshu", "adapter": "zzshu", "label": "zzshu", "env": "sandbox"},
    )
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    base = {"product_id": str(dup.product.id), "supplier_id": sid, "cost_estimate": "25"}
    ok = await admin.post(
        "/api/v1/admin/procurement/sources",
        json={**base, "params": {"product": "gpt", "plan": "plus", "payment_country": "PH"}},
    )
    assert ok.status_code in (200, 201), ok.text
    for params in (
        {"product": "gpt", "plan": "plus", "payment_country": "JP"},
        {"product": "gpt", "plan": "plus_half", "payment_country": "PH"},
        {"product": "gpt", "plan": "plus", "payment_country": "PH", "card_id": 4242},
        {"product": "claude", "plan": "pro", "payment_country": "US"},
    ):
        r = await admin.post("/api/v1/admin/procurement/sources", json={**base, "params": params})
        assert r.status_code in (409, 422), (params, r.text)


# ------------------------------------------------------------------ the fallback
async def test_a_precharge_failure_fails_over_to_zzshu_with_a_dedicated_card(env, dup):  # noqa: F811
    vendor, zz = _vendor(), MockZzshu()
    _zovo_failing(vendor)
    await _both(env, dup, vendor, zz)
    buyer, order = await _order(dup)
    await _wait(env, dup, order, buyer)
    job = await _job(dup, order)
    assert job.state == "fulfilled" and vendor.direct_executions == 1  # ZovoCard was tried once
    assert zz.creates == 1 and len(zz.orders) == 1
    zovo_ref = next(iter(vendor.direct_orders))
    # the ZovoCard order stays in the audit and in the staff timeline
    (row,) = await _audit(env, "procurement.precharge_failover")
    assert row[1]["from_ref"] == str(zovo_ref) and row[0] == "warning"
    assert any(e.get("code") == "precharge_failover" for e in job.progress["events"])
    # one NEW card for the order, loaded with 25 x 1.2 = 30, then returned and deleted
    assert vendor.card_opens == 1
    card = vendor.next_card
    assert job.card_id == str(card) and job.card_last4 == f"{card:012d}"[-4:]
    assert vendor.cards[card]["status"] == "DELETED"
    assert vendor.card_refunds == [(card, 29.0)]
    opened, swept = await _card_rows(env)
    assert opened["card_id"] == swept["card_id"] == str(card)
    assert opened["last4"] == swept["last4"] == job.card_last4
    assert Decimal(opened["loaded"]) == 30 and swept["swept"] is True
    assert Decimal(swept["leftover"]) == 30
    assert any(e.get("code") == "card_swept" for e in job.progress["events"])
    cards = (await buyer.post(f"/api/v1/orders/{order['id']}/reveal")).json()["cards"]
    assert cards[0]["code"].startswith("gpt/plus----") and "DIRECT-" not in cards[0]["code"]
    # the zzshu order was sent with the card that was opened for it and the buyer's session
    sent = zz.seen_cards[0]
    assert sent["cardNumber"] == f"4111{card:012d}" and sent["token"]["sessionToken"]
    assert not await _audit(env, "procurement.zzshu_card_unswept")
    # the dedicated card is never a funding card of another order
    from app.core.db import tenant_session
    from app.procurement.funding import funding_card_clash

    async with tenant_session(dup.ctx.maker, env.tenant_id) as db:
        assert await funding_card_clash(db, env.tenant_id, job.supplier_id, card)
        assert not await funding_card_clash(db, env.tenant_id, job.supplier_id, 4242)


@pytest.mark.parametrize(
    ("end", "fee"),
    [("declined", "released"), ("failed_precharge", "held"), ("failed_precharge", "settled")],
)
async def test_nothing_that_may_have_charged_is_ever_moved_to_zzshu(env, dup, end, fee):  # noqa: F811
    vendor, zz = _vendor(), MockZzshu()
    _zovo_failing(vendor, end, fee)
    await _both(env, dup, vendor, zz)
    _b, order = await _order(dup)
    await _settle(dup, 6)
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.upstream_ref and job.maybe_sent
    assert zz.creates == 0 and vendor.card_opens == 0
    assert not await _audit(env, "procurement.precharge_failover")


async def test_a_precharge_failure_without_a_standby_source_goes_to_staff(env, dup):  # noqa: F811
    vendor = _vendor()
    _zovo_failing(vendor)
    await _funding(env, auto_recharge=True)
    await _zovo(dup, vendor, GPT)
    _b, order = await _order(dup)
    await _settle(dup, 6)
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "order_failed_precharge"
    assert vendor.direct_executions == 1


# ------------------------------------------------------------------ primary switch
async def test_staff_make_zzshu_primary_by_priority_and_back(env, dup):  # noqa: F811
    vendor, zz = _vendor(), MockZzshu()
    _zsid, _sid, zoid = await _both(env, dup, vendor, zz, zz_priority=5)
    buyer, order = await _order(dup)
    await _wait(env, dup, order, buyer)
    assert zz.creates == 1 and vendor.direct_executions == 0  # ZovoCard was never asked
    # back: the ZovoCard source first again
    await _sql(
        env,
        "UPDATE supplier_offers SET priority = 50 WHERE supplier_id IN "
        "(SELECT id FROM suppliers WHERE code = 'zzshu')",
    )
    buyer2, order2 = await _order(dup)
    await _wait(env, dup, order2, buyer2)
    assert zz.creates == 1 and vendor.direct_executions == 1


async def test_repeated_precharge_failures_start_a_cooldown_that_sends_new_jobs_to_zzshu(
    env,
    dup,  # noqa: F811
):
    vendor, zz = _vendor(), MockZzshu()
    _zovo_failing(vendor)
    await _both(env, dup, vendor, zz)
    for _ in range(3):  # three pre-charge failures within the hour, each rescued by zzshu
        buyer, order = await _order(dup)
        await _wait(env, dup, order, buyer)
    assert vendor.direct_executions == 3 and zz.creates == 3
    (cool,) = await _audit(env, "procurement.direct_cooldown")
    assert cool[0] == "critical" and cool[1]["failures"] == 3 and cool[1]["cooldown_s"] == 1800
    # the 4th order goes to zzshu FIRST: ZovoCard is not asked at all
    buyer, order = await _order(dup)
    await _wait(env, dup, order, buyer)
    assert vendor.direct_executions == 3 and zz.creates == 4
    # the cool-down is announced once, and it ends by itself (the key expires)
    assert len(await _audit(env, "procurement.direct_cooldown")) == 1
    keys = [k async for k in env.res.redis.scan_iter("rl:proc:cool:*")]
    assert len(keys) == 1 and 0 < await env.res.redis.ttl(keys[0]) <= 1800
    await env.res.redis.delete(*keys)
    _zovo_failing(vendor, "completed", "settled")
    buyer, order = await _order(dup)
    await _wait(env, dup, order, buyer)
    assert vendor.direct_executions == 4  # ZovoCard first again


async def test_a_cooling_zovocard_is_still_used_when_nothing_else_is_left(env, dup):  # noqa: F811
    vendor, zz = _vendor(), MockZzshu(points=0)
    await _both(env, dup, vendor, zz)
    for k in await env.res.redis.keys("rl:proc:cool:*"):
        await env.res.redis.delete(k)
    sid = await env.scalar("SELECT id FROM suppliers WHERE code = 'zovocard'")
    await env.res.redis.set(f"rl:proc:cool:{sid}", "1", ex=600)
    buyer, order = await _order(dup)
    await _wait(env, dup, order, buyer)
    assert vendor.direct_executions == 1 and zz.creates == 0  # zzshu has no point; demoted != off


# ------------------------------------------------------------------ the dedicated card
async def test_the_card_margin_setting_sizes_the_card(env, dup):  # noqa: F811
    vendor, zz = _vendor(), MockZzshu()
    await _both(env, dup, vendor, zz, zz_priority=5)
    await _funding(env, auto_recharge=True, zzshu_card_margin_pct="50")
    buyer, order = await _order(dup)
    await _wait(env, dup, order, buyer)
    opened, _swept = await _card_rows(env)
    assert Decimal(opened["loaded"]) == 38  # 25 x 1.5 = 37.5, whole dollars up


async def test_without_the_auto_recharge_switch_no_card_is_opened(env, dup):  # noqa: F811
    vendor, zz = _vendor(), MockZzshu()
    await _both(env, dup, vendor, zz, zz_priority=5)
    await _funding(env)  # master switch off (the default)
    buyer, order = await _order(dup)
    await _wait(env, dup, order, buyer)
    assert vendor.card_opens == 0 and zz.creates == 0  # zzshu is skipped at the pre-check
    job = await _job(dup, order)
    assert job.state == "fulfilled" and vendor.direct_executions == 1  # ZovoCard took it


async def test_a_zzshu_order_that_fails_ends_with_staff_and_the_card_is_swept(env, dup):  # noqa: F811
    vendor, zz = _vendor(), MockZzshu(final="failed")
    await _both(env, dup, vendor, zz, zz_priority=5)
    _b, order = await _order(dup)
    await _settle(dup, 8)
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "order_failed"
    assert vendor.direct_executions == 0 and zz.creates == 1  # no third source is tried
    assert vendor.cards[vendor.next_card]["status"] == "DELETED"
    assert any(e.get("code") == "failed" for e in job.progress["events"])


async def test_a_definite_refusal_sweeps_the_card_and_goes_to_staff(env, dup):  # noqa: F811
    vendor, zz = _vendor(), MockZzshu(create="refuse_region")
    await _both(env, dup, vendor, zz, zz_priority=5)
    _b, order = await _order(dup)
    await _settle(dup, 5)
    job = await _job(dup, order)
    # nothing was created, so nothing was charged; the card goes back and staff decide
    assert job.state == "needs_manual" and job.error_code == "region_not_offered"
    assert zz.creates == 1 and vendor.direct_executions == 0 and not job.upstream_ref
    assert vendor.cards[vendor.next_card]["status"] == "DELETED"
    assert not await _audit(env, "procurement.zzshu_card_unswept")


async def test_queue_full_retries_with_the_same_card(env, dup):  # noqa: F811
    vendor, zz = _vendor(), MockZzshu(create="queue_full")
    await _both(env, dup, vendor, zz, zz_priority=5)
    buyer, order = await _order(dup)
    await _settle(dup, 3)
    assert zz.creates >= 2 and not zz.orders and vendor.card_opens == 1
    zz.create = "ok"
    await _wait(env, dup, order, buyer)
    assert len(zz.orders) == 1 and vendor.card_opens == 1  # ONE card for the whole order
    assert (await _job(dup, order)).state == "fulfilled"


async def test_the_points_gate_fails_closed(env, dup):  # noqa: F811
    vendor, zz = _vendor(), MockZzshu(points=0)
    await _both(env, dup, vendor, zz, zz_priority=5)
    buyer, order = await _order(dup)
    await _wait(env, dup, order, buyer)  # zzshu is skipped, ZovoCard delivers
    assert zz.creates == 0 and vendor.card_opens == 0 and vendor.direct_executions == 1


# ------------------------------------------------------------------ ambiguity
async def test_a_lost_create_answer_goes_to_staff_and_is_never_resent(env, dup):  # noqa: F811
    vendor, zz = _vendor(), MockZzshu(create="lost")
    await _both(env, dup, vendor, zz, zz_priority=5)
    _b, order = await _order(dup)
    await _settle(dup, 8)
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "create_unconfirmed"
    assert job.maybe_sent and zz.creates == 1  # exactly one create, ever
    assert vendor.direct_executions == 0  # and no other source after a possible charge
    # the card may hold money zzshu took: it is NOT swept, staff are told which card it is
    card = vendor.next_card
    assert vendor.cards[card]["status"] == "ACTIVE" and job.card_id == str(card)
    (row,) = await _audit(env, "procurement.zzshu_card_unswept")
    assert row[1]["card_id"] == str(card)
    await _settle(dup, 4)
    assert zz.creates == 1


# ------------------------------------------------------------------ secrets hygiene
async def test_card_number_cvv_and_session_never_leave_the_adapter(env, dup, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    marker = new_marker()
    vendor, zz = _vendor(), MockZzshu()
    _zovo_failing(vendor)
    await _both(env, dup, vendor, zz)
    buyer, order = await _order(dup, marker)
    await _wait(env, dup, order, buyer)
    card = vendor.next_card
    pan = f"4111{card:012d}"
    sent = zz.seen_cards[0]
    assert sent["cardNumber"] == pan and sent["cvv"] == "7319"
    secrets = [pan, f"{marker}-session-token", sent["token"]["accessToken"]]
    logs = caplog.text
    for s in secrets:
        assert s not in logs, s[:6]
    assert not re.search(r"cvv\W{0,6}7319", logs, re.I)
    tables = ("audit_log", "procurement_jobs", "outbox", "procurement_fundings", "card_stock")
    for table in tables:
        dump = await env.scalar(
            f"SELECT coalesce(string_agg(row_to_json(t)::text, ' '), '') FROM {table} t"  # noqa: S608
        )
        for s in secrets:
            assert s not in dump, (table, s[:6])
        assert '"7319"' not in dump, table
    job = await _job(dup, order)
    assert pan not in json.dumps(job.progress) and "7319" not in json.dumps(job.progress)
    # the buyer's delivery text carries no vendor key either
    cards = (await buyer.post(f"/api/v1/orders/{order['id']}/reveal")).json()["cards"]
    assert pan not in cards[0]["code"] and "7319" not in cards[0]["code"]
    rows = await _rows(env, "SELECT count(*) FROM procurement_credentials")
    assert rows[0][0] == 0  # the sealed session is gone at the terminal state
