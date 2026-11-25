"""ADR-055: vmcardio as the second issuer of the dedicated card of a zzshu order.

Real vmcardio adapter against ``mock_vmcardio``; the real engine, ZovoCard adapter and zzshu adapter
for the issuer-order tests. Covers the issuer fallback (ZovoCard opened nothing -> vmcardio), the
swapped order, BIN priority and reserve slots, the duplicate-request recovery, a lost answer, the
sweep + delete, an unswept card, the daily-cap accounting, the settings, the webhook (signed,
unsigned, bad signature) and the hygiene rule (no card number / CVV in logs, audit rows or DB)."""

from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import text

from app.core import funding as fs
from app.devtools.mock_vmcardio import APP_ID, APP_SECRET, MockVmcardio, create_mock_vmcardio
from app.devtools.mock_zzshu import OK_KEY, MockZzshu, create_mock_zzshu
from app.suppliers.base import FundingPolicy, Outcome, SupplierError
from app.suppliers.issuers import CardIssuers
from app.suppliers.registry import build_supplier, funding_codes
from app.suppliers.vmcardio import Holder, VmcardioClient, VmcardioConfig, VmcardioSupplier
from app.suppliers.vmcardio import webhook as vm_hook
from app.suppliers.zzshu import ZzshuClient, ZzshuConfig, ZzshuSupplier
from tests.test_procurement_card_modes import _audit, _funding, _fundings
from tests.test_procurement_direct import (
    _drive,
    _job,
    dup,  # noqa: F401  (fixture)
    gpt_session,
    new_marker,
)
from tests.test_procurement_vcc import _rows, _zovo
from tests.test_twofactor import code as totp
from tests.test_twofactor import enrol
from tests.test_zzshu import GPT, ZZ, _vendor

POL = FundingPolicy(
    auto_recharge=True,
    allowance=Decimal(200),
    auto_open=True,
    vmcardio_bins=("537872", "537882", "532604", "555659"),
    vmcardio_reserve_slots=1,
)


def _supplier(state: MockVmcardio, holder: Holder | None = None) -> VmcardioSupplier:
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_mock_vmcardio(state)))
    cfg = VmcardioConfig(
        base_url="http://mockvm", app_id=APP_ID, app_secret=APP_SECRET, env="sandbox"
    )
    return VmcardioSupplier("vmcardio", VmcardioClient(cfg, http=http), holder)


KEY = "zzopen:" + str(uuid.uuid4())


# ------------------------------------------------------------------ the adapter
async def test_open_read_and_sweep_a_card_and_log_no_card_data(caplog):
    caplog.set_level(logging.DEBUG)
    state = MockVmcardio()
    sup = _supplier(state)
    card = await sup.open_dedicated_card(Decimal("30"), POL, KEY)
    assert card.id.startswith("vm:") and card.loaded == 30
    (c,) = state.cards.values()
    assert c["product_code"] == "P5378OX" and state.balance == 470  # the owner's default BIN
    assert c["label"] == "zz-" + KEY.split(":")[1].replace("-", "")
    read = await sup.read_dedicated_card(card.id)
    assert read is not None and read.status == "ACTIVE" and read.available == 30
    assert read.secret is not None and read.secret.pan == c["card_number"]
    assert read.last4 == c["card_number"][-4:]
    leftover = await sup.finish_dedicated_card(card.id)
    assert leftover == 30 and c["status"] == "DELETED" and state.balance == 500
    assert await sup.finish_dedicated_card(card.id) == 0  # already deleted: nothing to move
    text = caplog.text + repr(read) + repr(card)
    assert c["card_number"] not in text and c["cvv"] not in text
    await sup.aclose()


async def test_the_token_is_cached_and_refreshed_after_a_bad_token_answer():
    state = MockVmcardio()
    sup = _supplier(state)
    await sup.balance()
    await sup.balance()
    assert state.tokens_issued == 1
    state.expire_tokens_once = True  # the next call answers 400003 once
    assert (await sup.balance()).spendable == 500
    assert state.tokens_issued == 2
    # the token's own expiry: a lifetime inside the refresh margin is renewed on every call
    short = MockVmcardio(token_ttl=60)
    sup2 = _supplier(short)
    await sup2.balance()
    await sup2.balance()
    assert short.tokens_issued >= 1


