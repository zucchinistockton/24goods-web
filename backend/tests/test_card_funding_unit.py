"""ADR-048 pure parts: what a card must hold, the card order, top-up amounts, settings."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core import funding as fs
from app.suppliers.base import FundingPolicy
from app.suppliers.zovocard import direct as dr
from app.suppliers.zovocard import funding as fd

D = Decimal
PH_GO = {"currency": "PHP", "balanceBufferBps": 500, "minBalanceBufferUsdMinor": 200}


def _card(cid: int, avail: str, *, bin6: str = "537800", minr: str = "10", **kw) -> fd.FundCard:
    return fd.FundCard(
        id=cid,
        last4=f"{cid % 10000:04d}",
        status=kw.get("status", "ACTIVE"),
        available=D(avail),
        min_recharge=D(minr),
        product_code=kw.get("code", "P"),
        bin6=bin6,
        restricted=tuple(kw.get("restricted", ())),
    )


def test_need_of_a_peso_plan_adds_fx_margin_and_the_vendor_buffer():
    # Go PH: USD 4.71 -> 4.71 x 1.03 = 4.8513 + max(5 %, USD 2) = 6.8513 -> 6.86 (cents, up)
    assert fd.card_need(D("4.71"), PH_GO, usd_price=False) == D("6.86")
    # a large plan: the 5 % buffer exceeds the USD 2 floor
    assert fd.card_need(D("100"), PH_GO, usd_price=False) == D("108.15")


def test_need_without_a_price_is_unknown():
    assert fd.card_need(None, PH_GO, usd_price=False) is None
    assert fd.card_need(D(0), PH_GO, usd_price=False) is None


def test_top_up_is_whole_dollars_and_at_least_the_card_minimum():
    assert fd.topup_amount(D("6.56"), D(10)) == D(10)
    assert fd.topup_amount(D("17.01"), D(10)) == D(18)
    assert fd.topup_amount(D("0.2"), D(0)) == D(1)


def test_only_exact_merchant_names_block_a_product():
    assert not fd.blocked_for("gpt", ["GOOGLE CHATGPT", "GOOGLE", "CLAUDE"])
    assert fd.blocked_for("gpt", ["OPENAI"])
    assert fd.blocked_for("claude", ["CLAUDE"])


def test_order_prefers_no_money_move_then_last_success_then_bins_then_low_minimum():
    pol = FundingPolicy(
        auto_recharge=True, allowance=D(100), bin_priority=("428852", "537882"), preferred=("7",)
    )
    cards = [
        _card(1, "0.3", bin6="537882", minr="10"),
        _card(2, "0.3", bin6="428852", minr="30"),
        _card(3, "0.3", bin6="999999", minr="10"),
        _card(7, "0.3", bin6="999999", minr="20"),
        _card(9, "50"),  # holds the money: always first
    ]
    order = [p.card.id for p in fd.plan_cards(cards, D("6.86"), "gpt", pol, D(100))]
    assert order == [9, 7, 2, 1, 3]
    tops = {p.card.id: p.topup for p in fd.plan_cards(cards, D("6.86"), "gpt", pol, D(100))}
    assert tops[9] == 0 and tops[2] == D(30) and tops[1] == D(10)


def test_a_long_bin_priority_entry_matches_its_first_six_digits():
    pol = FundingPolicy(auto_recharge=True, allowance=D(100), bin_priority=("537872713",))
    cards = [_card(1, "0", bin6="111111"), _card(2, "0", bin6="537872")]
    assert [p.card.id for p in fd.plan_cards(cards, D(5), "gpt", pol, D(100))] == [2, 1]


def test_top_ups_need_the_switch_the_allowance_and_the_wallet():
    cards = [_card(1, "0.3")]
    assert fd.plan_cards(cards, D("6.86"), "gpt", None, D(100)) == []  # off by default
    pol = FundingPolicy(auto_recharge=True, allowance=D(9))
    assert fd.plan_cards(cards, D("6.86"), "gpt", pol, D(100)) == []  # USD 10 > allowance 9
    pol = FundingPolicy(auto_recharge=True, allowance=D(50))
    assert fd.plan_cards(cards, D("6.86"), "gpt", pol, D("10.2")) == []  # 10 x 1.05 > 10.2
    assert fd.plan_cards(cards, D("6.86"), "gpt", pol, D("10.2"), {"P": D(0)})  # fee 0: fits
    assert fd.plan_cards(cards, D("6.86"), "gpt", pol, None) == []  # wallet unknown


def test_frozen_and_blocked_cards_never_pay():
    cards = [_card(1, "50", status="FROZEN"), _card(2, "50", restricted=["OPENAI"])]
    assert fd.plan_cards(cards, D(5), "gpt", None, None) == []
    assert fd.shortfall_reason(cards, "gpt") == "no_funding_card"
    assert fd.shortfall_reason([_card(3, "0")], "gpt") == "funds_insufficient"


def test_card_rows_keep_no_card_number():
    data = {
        "total": 2,
        "list": [
            {
                "id": 921097,
                "card_number": "4865550287698575",
                "cvv": "123",
                "available_amount": 0.3,
                "status": "ACTIVE",
                "product_code": "11107407P",
                "min_recharge": 10,
                "restricted_merchants": ["GOOGLE", "CLAUDE"],
            },
            {"id": 5, "card_number": "x", "available_amount": 1, "status": "DELETED"},
        ],
    }
    (c,) = fd.parse_fund_cards(data)
    assert (c.id, c.last4, c.bin6, c.available, c.min_recharge) == (
        921097,
        "8575",
        "486555",
        D("0.3"),
        D(10),
    )
    assert "4865550287698575" not in repr(c) and "mask" not in repr(c)
    assert c.mask and c.mask.startswith("486555") and c.mask.endswith("8575")


def test_picked_top_up_param_is_strict():
    ok = {dr.PICKED_TOPUP_KEY: 1000, dr.PICKED_TOPUP_IDEM: "pj:ab:fund1"}
    assert dr.picked_topup(ok) == (D(10), "pj:ab:fund1")
    assert dr.picked_topup({**ok, dr.PICKED_TOPUP_KEY: True}) is None
    assert dr.picked_topup({**ok, dr.PICKED_TOPUP_KEY: 0}) is None
    assert dr.picked_topup({**ok, dr.PICKED_TOPUP_IDEM: "bad key!"}) is None


def test_settings_default_off_and_validate():
    st = fs.normalize(None)
    assert st["auto_recharge"] is False and st["bin_priority"] == list(fs.DEFAULT_BINS)
    # ADR-049: the card switches default ON but act only under auto_recharge (the master switch)
    assert st["auto_open_cards"] is True and st["sweep_leftover"] is True
    assert st["wallet_alert_usd"] == "60"
    new = fs.validate({"auto_recharge": True, "daily_cap_usd": "50", "bin_priority": ["537872"]})
    assert new["auto_recharge"] is True and new["daily_cap_usd"] == "50.00"
    for bad in (
        {"auto_recharge": "yes"},
        {"daily_cap_usd": "-1"},
        {"per_topup_max_usd": "99999"},
        {"bin_priority": ["53787"]},
        {"auto_open_cards": 1},
        {"wallet_alert_usd": "-5"},
        {"unknown": 1},
    ):
        with pytest.raises(ValueError):
            fs.validate(bad)
    # a corrupted stored value can never switch spending on
    assert fs.normalize({"auto_recharge": "true", "daily_cap_usd": "x"})["auto_recharge"] is False


# ------------------------------------------------------------------ ADR-049: open / sweep
def _prod(code: str, bin_: str, fee: str = "1.5", low: str = "10", **kw):
    from types import SimpleNamespace

    return SimpleNamespace(
        product_code=code,
        bin=bin_,
        open_fee=D(fee),
        min_amount=D(low),
        max_amount=kw.get("high"),
        recharge_fee=kw.get("rate", D(0)),
        enabled=kw.get("enabled", True),
        restricted=tuple(kw.get("restricted", ())),
    )


OPEN = FundingPolicy(
    auto_recharge=True, allowance=D(200), bin_priority=fs.DEFAULT_BINS, auto_open=True
)


def test_a_new_card_follows_the_bin_priority_and_loads_the_order_amount():
    products = [
        _prod("XL474613", "474613041"),
        _prod("XL537882", "537882130"),
        _prod("P4466BD", "446619", low="20"),
        _prod("OPENAI_BLOCKED", "532604480", restricted=["OPENAI"]),  # first BIN, but blocked
    ]
    plan = fd.pick_open_product(products, D("17.92"), "gpt", OPEN, D(60))
    assert plan is not None and plan.product_code == "XL537882"
    assert plan.init == D(18) and plan.fee == D("1.5") and plan.total == D("19.5")
    # a small plan still loads at least the product minimum
    go = fd.pick_open_product(products, D("6.86"), "gpt", OPEN, D(60))
    assert go is not None and go.init == D(10)


def test_no_new_card_without_the_switch_the_wallet_or_the_cap():
    products = [_prod("XL537872", "537872713")]
    assert fd.pick_open_product(products, D(18), "gpt", None, D(60)) is None
    off = FundingPolicy(auto_recharge=True, allowance=D(200), auto_open=False)
    assert fd.pick_open_product(products, D(18), "gpt", off, D(60)) is None
    assert fd.pick_open_product(products, D(18), "gpt", OPEN, D("19.49")) is None  # wallet
    assert fd.pick_open_product(products, D(18), "gpt", OPEN, None) is None  # unknown wallet
    tight = FundingPolicy(auto_recharge=True, allowance=D(19), auto_open=True)
    assert fd.pick_open_product(products, D(18), "gpt", tight, D(60)) is None  # daily cap
    off_sale = [_prod("XL537872", "537872713", enabled=False)]
    assert fd.pick_open_product(off_sale, D(18), "gpt", OPEN, D(60)) is None


def test_new_cards_reuse_the_newest_holder_name_and_sweeps_keep_the_vendor_minimum():
    old = _card(1, "12.62")
    old = fd.FundCard(**{**old.__dict__, "holder": ("Taylor", "Clark"), "created": "2026-11-14"})
    new = fd.FundCard(
        **{**_card(2, "1.1").__dict__, "holder": ("Bai", "Xiao"), "created": "2026-11-21"}
    )
    assert fd.holder_for([old, new]) == ("Bai", "Xiao")
    assert fd.holder_for([_card(3, "0")]) == fd.DEFAULT_HOLDER
    assert fd.sweep_amount(old) == D("11.62")  # keeps USD 1 (refund_keep default)
    keep_cent = fd.FundCard(**{**_card(4, "1.10").__dict__, "refund_keep": D("0.01")})
    assert fd.sweep_amount(keep_cent) == D("1.09")
    assert fd.sweep_amount(_card(5, "1.5")) is None  # below USD 1 after the keep
    assert fd.sweep_amount(_card(6, "50", status="FROZEN")) is None


def test_card_rows_keep_refund_minimum_and_holder():
    (c,) = fd.parse_fund_cards(
        {
            "list": [
                {
                    "id": 1109451,
                    "card_number": "5378727109708264",
                    "available_amount": 1.1,
                    "status": "ACTIVE",
                    "refund_keep_min": 0.01,
                    "first_name": "bai",
                    "last_name": "xiaokang",
                    "created_at": "2026-11-21T08:37:26Z",
                }
            ]
        }
    )
    assert c.refund_keep == D("0.01") and c.holder == ("bai", "xiaokang")
    assert "bai" not in repr(c)
