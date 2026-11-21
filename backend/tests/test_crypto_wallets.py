"""Crypto networks + BEpusdt receiving wallets from the admin (owner 2026-11-21)."""

from __future__ import annotations

import pytest

from app.modules.admin import crypto_wallets as cw
from app.modules.payments import channels as ch
from app.payments.bepusdt_admin import BepusdtAdmin, BepusdtAdminError
from tests.test_procurement_vcc import _rows

URL = "/api/v1/admin/payments/crypto"
AGENTIC = "0x97f64f884cec7272515849cdac6b37b874cf7b4b"


def test_the_catalogue_covers_every_bepusdt_stablecoin_network():
    assert ch.USDT_CHANNELS["usdt_polygon"] == "usdt.polygon"
    assert ch.USDT_CHANNELS["usdc_base"] == "usdc.base"
    assert list(ch.USDT_CHANNELS)[:2] == ["usdt_xlayer", "usdt_polygon"]  # cheap networks first
    assert ch.LABELS["usdt_polygon"]["en"] == "USDT (Polygon PoS)"
    assert ch.LABELS["usdt_trc20"]["en"] == "USDT (TRON TRC20)"  # unchanged for old orders
    assert ch.is_usdt("usdc_solana") and ch.provider("usdt_ton") == "bepusdt"
    # every crypto channel is OFF until staff switch it on
    assert (
        not ch.channel_cfg({}, "usdt_polygon").enabled and ch.channel_cfg({}, "zpay_alipay").enabled
    )


@pytest.mark.parametrize(
    ("trade_type", "address", "ok"),
    [
        ("usdt.polygon", AGENTIC, True),
        ("usdt.polygon", "TTAWAWPXTouM5Pztv6Tu5dhqFZXJtSh1N2", False),
        ("usdt.trc20", "TTAWAWPXTouM5Pztv6Tu5dhqFZXJtSh1N2", True),
        ("usdt.trc20", AGENTIC, False),
        ("usdt.solana", "F8V7Wbrsv6dcvVfS58Ghpu3vA3wY6ZtZ4rqbMoJEsxZh", True),
        ("usdt.ton", "UQ" + "A" * 46, True),
        ("usdt.nowhere", AGENTIC, False),
    ],
)
def test_addresses_are_checked_per_network(trade_type, address, ok):
    assert cw.valid_address(trade_type, address) is ok


class FakeBep:
    def __init__(self) -> None:
        self.rows = [
            {"id": 2, "name": "X Layer", "trade_type": "usdt.xlayer", "address": AGENTIC,
             "enabled": True, "remark": "", "other_notify": 0, "updated_at": ""},
        ]  # fmt: skip
        self.calls: list[tuple[str, dict]] = []

    @property
    def configured(self) -> bool:
        return True

    async def wallets(self):
        return [dict(r) for r in self.rows]

    async def add(self, *, name, trade_type, address, remark=""):
        if any(r["address"] == address and r["trade_type"] == trade_type for r in self.rows):
            raise BepusdtAdminError("bepusdt_refused", "exists")
        self.calls.append(("add", {"trade_type": trade_type, "address": address}))
        self.rows.append(
            {"id": 9, "name": name, "trade_type": trade_type, "address": address, "enabled": True,
             "remark": remark, "other_notify": 0, "updated_at": ""}
        )  # fmt: skip

    async def modify(self, row):
        self.calls.append(("mod", row))
        for r in self.rows:
            if r["id"] == row["id"]:
                r.update(address=row["address"], enabled=row["status"] == 1, remark=row["remark"])

    async def delete(self, wallet_id):
        self.calls.append(("del", {"id": wallet_id}))
        self.rows = [r for r in self.rows if r["id"] != wallet_id]


async def test_an_admin_adds_switches_and_removes_a_polygon_wallet(env, monkeypatch):
    fake = FakeBep()
    monkeypatch.setattr(cw, "_admin", lambda _res: fake)
    admin, _ = await env.login("admin")
    v = (await admin.get(URL)).json()
    poly = next(c for c in v["channels"] if c["id"] == "usdt_polygon")
    assert poly["has_wallet"] is False and poly["enabled"] is False and poly["family"] == "evm"
    assert next(c for c in v["channels"] if c["id"] == "usdt_xlayer")["has_wallet"] is True

    bad = await admin.post(
        f"{URL}/wallets",
        json={"trade_type": "usdt.polygon", "address": "TTAWAWPXTouM5Pztv6Tu5dhqFZXJtSh1N2"},
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "bad_address"
    r = await admin.post(f"{URL}/wallets", json={"trade_type": "usdt.polygon", "address": AGENTIC})
    assert r.status_code == 200, r.text
    assert next(c for c in r.json()["channels"] if c["id"] == "usdt_polygon")["has_wallet"] is True
    dup = await admin.post(
        f"{URL}/wallets", json={"trade_type": "usdt.polygon", "address": AGENTIC}
    )
    assert dup.status_code == 422 and dup.json()["error"]["code"] == "bepusdt_refused"

    r = await admin.patch(f"{URL}/wallets/9", json={"enabled": False})
    assert r.status_code == 200 and ("mod", {**fake.calls[-1][1]}) == fake.calls[-1]
    assert fake.calls[-1][1]["status"] == 0 and fake.calls[-1][1]["trade_type"] == "usdt.polygon"
    assert (await admin.delete(f"{URL}/wallets/9")).status_code == 200
    assert (await admin.delete(f"{URL}/wallets/9")).status_code == 404
    rows = await _rows(
        env,
        "SELECT action FROM audit_log WHERE action LIKE 'payments.crypto_wallet_%' ORDER BY id",
    )
    actions = [r[0] for r in rows]
    assert actions == [
        "payments.crypto_wallet_added",
        "payments.crypto_wallet_updated",
        "payments.crypto_wallet_removed",
    ]


async def test_staff_and_tokens_cannot_manage_wallets(env, monkeypatch):
    monkeypatch.setattr(cw, "_admin", lambda _res: FakeBep())
    staff, _ = await env.login("agent")
    assert (await staff.get(URL)).status_code == 403
    assert (
        await staff.post(f"{URL}/wallets", json={"trade_type": "usdt.polygon", "address": AGENTIC})
    ).status_code == 403


async def test_without_the_bepusdt_admin_login_the_page_says_so(env):
    admin, _ = await env.login("admin")
    v = (await admin.get(URL)).json()
    assert v["error"] == "bepusdt_admin_off" and v["wallets"] == []
    assert len(v["channels"]) == len(ch.USDT_CHANNELS)


def test_buyers_can_pick_a_newly_switched_on_network():
    """A channel id outside the old fixed list is accepted by the order schemas now."""
    from pydantic import ValidationError

    from app.modules.orders.schemas import PayIn

    assert PayIn(channel="usdt_polygon").channel == "usdt_polygon"
    with pytest.raises(ValidationError):
        PayIn(channel="usdt_moon")


def test_the_client_is_configured_only_with_every_part():
    assert not BepusdtAdmin("http://bepusdt:8080", "u", "p", "").configured
    assert BepusdtAdmin("http://bepusdt:8080", "u", "p", "entry").configured