async def test_bin_priority_and_reserve_slots():
    state = MockVmcardio()
    state.products["537872"] = ("P5378OX", 1)  # one slot left: the reserve keeps it
    sup = _supplier(state)
    await sup.open_dedicated_card(Decimal("10"), POL, "zzopen:" + str(uuid.uuid4()))
    assert next(iter(state.cards.values()))["product_code"] == "P5378XB"  # next BIN of the list
    # reserve 0 uses the last slot of the first BIN
    await sup.open_dedicated_card(
        Decimal("10"),
        FundingPolicy(**{**POL.__dict__, "vmcardio_reserve_slots": 0}),
        "zzopen:" + str(uuid.uuid4()),
    )
    assert [c["product_code"] for c in state.cards.values()] == ["P5378XB", "P5378OX"]
    # no BIN of the list has a free slot (446619 is not on it): nothing opened
    for b in ("537872", "537882", "532604", "555659"):
        state.products[b] = (state.products[b][0], 1)
    with pytest.raises(SupplierError) as ei:
        await sup.open_dedicated_card(Decimal("10"), POL, "zzopen:" + str(uuid.uuid4()))
    assert ei.value.outcome is Outcome.UNAVAILABLE and ei.value.error_code == "no_card_product"
    assert len(state.cards) == 2


async def test_wallet_cap_and_refusal_open_nothing():
    state = MockVmcardio(balance=Decimal("20"))
    sup = _supplier(state)
    for pol, need, code in (
        (POL, "30", "funds_insufficient"),
        (FundingPolicy(**{**POL.__dict__, "allowance": Decimal(5)}), "10", "funding_cap_reached"),
    ):
        with pytest.raises(SupplierError) as ei:
            await sup.open_dedicated_card(Decimal(need), pol, "zzopen:" + str(uuid.uuid4()))
        assert ei.value.outcome is Outcome.UNAVAILABLE and ei.value.error_code == code
    state.create = "refuse"  # the vendor says no before any card exists
    with pytest.raises(SupplierError) as ei:
        await sup.open_dedicated_card(Decimal("10"), POL, "zzopen:" + str(uuid.uuid4()))
    assert ei.value.outcome is Outcome.UNAVAILABLE
    state.create = "bad_gateway"  # a 5xx: a card may exist, the same-id retry looks it up
    with pytest.raises(SupplierError) as ei:
        await sup.open_dedicated_card(Decimal("10"), POL, "zzopen:" + str(uuid.uuid4()))
    assert ei.value.outcome is Outcome.RETRYABLE
    assert state.creates == 0


async def test_lost_answer_is_recovered_by_lookup_never_created_twice():
    state = MockVmcardio(create="lost")
    sup = _supplier(state)
    key = "zzopen:" + str(uuid.uuid4())
    with pytest.raises(SupplierError) as ei:
        await sup.open_dedicated_card(Decimal("30"), POL, key)
    assert ei.value.outcome is Outcome.RETRYABLE and state.creates == 1  # executed, answer lost
    state.create = "ok"
    card = await sup.open_dedicated_card(Decimal("30"), POL, key)  # same key -> duplicate -> lookup
    assert state.creates == 1 and next(iter(state.cards)) == card.id[3:]
    assert card.loaded == 30 and card.last4 == next(iter(state.cards.values()))["card_number"][-4:]


