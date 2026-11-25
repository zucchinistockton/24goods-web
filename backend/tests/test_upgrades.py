"""Upgrades of a running ChatGPT plan (owner 2026-11-22) and the free-account purchase rule."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from app.core import costmodel as cm
from app.devtools.mock_zovocard import OK_KEY, MockZovo, create_mock_zovocard
from app.suppliers.zovocard import TokenBucket, ZovoCardSupplier, ZovoClient, ZovoConfig
from app.suppliers.zovocard import direct as dr
from app.suppliers.zovocard.supplier import _account_problem
from tests.test_procurement_direct import gpt_session

D = Decimal


def _pf(plan: str | None, *, active: bool = False, wait_days: int | None = None) -> dr.Preflight:
    return dr.Preflight(
        token="pf_" + "a" * 20,
        account_mask="m***@gmail.com",
        current_plan=plan,
        expires_at=None,
        quote_error=False,
        can_purchase_at=(
            datetime.now(timezone.utc) + timedelta(days=wait_days) if wait_days else None
        ),
        has_active=active,
    )


def test_only_a_higher_pro_plan_over_a_paid_plan_is_an_upgrade():
    assert dr.upgradable(_pf("plus"), "pro_5x")
    assert dr.upgradable(_pf("go"), "pro_50x")
    assert dr.upgradable(_pf("pro_5x"), "pro_20x")
    assert not dr.upgradable(_pf("plus"), "plus")  # same plan: not an upgrade
    assert not dr.upgradable(_pf("pro_20x"), "pro_5x")  # lower
    assert not dr.upgradable(_pf("free"), "pro_5x")  # a free account simply buys
    assert not dr.upgradable(_pf(None), "pro_5x")
    assert not dr.upgradable(_pf("go"), "plus")  # Plus is not an upgrade target


def test_a_future_purchase_date_blocks_only_an_account_that_holds_a_plan():
    """Owner 2026-11-22: a FREE account was refused on the date alone, then bought Plus fine."""
    assert _account_problem(_pf("free", wait_days=20)) is None
    assert _account_problem(_pf(None, wait_days=20)) is None
    assert _account_problem(_pf("plus", wait_days=20)) == "purchase_not_yet"
    assert _account_problem(_pf("free", active=True, wait_days=20)) == "purchase_not_yet"


def test_the_upgrade_quote_is_parsed_strictly():
    data = {
        "quotes": [
            {"plan": "pro_20x", "available": False, "error_code": "upgrade_quote_unavailable"},
            {"plan": "pro_5x", "current_plan": "plus", "available": True, "currency": "php",
             "amount_minor": 566599, "minor_unit_exponent": 2, "funding_usd_minor": 10700,
             "preflight_token": "up_" + "b" * 20},
        ]
    }  # fmt: skip
    q = dr.parse_upgrade_quote(data, "pro_5x")
    assert q is not None and q.currency == "PHP" and q.amount_minor == 566599
    assert q.usd == D("97.27")  # 107.00 / 1.10
    assert q.view()["amount"] == "5665.99" and "up_" not in json.dumps(q.view())
    assert dr.parse_upgrade_quote(data, "pro_20x") is None
    bad = {"quotes": [{**data["quotes"][1], "amount_minor": True}]}
    assert dr.parse_upgrade_quote(bad, "pro_5x") is None


async def test_the_account_check_passes_an_upgrade_with_the_vendor_quote():
    vendor = MockZovo()
    vendor.upgrade_from = "plus"
    vendor.direct_current_plan = "plus"  # what the mock's preflight reports for the account
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_mock_zovocard(vendor))
    ) as http:
        s = ZovoCardSupplier(
            "zovocard",
            ZovoClient(
                ZovoConfig(base_url="http://m/openapi/v1", app_secret=OK_KEY),
                http=http,
                bucket=TokenBucket(6000, 100),
            ),
        )
        session = json.loads(gpt_session("UPG1", email="buyer+active@example.com"))
        session["account"]["planType"] = "plus"
        res = await s.account_check("gpt", "pro_5x", "PH", "PHP", json.dumps(session))
        assert res.status == "passed" and res.upgrade is not None
        assert res.upgrade["plan"] == "pro_5x" and res.upgrade["currency"] == "PHP"
        assert D(res.upgrade["usd"]) > 0
        vendor.upgrade_off = True  # the vendor offers no upgrade: a clear refusal, no full price
        res = await s.account_check("gpt", "pro_5x", "PH", "PHP", json.dumps(session))
        assert res.status == "blocked" and res.reason == "upgrade_unavailable"
        # the same plan while it runs: still "plan active"
        res = await s.account_check("gpt", "plus", "PH", "PHP", json.dumps(session))
        assert res.status == "blocked" and res.reason == "plan_active"


def test_the_upgrade_price_is_the_difference_plus_markup_with_the_shop_ending():
    """The order's price: USD difference x rate x (1 + markup), rounded like recommendations."""
    st = cm.normalize({"target_markup": "0.079", "price_ending": 9})
    usd = D("97.27") * (1 + cm.target_markup(st, "p"))
    unit = cm.recommend_minor(usd, D("6.680778"), st["round_step_minor"], st["price_ending"])
    assert unit == 70900  # 97.27 x 1.079 x 6.68 = 701.2 -> 702 -> 709
    assert min(unit, 72900) == 70900  # never above the list price (Pro 5x US 729)


