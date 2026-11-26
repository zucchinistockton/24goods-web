"""Staff Telegram alerts when an EPay gateway refuses / is unreachable / none is left."""

from __future__ import annotations

import pytest

from app.core import notify_tg
from app.modules.payments.alerts import clean_text
from app.payments.zpay import ZPayRefused, ZPayUnreachable
from tests.test_gateway_routing_v10 import PAY, _order, _setup
from tests.test_telegram_v52 import TOKEN, Telegram, _enable

REFUSAL = "该商户保证金未缴清，暂不可收款，请联系平台补缴"


@pytest.fixture
async def tg(env, monkeypatch):
    t = Telegram(env.res.http.post)
    monkeypatch.setattr(env.res.http, "post", t.post)
    yield t
    await notify_tg.drain()


async def _pay(env, buyer, order) -> int:
    r = await buyer.post(PAY.format(order["id"]), json={"channel": "zpay_alipay"})
    await notify_tg.drain()
    return r.status_code


def test_clean_text_strips_controls_and_caps():
    assert clean_text("a\x00b\u202ec\nd  e", 120) == "a b c d e"
    assert len(clean_text("x" * 500, 120)) == 120
    assert clean_text(None, 120) == ""


async def test_refusal_alerts_once_and_a_second_refusal_is_silent(env, tg, monkeypatch):
    admin, buyer, _a, _b, calls = await _setup(
        env, monkeypatch, {"gwa": ZPayRefused(REFUSAL), "gwb": None}
    )
    await _enable(env, admin)
    assert await _pay(env, buyer, await _order(env, buyer)) == 200  # snpay-like fallback pays
    assert calls == ["gwa", "gwb"]
    assert len(tg.calls) == 1
    text = tg.texts[0]
    assert "Payment gateway" in text and "gw gwa" in text and REFUSAL in text
    assert "fall through to the next gateway" in text and "/admin/settings/payment" in text
    assert TOKEN not in text
    # a second order within the 6 h window: refused again, still only one message
    assert await _pay(env, buyer, await _order(env, buyer)) == 200
    assert calls == ["gwa", "gwb", "gwa", "gwb"]
    assert len(tg.calls) == 1


async def test_unreachable_alert_is_separate_and_throttled_per_gateway(env, tg, monkeypatch):
    admin, buyer, _a, _b, _calls = await _setup(
        env, monkeypatch, {"gwa": ZPayUnreachable("down"), "gwb": None}
    )
    await _enable(env, admin)
    for _ in range(2):
        assert await _pay(env, buyer, await _order(env, buyer)) == 200
    assert len(tg.calls) == 1
    assert "is unreachable" in tg.texts[0] and "gw gwa" in tg.texts[0]


async def test_refusal_message_is_sanitised_and_escaped(env, tg, monkeypatch):
    nasty = "<b>x</b>\x07" + "y" * 400
    admin, buyer, _a, _b, _calls = await _setup(
        env, monkeypatch, {"gwa": ZPayRefused(nasty), "gwb": None}
    )
    await _enable(env, admin)
    await _pay(env, buyer, await _order(env, buyer))
    text = tg.texts[0]
    assert "<b>x</b>" not in text and "&lt;b&gt;x&lt;/b&gt;" in text
    assert "\x07" not in text and "y" * 121 not in text


async def test_all_gateways_failing_sends_an_urgent_alert_once(env, tg, monkeypatch):
    admin, buyer, _a, _b, _calls = await _setup(
        env, monkeypatch, {"gwa": ZPayRefused(REFUSAL), "gwb": ZPayUnreachable("down")}
    )
    await _enable(env, admin)
    assert await _pay(env, buyer, await _order(env, buyer)) != 200
    urgent = [t for t in tg.texts if "URGENT: no payment gateway available" in t]
    assert len(urgent) == 1
    assert "buyers cannot pay" in urgent[0]
    assert len(tg.calls) == 3  # refused, unreachable, none left
    assert await _pay(env, buyer, await _order(env, buyer)) != 200
    assert len([t for t in tg.texts if "buyers cannot pay" in t]) == 1  # 15 min throttle


async def test_toggle_off_sends_nothing(env, tg, monkeypatch):
    admin, buyer, _a, _b, _calls = await _setup(
        env, monkeypatch, {"gwa": ZPayRefused(REFUSAL), "gwb": None}
    )
    await _enable(env, admin, event_gateway_down=False)
    assert await _pay(env, buyer, await _order(env, buyer)) == 200
    assert tg.calls == []