async def test_duplicate_without_one_clear_match_is_ambiguous_for_staff():
    state = MockVmcardio(create="lost")
    sup = _supplier(state)
    key = "zzopen:" + str(uuid.uuid4())
    with pytest.raises(SupplierError):
        await sup.open_dedicated_card(Decimal("30"), POL, key)
    state.create = "ok"
    # a second card with the same load appears: the lookup cannot tell which is ours
    await sup.open_dedicated_card(Decimal("30"), POL, "zzopen:" + str(uuid.uuid4()))
    with pytest.raises(SupplierError) as ei:
        await sup.open_dedicated_card(Decimal("30"), POL, key)
    assert ei.value.outcome is Outcome.AMBIGUOUS and ei.value.error_code == "open_unconfirmed"
    # and an old card is never adopted
    old = MockVmcardio(create="lost")
    old.created_at = datetime.now(timezone.utc) - timedelta(days=2)
    sup2 = _supplier(old)
    k2 = "zzopen:" + str(uuid.uuid4())
    with pytest.raises(SupplierError):
        await sup2.open_dedicated_card(Decimal("30"), POL, k2)
    old.create = "ok"
    with pytest.raises(SupplierError) as ei:
        await sup2.open_dedicated_card(Decimal("30"), POL, k2)
    assert ei.value.error_code == "open_unconfirmed" and old.creates == 1


async def test_cardholder_address_is_sent_only_when_complete():
    full = Holder("Pat", "Doe", "1 Example Street", "Sampletown", "SS", "00000", "US")
    state = MockVmcardio()
    await _supplier(state, full).open_dedicated_card(Decimal("10"), POL, KEY)
    (c,) = state.cards.values()
    assert c["card_address"] == {
        "address_line_one": "1 Example Street",
        "city": "Sampletown",
        "state": "SS",
        "country": "US",
        "post_code": "00000",
    }
    state2 = MockVmcardio()
    part = Holder("Pat", "Doe", "1 Example Street", "", "", "", "US")
    await _supplier(state2, part).open_dedicated_card(Decimal("10"), POL, KEY)
    assert next(iter(state2.cards.values()))["card_address"] is None


async def test_probe_reads_token_balance_and_products():
    sup = _supplier(MockVmcardio())
    rep = await sup.probe()
    assert rep.ok and rep.auth_ok and rep.balance and rep.balance.spendable == 500
    assert rep.card_products == 5  # the share product is not counted
    wrong = VmcardioSupplier(
        "vmcardio",
        VmcardioClient(
            VmcardioConfig(
                base_url="http://mockvm", app_id=APP_ID, app_secret="nope", env="sandbox"
            ),
            http=httpx.AsyncClient(transport=httpx.ASGITransport(app=create_mock_vmcardio())),
        ),
    )
    rep = await wrong.probe()
    assert not rep.ok and rep.auth_ok is False


# ------------------------------------------------------------------ the issuer order
class _Stub:
    """A ZovoCard stand-in answering as configured."""

    adapter_name = "zovocard"

    def __init__(self, outcome: Outcome | None, code: str | None = "funds_insufficient") -> None:
        self.outcome, self.code, self.calls = outcome, code, 0

    async def _wallet(self, fresh):
        return Decimal(1)

    async def balance(self):
        raise AssertionError

    async def read_dedicated_card(self, card_id):
        self.reads = [*getattr(self, "reads", []), card_id]
        return None

    async def finish_dedicated_card(self, card_id):
        return Decimal(7)

    async def open_dedicated_card(self, need, pol, key):
        self.calls += 1
        if self.outcome is not None:
            raise SupplierError("x", outcome=self.outcome, error_code=self.code)
        from app.suppliers.zovocard.supplier import DedicatedCard

        return DedicatedCard("4242", "4242", need, need)


@pytest.mark.parametrize(
    ("outcome", "code", "falls_through"),
    [
        (Outcome.UNAVAILABLE, "funds_insufficient", True),
        (Outcome.FATAL, "amount_below_min", True),  # a coded refusal before any card exists
        (Outcome.FATAL, None, False),
        (Outcome.AMBIGUOUS, "request_failed", False),
        (Outcome.RETRYABLE, "connect_error", False),
    ],
)
async def test_vmcardio_opens_only_after_a_definite_nothing_opened(outcome, code, falls_through):
    state = MockVmcardio()
    zovo = _Stub(outcome, code)
    issuers = CardIssuers([("zovocard", zovo), ("vmcardio", _supplier(state))])
    if falls_through:
        card = await issuers.open_dedicated_card(Decimal(30), POL, KEY)
        assert card.id.startswith("vm:") and state.creates == 1
    else:
        with pytest.raises(SupplierError) as ei:
            await issuers.open_dedicated_card(Decimal(30), POL, KEY)
        assert ei.value.outcome is outcome and state.creates == 0