def test_upgrade_switches_are_off_by_default_and_validated():
    """Owner 2026-11-25: upgrades sell at the full list price and no renewal is cancelled unless
    staff switch it on (funding settings, 2FA write)."""
    from app.core import funding as fs

    d = fs.normalize(None)
    assert d["upgrade_pay_difference"] is False and d["cancel_renewal_before_upgrade"] is False
    on = fs.validate({"upgrade_pay_difference": True, "cancel_renewal_before_upgrade": True})
    assert on["upgrade_pay_difference"] is True and on["cancel_renewal_before_upgrade"] is True
    import pytest

    with pytest.raises(ValueError):
        fs.validate({"upgrade_pay_difference": "yes"})


async def test_an_upgrade_order_pays_the_list_price_unless_the_switch_is_on(env):
    from sqlalchemy import text

    from app.modules.orders.service import pays_difference

    async with env.owner_session() as db:
        assert await pays_difference(db, env.tenant_id) is False
        await db.execute(
            text(
                "UPDATE tenants SET funding_settings = "
                "'{\"upgrade_pay_difference\": true}'::jsonb WHERE id = :t"
            ),
            {"t": env.tenant_id},
        )
        await db.commit()
        assert await pays_difference(db, env.tenant_id) is True


async def test_cancel_renewal_is_called_with_the_session_and_its_outcome_returned():
    from app.suppliers.base import JobSpec, OfferSpec

    vendor = MockZovo()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_mock_zovocard(vendor))
    ) as http:
        s = ZovoCardSupplier(
            "zovocard",
            ZovoClient(
                ZovoConfig(base_url="http://m/openapi/v1", app_secret=OK_KEY),
                http=http,
                bucket=TokenBucket(6000, 100),
            ),
        )
        job = JobSpec(
            job_id="a" * 32,
            step="submit",
            idempotency_key="pj:" + "a" * 32,
            offer=OfferSpec(kind="direct", params={"product": "gpt", "plan": "pro_5x"}),
            cancel_renewal=True,
        )
        session = gpt_session("CR1")
        assert await s._cancel_renewal(session, job, "m***@gmail.com") == "success"
        assert vendor.renewal_cancels == 1
        vendor.renewal_status = "pending"
        assert await s._cancel_renewal(session, job, None) == "pending"
        vendor.forced["/gpt-direct/cancel-renewal"].append("500")
        assert (await s._cancel_renewal(session, job, None)).startswith("error:")
