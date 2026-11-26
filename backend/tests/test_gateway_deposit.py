"""Per-gateway deposit guard: helpers, routing skip, admin settings, large-payment alert."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core import notify_tg
from app.models import AuditLog, PaymentGateway
from app.modules.payments import deposit
from app.payments import epay
from app.payments.zpay import QueryResult, ZPayClient
from tests.helpers import buy
from tests.test_gateway_routing_v10 import GW, NOTIFY, PAY, _attempts, _setup
from tests.test_telegram_v52 import Telegram, _enable


# ------------------------------------------------------------------------------ helpers (unit)
def test_required_is_the_larger_of_floor_and_percentage_rounded_up():
    assert deposit.required_minor(66100, 68, 5000) == 44948  # ¥661.00 x 68 % = ¥449.48
    assert deposit.required_minor(1000, 68, 5000) == 5000  # the floor wins
    assert deposit.required_minor(0, 68, 5000) == 5000
    assert deposit.required_minor(101, 68, 0) == 69  # 68.68 rounds UP


def test_cap_is_the_largest_payment_the_paid_deposit_covers():
    assert deposit.cap_minor(44948, 68) == 66100
    assert deposit.cap_minor(10000, 68) == 14705
    # the cap and the requirement agree: at the cap the deposit is enough, one fen more is not
    cap = deposit.cap_minor(10000, 68)
    assert deposit.required_minor(cap, 68, 0) <= 10000 < deposit.required_minor(cap + 1, 68, 0)


def _row(name, dep=None, pct=None):
    return SimpleNamespace(label=name, deposit_minor=dep, deposit_pct=pct)


def test_within_cap_skips_only_guarded_gateways_over_their_cap_and_keeps_order():
    a, b, c = _row("a", 10000, 68), _row("b"), _row("c", 100000, 68)
    assert [r.label for r in deposit.within_cap([a, b, c], 30000)] == ["b", "c"]
    assert [r.label for r in deposit.within_cap([a, b, c], 14705)] == ["a", "b", "c"]
    # a half-configured gateway (no percentage) is unguarded
    assert [r.label for r in deposit.within_cap([_row("x", 1, None)], 10**8)] == ["x"]


def test_when_every_gateway_would_be_skipped_the_old_behaviour_stays():
    rows = [_row("a", 10000, 68), _row("c", 20000, 68)]
    assert [r.label for r in deposit.within_cap(rows, 10**8)] == ["a", "c"]


# ---------------------------------------------------------------------------- routing (flow)
async def _set_deposit(admin, ref, **body):
    r = await admin.patch(f"{GW}/{ref}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _buy(env, buyer, price):
    return await buy(buyer, await env.product(mode="manual", price=price, floor=100))


async def _pay(buyer, order):
    r = await buyer.post(PAY.format(order["id"]), json={"channel": "zpay_alipay"})
    assert r.status_code == 200, r.text


async def test_gateway_is_skipped_for_a_payment_above_its_deposit_cap(env, monkeypatch):
    admin, buyer, a, _b, calls = await _setup(env, monkeypatch, {"gwa": None, "gwb": None})
    await _set_deposit(admin, a[0], deposit_cny="100", deposit_pct=68, deposit_floor_cny="50")
    await _pay(buyer, await _buy(env, buyer, 30000))  # ¥300 > cap ¥147.05
    assert calls == ["gwb"]
    await _pay(buyer, await _buy(env, buyer, 5000))  # ¥50 fits
    assert calls == ["gwb", "gwa"]
    # an unconfigured gateway never changes behaviour
    await _set_deposit(admin, a[0], deposit_cny=None, deposit_pct=None, deposit_floor_cny=None)
    await _pay(buyer, await _buy(env, buyer, 30000))
    assert calls == ["gwb", "gwa", "gwa"]


async def test_if_every_gateway_is_over_its_cap_the_first_is_still_tried(env, monkeypatch):
    admin, buyer, a, b, calls = await _setup(env, monkeypatch, {"gwa": None, "gwb": None})
    for ref in (a[0], b[0]):
        await _set_deposit(admin, ref, deposit_cny="10", deposit_pct=68)
    await _pay(buyer, await _buy(env, buyer, 30000))
    assert calls == ["gwa"]
    (att,) = await _attempts(env)
    assert att.status == "ready"


# ----------------------------------------------------------------------------------- admin
async def test_admin_lists_and_updates_the_deposit_settings(env, monkeypatch):
    admin, _buyer, a, _b, _calls = await _setup(env, monkeypatch, {"gwa": None, "gwb": None})
    row = next(i for i in (await admin.get(GW)).json()["items"] if i["id"] == a[0])
    assert row["deposit_minor"] is None and row["cap_minor"] is None and row["max30_minor"] == 0
    out = await _set_deposit(
        admin, a[0], deposit_cny="449.48", deposit_pct=68, deposit_floor_cny="50"
    )
    assert out["deposit_minor"] == 44948 and out["deposit_floor_minor"] == 5000
    row = next(i for i in (await admin.get(GW)).json()["items"] if i["id"] == a[0])
    assert (row["deposit_minor"], row["deposit_pct"], row["deposit_floor_minor"]) == (
        44948,
        68,
        5000,
    )
    assert row["required_minor"] == 5000 and row["cap_minor"] == 66100
    # a partial update leaves the other settings alone
    await _set_deposit(admin, a[0], deposit_pct=70)
    row = next(i for i in (await admin.get(GW)).json()["items"] if i["id"] == a[0])
    assert (row["deposit_minor"], row["deposit_pct"]) == (44948, 70)
    async with env.api_session() as db:
        audits = list(
            await db.scalars(
                select(AuditLog)
                .where(AuditLog.action == "payment.gateway_updated")
                .order_by(AuditLog.created_at)
            )
        )
    audits = [a for a in audits if "deposit_minor" in a.after or "deposit_pct" in a.after]
    first = audits[0]
    assert first.before["deposit_minor"] is None and first.after["deposit_minor"] == 44948
    assert audits[-1].before["deposit_pct"] == 68 and audits[-1].after["deposit_pct"] == 70


@pytest.mark.parametrize(
    "body",
    [
        {"deposit_pct": 0},
        {"deposit_pct": 101},
        {"deposit_cny": "-1"},
        {"deposit_cny": "10000000.01"},
        {"deposit_cny": "1.234"},
        {"deposit_floor_cny": "-0.01"},
        {"deposit_cny": "abc"},
    ],
)
async def test_deposit_settings_are_validated(env, monkeypatch, body):
    admin, _buyer, a, _b, _calls = await _setup(env, monkeypatch, {"gwa": None, "gwb": None})
    r = await admin.patch(f"{GW}/{a[0]}", json=body)
    assert r.status_code == 422, r.text


async def test_deposit_settings_are_admin_only_and_tenant_scoped(env, monkeypatch):
    admin, buyer, a, _b, _calls = await _setup(env, monkeypatch, {"gwa": None, "gwb": None})
    assert (await buyer.patch(f"{GW}/{a[0]}", json={"deposit_pct": 50})).status_code in (401, 403)
    other_admin = await env.user("admin", tenant_id=env.other_tenant_id)
    foreign = uuid.uuid4()
    async with env.owner_session(env.other_tenant_id) as db:
        db.add(
            PaymentGateway(
                id=foreign,
                tenant_id=env.other_tenant_id,
                label="foreign",
                gateway="https://f.example",
                merchant_id="f",
                key_ciphertext=b"x",
                created_by=other_admin.id,
            )
        )
        await db.commit()
    r = await admin.patch(f"{GW}/{foreign}", json={"deposit_pct": 50})
    assert r.status_code == 404
    assert str(foreign) not in {i["id"] for i in (await admin.get(GW)).json()["items"]}


# -------------------------------------------------------------------------- large payment
def large_texts(tg):  # the paid-order alert goes out too: only the large ones
    return [t for t in tg.texts if "Large payment" in t]


@pytest.fixture
async def tg(env, monkeypatch):
    t = Telegram(env.res.http.post)
    monkeypatch.setattr(env.res.http, "post", t.post)
    yield t
    await notify_tg.drain()


async def _settle(env, monkeypatch, buyer, order, key, trade, seen):
    await _pay(buyer, order)
    att = (await _attempts(env))[-1]
    seen.update(amount=Decimal(order["amount"]), trade=trade)
    params = {
        "pid": "gwa",
        "out_trade_no": att.gateway_ref,
        "trade_no": trade,
        "money": order["amount"],
        "trade_status": "TRADE_SUCCESS",
        "type": "alipay",
        "sign_type": "MD5",
    }
    params["sign"] = epay.sign(params, key)
    assert (await env.client(csrf=False).get(NOTIFY, params=params)).text == "success"
    await notify_tg.drain()


async def test_a_new_largest_payment_alerts_with_the_previous_maximum(env, monkeypatch, tg):
    admin, buyer, a, _b, _calls = await _setup(env, monkeypatch, {"gwa": None, "gwb": None})
    await _enable(env, admin)
    await _set_deposit(admin, a[0], deposit_cny="100", deposit_pct=68, deposit_floor_cny="50")
    seen: dict = {}

    async def query(self, no, **_kw):
        return QueryResult(True, True, seen["amount"], seen["trade"], no)

    monkeypatch.setattr(ZPayClient, "query_order", query)
    # ¥100 is within the guard, and the very first payment is a new maximum
    first = await _buy(env, buyer, 10000)
    await _settle(env, monkeypatch, buyer, first, a[1], "T1", seen)
    assert len(large_texts(tg)) == 1
    assert "Large payment ¥100.00 via gw gwa (previous 30-day max ¥0.00)" in large_texts(tg)[0]
    assert "Deposit due now ¥68.00; paid ¥100.00" in large_texts(tg)[0]
    # a smaller payment is silent
    await _settle(env, monkeypatch, buyer, await _buy(env, buyer, 5000), a[1], "T2", seen)
    assert len(large_texts(tg)) == 1
    # a bigger one alerts with the previous maximum; the percentage is lowered so the guard still
    # routes the ¥300 order to gwa (cap = ¥100 / 30 % = ¥333.33)
    await _set_deposit(admin, a[0], deposit_cny="100", deposit_pct=30)
    big = await _buy(env, buyer, 30000)
    await _settle(env, monkeypatch, buyer, big, a[1], "T3", seen)
    assert len(large_texts(tg)) == 2
    text = large_texts(tg)[1]
    assert "Large payment ¥300.00 via gw gwa (previous 30-day max ¥100.00)" in text
    assert "Deposit due now ¥90.00; paid ¥100.00" in text  # 30 % of ¥300: still covered


async def test_short_deposit_says_how_much_to_pay_and_by_when(env, monkeypatch, tg):
    admin, buyer, a, _b, _calls = await _setup(env, monkeypatch, {"gwa": None, "gwb": None})
    await _enable(env, admin)
    await _set_deposit(admin, a[0], deposit_cny="50", deposit_pct=68, deposit_floor_cny="50")
    # the guard would route a ¥300 order away from gwa: configure the deposit only after the
    # order is placed on gwa (as when the gateway's rule changes under a standing QR code)
    seen: dict = {}

    async def query(self, no, **_kw):
        return QueryResult(True, True, seen["amount"], seen["trade"], no)

    monkeypatch.setattr(ZPayClient, "query_order", query)
    order = await _buy(env, buyer, 30000)
    await _set_deposit(admin, a[0], deposit_pct=None, deposit_cny=None, deposit_floor_cny=None)
    await _pay(buyer, order)
    await _set_deposit(admin, a[0], deposit_cny="50", deposit_pct=68, deposit_floor_cny="50")
    att = (await _attempts(env))[-1]
    seen.update(amount=Decimal(order["amount"]), trade="T9")
    params = {
        "pid": "gwa",
        "out_trade_no": att.gateway_ref,
        "trade_no": "T9",
        "money": order["amount"],
        "trade_status": "TRADE_SUCCESS",
        "type": "alipay",
        "sign_type": "MD5",
    }
    params["sign"] = epay.sign(params, a[1])
    assert (await env.client(csrf=False).get(NOTIFY, params=params)).text == "success"
    await notify_tg.drain()
    assert len(large_texts(tg)) == 1
    text = large_texts(tg)[0]
    assert "Large payment ¥300.00" in text
    assert "Deposit due now ¥204.00; paid ¥50.00; short ¥154.00" in text
    assert "pay within 48 h or new orders are refused" in text


async def test_large_payment_toggle_off_sends_nothing(env, monkeypatch, tg):
    admin, buyer, a, _b, _calls = await _setup(env, monkeypatch, {"gwa": None, "gwb": None})
    await _enable(env, admin, event_large_payment=False)
    seen: dict = {}

    async def query(self, no, **_kw):
        return QueryResult(True, True, seen["amount"], seen["trade"], no)

    monkeypatch.setattr(ZPayClient, "query_order", query)
    await _settle(env, monkeypatch, buyer, await _buy(env, buyer, 10000), a[1], "T1", seen)
    assert large_texts(tg) == []