async def test_staff_can_swap_the_order_and_unknown_issuers_are_ignored():
    state = MockVmcardio()
    zovo = _Stub(None)
    issuers = CardIssuers([("zovocard", zovo), ("vmcardio", _supplier(state))])
    first = await issuers.open_dedicated_card(Decimal(30), POL, KEY)
    assert first.id == "4242" and state.creates == 0  # default order: ZovoCard first
    swapped = FundingPolicy(**{**POL.__dict__, "card_issuers": ("vmcardio", "zovocard")})
    card = await issuers.open_dedicated_card(Decimal(30), swapped, KEY + "x")
    assert card.id.startswith("vm:") and zovo.calls == 1
    only_vm = FundingPolicy(**{**POL.__dict__, "card_issuers": ("vmcardio", "ghost")})
    assert (await issuers.open_dedicated_card(Decimal(30), only_vm, KEY + "y")).id.startswith("vm:")
    none = FundingPolicy(**{**POL.__dict__, "card_issuers": ("ghost",)})
    with pytest.raises(SupplierError) as ei:
        await issuers.open_dedicated_card(Decimal(30), none, KEY + "z")
    assert ei.value.outcome is Outcome.UNAVAILABLE
    # every issuer opened nothing: the FIRST one's answer is what staff see
    both = CardIssuers(
        [
            ("zovocard", _Stub(Outcome.UNAVAILABLE, "funds_insufficient")),
            ("vmcardio", _supplier(MockVmcardio(balance=Decimal(1)))),
        ]
    )
    with pytest.raises(SupplierError) as ei:
        await both.open_dedicated_card(Decimal(30), POL, KEY)
    assert ei.value.error_code == "funds_insufficient"


async def test_a_card_is_read_and_finished_by_its_own_issuer():
    state = MockVmcardio()
    zovo = _Stub(Outcome.UNAVAILABLE)
    issuers = CardIssuers([("zovocard", zovo), ("vmcardio", _supplier(state))])
    card = await issuers.open_dedicated_card(Decimal(30), POL, KEY)
    read = await issuers.read_dedicated_card(card.id)
    assert read is not None and read.secret is not None
    assert await issuers.finish_dedicated_card(card.id) == 30
    # a plain number belongs to ZovoCard, never to vmcardio
    assert await issuers.read_dedicated_card("12345") is None and zovo.reads == ["12345"]
    assert await issuers.finish_dedicated_card("12345") == 7


def test_env_list_of_funding_suppliers_and_the_vmcardio_row():
    assert funding_codes({}) == ["zovocard"]
    assert funding_codes({"ZZSHU_FUNDING_SUPPLIER": "zovocard, vmcardio,zovocard"}) == [
        "zovocard",
        "vmcardio",
    ]
    from app.suppliers.registry import SupplierConfigError

    for bad in ("Zovo Card", "a" * 40, "zovocard,,x;y"):
        with pytest.raises(SupplierConfigError):
            funding_codes({"ZZSHU_FUNDING_SUPPLIER": bad})
    env = {
        "VMCARDIO_APP_ID": APP_ID,
        "VMCARDIO_APP_SECRET": APP_SECRET,
        "VMCARDIO_CARDHOLDER_FIRST_NAME": "Pat",
    }
    sup = build_supplier(
        code="vmcardio", adapter="vmcardio", env="production", environ=env, app_env="prod"
    )
    assert isinstance(sup, VmcardioSupplier) and sup.holder.first_name == "Pat"
    assert sup.client.config.base_url == "https://vmapi.vmcardio.com"
    assert not sup.capabilities  # it sells nothing
    with pytest.raises(SupplierConfigError):
        build_supplier(
            code="vmcardio",
            adapter="vmcardio",
            env="production",
            environ={"VMCARDIO_APP_SECRET": "x"},
            app_env="prod",
        )
    with pytest.raises(SupplierConfigError):  # an id is required as well
        build_supplier(
            code="vmcardio",
            adapter="vmcardio",
            env="production",
            environ={"VMCARDIO_APP_ID": "x"},
            app_env="prod",
        )


# ------------------------------------------------------------------ settings
def test_funding_settings_for_the_issuers_are_validated():
    d = fs.normalize(None)
    assert d["zzshu_card_issuers"] == ["zovocard", "vmcardio"]
    assert d["vmcardio_bin_priority"] == ["537872", "537882", "532604", "555659"]
    assert d["vmcardio_reserve_slots"] == 1
    out = fs.validate(
        {
            "zzshu_card_issuers": ["vmcardio", "zovocard", "vmcardio"],
            "vmcardio_bin_priority": ["537872"],
            "vmcardio_reserve_slots": 0,
        }
    )
    assert out["zzshu_card_issuers"] == ["vmcardio", "zovocard"]
    assert out["vmcardio_bin_priority"] == ["537872"] and out["vmcardio_reserve_slots"] == 0
    for patch in (
        {"zzshu_card_issuers": []},
        {"zzshu_card_issuers": ["Zovo Card"]},
        {"zzshu_card_issuers": ["a_sandbox"]},
        {"zzshu_card_issuers": "zovocard"},
        {"zzshu_card_issuers": ["a", "b", "c", "d", "e"]},
        {"vmcardio_bin_priority": ["12"]},
        {"vmcardio_bin_priority": "537872"},
        {"vmcardio_reserve_slots": -1},
        {"vmcardio_reserve_slots": 101},
        {"vmcardio_reserve_slots": True},
        {"vmcardio_reserve_slots": "x"},
    ):
        with pytest.raises(ValueError):
            fs.validate(patch)
    # a stored bad value falls back to its default
    bad = fs.normalize({"zzshu_card_issuers": [], "vmcardio_reserve_slots": 999})
    assert (
        bad["zzshu_card_issuers"] == ["zovocard", "vmcardio"] and bad["vmcardio_reserve_slots"] == 1
    )


async def test_admin_edits_the_issuer_settings_with_2fa_and_only_configured_suppliers(env):
    url = "/api/v1/admin/procurement/funding"
    admin, _u, secret, _rec = await enrol(env, role="admin", email="owner@example.com")
    async with env.owner_session() as db:
        await db.execute(
            text(
                "INSERT INTO suppliers (id, tenant_id, code, adapter, label, settings) "
                "VALUES (gen_random_uuid(), :t, 'zovocard', 'zovocard', 'Z', '{}')"
            ),
            {"t": env.tenant_id},
        )
        await db.commit()
    g = (await admin.get(url)).json()
    assert g["issuer_options"] == ["zovocard"]  # no vmcardio row yet
    assert g["settings"]["zzshu_card_issuers"] == ["zovocard", "vmcardio"]
    r = await admin.put(
        url, json={"zzshu_card_issuers": ["vmcardio", "zovocard"], "totp_code": totp(secret, 1)}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_funding_settings"
    body = {
        "zzshu_card_issuers": ["zovocard"],
        "vmcardio_reserve_slots": 2,
        "vmcardio_bin_priority": ["537872", "537882"],
        "totp_code": totp(secret, 1),
    }
    ok = await admin.put(url, json=body)
    assert ok.status_code == 200, ok.text
    st = ok.json()["settings"]
    assert st["zzshu_card_issuers"] == ["zovocard"] and st["vmcardio_reserve_slots"] == 2
    assert st["vmcardio_bin_priority"] == ["537872", "537882"]
    (row,) = await _audit(env, "procurement.funding_settings_updated")
    assert row[0] == "warning" and row[1]["two_factor"] is True
    # without a fresh code nothing changes
    r = await admin.put(url, json={**body, "totp_code": "000000"})
    assert r.status_code == 422
    assert (
        await admin.put(url, json={"vmcardio_reserve_slots": 500, "totp_code": "1"})
    ).status_code == 422


# ------------------------------------------------------------------ engine: issuer order
async def _setup(env, dup, vendor, vm, *, zz=None, issuers=None):  # noqa: F811
    """ZovoCard (wallet only) + vmcardio as the two issuers of one zzshu source."""
    await _funding(env, auto_recharge=True, **({"zzshu_card_issuers": issuers} if issuers else {}))
    # the ZovoCard source stays enabled (the checkout pre-check needs one) but behind zzshu
    zsid, _o = await _zovo(dup, vendor, GPT, priority=10)
    zz = zz or MockZzshu()
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_mock_zzshu(zz)))
    client = ZzshuClient(
        ZzshuConfig(base_url="http://mockzz/api/v1/third-party", api_key=OK_KEY, env="sandbox"),
        http=http,
    )
    dup.zz_http = http
    sup = ZzshuSupplier(
        "zzshu", client, CardIssuers([("zovocard", dup.mocks[zsid]), ("vmcardio", _supplier(vm))])
    )
    sup.status_recheck_s = 0
    await dup.supplier(sup, code="zzshu", adapter="zzshu", params=ZZ, priority=5)
    return zz


async def _run(env, dup):  # noqa: F811
    buyer, order = await dup.paid_direct(gpt_session(new_marker()))
    try:
        await _drive(env, dup, order, buyer, rounds=14)
    except AssertionError as exc:
        jobs = await dup.jobs(order["id"])
        raise AssertionError(
            f"{exc}; jobs={[(j.state, j.step, j.error_code, j.upstream_ref) for j in jobs]}"
        ) from None
    return buyer, order


async def test_zovocard_cannot_open_so_vmcardio_does_and_everything_is_recorded(env, dup, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    vendor, vm = _vendor(), MockVmcardio()
    vendor.forced["/cards/open"].append("insufficient_balance")  # ZovoCard: wallet too low
    zz = await _setup(env, dup, vendor, vm)
    buyer, order = await _run(env, dup)
    job = await _job(dup, order)
    assert (
        job.state == "fulfilled" and vendor.card_opens == 0 and vm.creates == 1 and zz.creates == 1
    )
    ((vid, card),) = vm.cards.items()
    assert job.card_id == "vm:" + vid and job.card_last4 == card["card_number"][-4:]
    assert card["status"] == "DELETED" and vm.balance == 500  # swept and deleted, nothing left
    # cap accounting: the open counts against the daily cap under the card's id
    funds = await _fundings(env)
    assert [(f[0], f[1], f[2]) for f in funds] == [("done", Decimal(30), "vm:" + vid)]
    # the staff timeline and the audit rows name the issuer, card id, loaded and leftover
    msgs = [e["msg"] for e in job.progress["events"] if e.get("cat") == "zzshu"]
    assert any("vmcardio" in m and vid in m and "loaded 30.00" in m for m in msgs)
    assert any("returned 30.00 USD" in m for m in msgs)
    opened, swept = [
        r[0]
        for r in await _rows(
            env, "SELECT detail FROM audit_log WHERE action = 'procurement.zzshu_card' ORDER BY id"
        )
    ]
    assert opened["issuer"] == swept["issuer"] == "vmcardio" and swept["swept"] is True
    assert Decimal(opened["loaded"]) == 30 and Decimal(swept["leftover"]) == 30
    assert not await _audit(env, "procurement.zzshu_card_unswept")
    # zzshu got the vmcardio card; neither number nor CVV is anywhere else
    assert (
        zz.seen_cards[0]["cardNumber"] == card["card_number"] and zz.seen_cards[0]["cvv"] == "737"
    )
    sweep = (
        caplog.text
        + json.dumps(
            [
                list(map(str, r))
                for r in await _rows(env, "SELECT detail, before, after FROM audit_log")
            ]
        )
        + str(await _rows(env, "SELECT progress::text, card_id, card_last4 FROM procurement_jobs"))
        + str(await _rows(env, "SELECT payload::text FROM supplier_events"))
    )
    assert card["card_number"] not in sweep and f'"{card["cvv"]}"' not in sweep
    cards = (await buyer.post(f"/api/v1/orders/{order['id']}/reveal")).json()["cards"]
    assert cards[0]["code"].startswith("gpt/plus----")


async def test_zovocard_stays_first_and_vmcardio_is_untouched_when_it_can_open(env, dup):  # noqa: F811
    vendor, vm = _vendor(), MockVmcardio()
    await _setup(env, dup, vendor, vm)
    _b, order = await _run(env, dup)
    job = await _job(dup, order)
    assert job.state == "fulfilled" and vendor.card_opens == 1 and vm.creates == 0
    assert job.card_id.isdigit()
    assert not vm.requests or all(p != "/v2/createCard" for _m, p in vm.requests)


async def test_staff_swap_makes_vmcardio_the_first_issuer(env, dup):  # noqa: F811
    vendor, vm = _vendor(), MockVmcardio()
    await _setup(env, dup, vendor, vm, issuers=["vmcardio", "zovocard"])
    _b, order = await _run(env, dup)
    job = await _job(dup, order)
    assert job.state == "fulfilled" and vm.creates == 1 and vendor.card_opens == 0
    assert job.card_id.startswith("vm:")


async def test_a_card_that_cannot_be_deleted_is_reported_as_unswept(env, dup):  # noqa: F811
    vendor, vm = _vendor(), MockVmcardio(refuse_delete=True)
    vendor.forced["/cards/open"].append("insufficient_balance")
    await _setup(env, dup, vendor, vm)
    _b, order = await _run(env, dup)
    job = await _job(dup, order)
    assert job.state == "fulfilled"
    ((vid, card),) = vm.cards.items()
    assert card["status"] == "ACTIVE" and Decimal(str(card["available_amount"])) < 30  # refunded
    (row,) = await _audit(env, "procurement.zzshu_card_unswept")
    assert (
        row[0] == "warning" and row[1]["issuer"] == "vmcardio" and row[1]["card_id"] == "vm:" + vid
    )
    assert any(e.get("code") == "card_unswept" for e in job.progress["events"])


async def test_a_lost_create_answer_at_vmcardio_never_makes_a_second_card(env, dup):  # noqa: F811
    vendor, vm = _vendor(), MockVmcardio(create="lost")
    vendor.forced["/cards/open"].append("insufficient_balance")
    await _setup(env, dup, vendor, vm)
    buyer, order = await dup.paid_direct(gpt_session(new_marker()))
    await dup.run()  # the create ran, its answer was lost: the job waits (ambiguous)
    assert vm.creates == 1
    vm.create = "ok"  # the vendor answers again
    await _drive(env, dup, order, buyer, rounds=14)
    job = await _job(dup, order)
    assert vm.creates == 1 and job.state == "fulfilled" and job.card_id.startswith("vm:")


# ------------------------------------------------------------------ webhook
def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return key, pem


def _sign(key, raw: bytes) -> str:
    return base64.b64encode(key.sign(raw, padding.PKCS1v15(), hashes.SHA256())).decode()


EVENT = {
    "auth_id": "VM202611250001",
    "vm_card_id": "vmc0123456789abcdef",
    "status": "approved",
    "type": "authorization",
    "amount": 12.34,
    "currency": "USD",
    "merchant_name": "EXAMPLE SHOP",
    "description": "order 98765432 code 123456",
    "card_number": "5378720000001234",
    "cvv": "737",
}


@pytest.fixture
async def vm_hook_env(env):
    from app.models import SupplierAccount

    sid = uuid.uuid4()
    async with env.owner_session() as db:
        db.add(
            SupplierAccount(
                id=sid,
                tenant_id=env.tenant_id,
                code="vmcardio",
                adapter="vmcardio",
                label="vm",
                env="production",
                settings={},
                enabled=True,
            )
        )
        await db.commit()
    return sid


def test_webhook_normalise_keeps_a_whitelist_and_masks_digit_runs():
    dedup, payload = vm_hook.normalise(EVENT, verified=True)
    assert dedup == "vmcardio:VM202611250001:authorization:approved:vmc0123456789abcdef"
    assert payload["amount"] == "12.34" and payload["verified"] is True
    assert "card_number" not in payload and "cvv" not in payload
    assert payload["description"] == "order **** code ****"
    with pytest.raises(Exception):  # noqa: B017
        vm_hook.normalise({"status": "x"}, verified=False)


async def test_webhook_signed_is_stored_verified_and_changes_nothing_else(env, vm_hook_env):
    key, pem = _keypair()
    env.res.settings = env.res.settings.model_copy(update={"vmcardio_webhook_public_key": pem})
    c = env.client(csrf=False)
    raw = json.dumps(EVENT).encode()
    h = {"X-VM-SIGN": _sign(key, raw), "Content-Type": "application/json"}
    orders_before = await env.scalar("SELECT count(*) FROM procurement_jobs")
    r = await c.post("/api/v1/webhooks/vmcardio", content=raw, headers=h)
    assert r.status_code == 200 and r.json() == {"code": 0, "msg": "ok"}
    r = await c.post("/api/v1/webhooks/vmcardio", content=raw, headers=h)  # a repeat delivery
    assert r.status_code == 200
    rows = await _rows(
        env, "SELECT kind, payload::text, processed_at IS NOT NULL FROM supplier_events"
    )
    assert len(rows) == 1 and rows[0][0] == "vmcardio_card_transaction" and rows[0][2] is True
    stored = json.loads(rows[0][1])
    assert (
        stored["verified"] is True and "5378720000001234" not in rows[0][1] and "cvv" not in stored
    )
    assert await env.scalar("SELECT count(*) FROM outbox WHERE topic = 'supplier.event'") == 0
    assert await env.scalar("SELECT count(*) FROM procurement_jobs") == orders_before


async def test_webhook_bad_or_missing_signature_is_refused_when_a_key_is_set(env, vm_hook_env):
    key, pem = _keypair()
    other, _ = _keypair()
    env.res.settings = env.res.settings.model_copy(update={"vmcardio_webhook_public_key": pem})
    c = env.client(csrf=False)
    raw = json.dumps(EVENT).encode()
    for headers in (
        {"X-VM-SIGN": _sign(other, raw)},  # signed by somebody else
        {"X-VM-SIGN": _sign(key, raw + b" ")},  # body changed after signing
        {"X-VM-SIGN": "not base64!"},
        {},
    ):
        r = await c.post("/api/v1/webhooks/vmcardio", content=raw, headers=headers)
        assert r.status_code == 401, headers
    assert await env.scalar("SELECT count(*) FROM supplier_events") == 0
    # a configured key that cannot be read never downgrades to "unverified"
    env.res.settings = env.res.settings.model_copy(update={"vmcardio_webhook_public_key": "junk"})
    r = await c.post("/api/v1/webhooks/vmcardio", content=raw, headers={})
    assert r.status_code == 503 and await env.scalar("SELECT count(*) FROM supplier_events") == 0


async def test_webhook_without_a_key_is_accepted_and_marked_unverified(env, vm_hook_env):
    c = env.client(csrf=False)
    raw = json.dumps(EVENT).encode()
    r = await c.post("/api/v1/webhooks/vmcardio", content=raw, headers={"X-VM-SIGN": "ignored"})
    assert r.status_code == 200 and r.json() == {"code": 0, "msg": "ok"}
    (row,) = await _rows(env, "SELECT payload::text FROM supplier_events")
    assert json.loads(row[0])["verified"] is False and "5378720000001234" not in row[0]
    # malformed / oversized bodies are refused whatever the key setting is
    assert (await c.post("/api/v1/webhooks/vmcardio", content=b"[1]")).status_code == 400
    assert (await c.post("/api/v1/webhooks/vmcardio", content=b"x" * 70_000)).status_code == 413


async def test_webhook_rate_limit_and_no_supplier_row(env):
    c = env.client(csrf=False)
    raw = json.dumps(EVENT).encode()
    r = await c.post("/api/v1/webhooks/vmcardio", content=raw)  # no vmcardio account: acknowledged
    assert r.status_code == 200 and await env.scalar("SELECT count(*) FROM supplier_events") == 0
    from app.modules import webhooks

    assert webhooks.PER_IP_PER_MIN == 300  # the same limit as the ZovoCard webhook
