"""The account pre-check BEFORE payment for every direct order (review-v12 §24).

At checkout the API seals the buyer's session for one call (AAD ``preflight:<nonce>``), procurement
opens it in memory and asks the vendor's preflight whether this account can take the plan now. A
logged-out session, an account that already holds the plan / a subscription, or a vendor block
stops checkout BEFORE any order exists, so the buyer can never pay for an order that cannot run.
"""

from __future__ import annotations

# ruff: noqa: F811  (the ``dup`` fixture is imported from the direct tests)
import base64
import logging
import uuid

import httpx
import pytest

from app.procurement.credopen import CredentialOpener
from tests.conftest import CRED_KID, CRED_PRIV
from tests.test_procurement_direct import (
    GPT,
    _creds,
    _drive,
    _job,
    _vendor,
    dup,  # noqa: F401
    gpt_session,
    new_marker,
)
from tests.test_procurement_vcc import _rows, _sql, _zovo

API = "/api/v1"


async def _attempt(buyer, dup, session: str):
    return await buyer.post(
        f"{API}/orders", json={"product_id": str(dup.product.id), "qty": 1, "session": session}
    )


async def _orders(env) -> int:
    return await env.scalar("SELECT count(*) FROM orders")


async def _refusals(env) -> list[str]:
    rows = await _rows(
        env, "SELECT detail::text FROM audit_log WHERE action = 'order.direct_preflight_refused'"
    )
    return [r[0] for r in rows]


async def test_a_usable_account_passes_and_the_order_is_created(env, dup):
    vendor = _vendor()
    await _zovo(dup, vendor, GPT)
    buyer, _ = await env.login("buyer")
    r = await _attempt(buyer, dup, gpt_session(new_marker()))
    assert r.status_code == 200, r.text
    order = r.json()
    assert order["status"] == "pending_payment"
    assert vendor.preflights == 1 and vendor.direct_executions == 0  # a read, nothing bought
    assert await _creds(env, order) == 1  # sealed for the order as before
    assert await _refusals(env) == []


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ({"direct_current_plan": "plus"}, "preflight_plan_active"),
        ({"preflight_over": {"subscription_has_active": True}}, "preflight_plan_active"),
        ({"preflight_over": {"blocking_reason": "risk_control"}}, "preflight_account_blocked"),
        (
            {"preflight_over": {"subscription_recovery_required": True}},
            "preflight_account_blocked",
        ),
        (
            {"preflight_over": {"can_purchase_at": "2999-01-01T00:00:00Z"}},
            "preflight_purchase_not_yet",
        ),
        ({"reject_sessions": True}, "preflight_session_invalid"),
    ],
)
async def test_an_account_that_cannot_take_the_plan_never_reaches_payment(env, dup, setup, code):
    vendor = _vendor()
    for key, value in setup.items():
        setattr(vendor, key, value)
    await _zovo(dup, vendor, GPT)
    buyer, _ = await env.login("buyer")
    marker = new_marker()
    r = await _attempt(buyer, dup, gpt_session(marker))
    assert r.status_code == 422 and r.json()["error"]["code"] == code, r.text
    # no order, no stored session, nothing bought
    assert await _orders(env) == 0 and await _creds(env) == 0
    assert vendor.direct_executions == 0
    (row,) = await _refusals(env)
    # the reason is recorded, never the session
    assert code.removeprefix("preflight_") in row and marker not in row
    if code == "preflight_plan_active" and "direct_current_plan" in setup:
        assert r.json()["error"]["current_plan"] == "plus"  # the buyer's own plan, shown back


async def test_vendor_or_procurement_down_fails_closed(env, dup):
    vendor = _vendor()
    vendor.force("/gpt-direct/preflight", "503")
    await _zovo(dup, vendor, GPT)
    buyer, _ = await env.login("buyer")
    r = await _attempt(buyer, dup, gpt_session(new_marker()))
    assert r.status_code == 503 and r.json()["error"]["code"] == "preflight_unavailable"
    real = env.res.procurement
    env.res.procurement = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("x"))),
        base_url="http://procurement",
    )
    try:
        r = await _attempt(buyer, dup, gpt_session(new_marker()))
    finally:
        await env.res.procurement.aclose()
        env.res.procurement = real
    assert r.status_code == 503 and r.json()["error"]["code"] == "preflight_unavailable"
    assert await _orders(env) == 0 and await _creds(env) == 0
    assert len(await _refusals(env)) == 2
    # the vendor answers again: the same buyer goes through
    r = await _attempt(buyer, dup, gpt_session(new_marker()))
    assert r.status_code == 200, r.text


async def test_the_pre_check_is_rate_limited_per_buyer(env, dup):
    vendor = _vendor()
    vendor.direct_current_plan = "plus"
    await _zovo(dup, vendor, GPT)
    buyer, _ = await env.login("buyer")
    codes = [
        (await _attempt(buyer, dup, gpt_session(new_marker()))).json()["error"]["code"]
        for _ in range(13)
    ]
    assert codes[:12] == ["preflight_plan_active"] * 12 and codes[12] == "rate_limited"
    assert vendor.preflights == 12  # the 13th never reached the vendor


async def test_a_new_session_for_a_paid_order_gets_the_same_pre_check(env, dup):
    vendor = _vendor()
    await _zovo(dup, vendor, GPT)
    buyer, order = await dup.paid_direct(gpt_session(new_marker()))
    await _sql(env, "DELETE FROM procurement_credentials")  # expired / swept before it was used
    vendor.reject_sessions = True
    url = f"{API}/orders/{order['id']}/direct-session"
    r = await buyer.post(url, json={"session": gpt_session(new_marker())})
    assert r.status_code == 422 and r.json()["error"]["code"] == "preflight_session_invalid"
    assert await _creds(env, order) == 0  # nothing stored
    vendor.reject_sessions = False
    r = await buyer.post(url, json={"session": gpt_session(new_marker())})
    assert r.status_code == 200, r.text
    assert await _creds(env, order) == 1


async def test_procurement_opens_only_a_blob_sealed_for_a_pre_check(env, dup):
    """A sealed ORDER session (AAD = order id) cannot be replayed through the pre-check route, and
    the route answers coarse keys only (never the session)."""
    vendor = _vendor()
    await _zovo(dup, vendor, GPT)
    sealer = env.res.cred_sealer
    tid = dup.product.tenant_id
    session = gpt_session(new_marker())
    nonce = uuid.uuid4()
    token = env.res.settings.procurement_token

    async def call(blob: bytes) -> dict:
        r = await env.res.procurement.post(
            "/internal/direct-preflight",
            json={
                "tenant_id": str(tid),
                "product_id": str(dup.product.id),
                "variant_id": None,
                "nonce": str(nonce),
                "blob": base64.b64encode(blob).decode(),
            },
            headers={"X-Internal-Token": token},
        )
        assert r.status_code == 200, r.text
        return r.json()

    other = sealer.seal(session, tenant_id=str(tid), order_id=str(nonce))  # an order's AAD
    assert await call(other) == {
        "status": "unavailable",
        "reason": "unreadable_session",
        "account": None,
        "current_plan": None,
    }
    good = sealer.seal(session, tenant_id=str(tid), order_id=f"preflight:{nonce}")
    out = await call(good)
    assert out["status"] == "passed" and out["current_plan"] == "free"
    assert set(out) == {"status", "reason", "account", "current_plan"}
    assert vendor.preflights == 1
    # without the internal token: refused before anything is opened
    r = await env.res.procurement.post("/internal/direct-preflight", json={})
    assert r.status_code == 401


async def test_the_session_is_not_logged_on_any_pre_check_path(env, dup, caplog):
    caplog.set_level(logging.DEBUG)
    vendor = _vendor()
    await _zovo(dup, vendor, GPT)
    buyer, _ = await env.login("buyer")
    marker = new_marker()
    assert (await _attempt(buyer, dup, gpt_session(marker))).status_code == 200
    vendor.direct_current_plan = "plus"
    marker2 = new_marker()
    assert (await _attempt(buyer, dup, gpt_session(marker2))).status_code == 422
    logged = caplog.text
    assert marker not in logged and marker2 not in logged
    dump = await env.scalar("SELECT coalesce(string_agg(detail::text, ' '), '') FROM audit_log")
    assert marker not in dump and marker2 not in dump


def test_the_opener_refuses_the_pre_check_namespace_for_an_order_blob():
    """The AAD namespaces never overlap: an order id is a UUID, a pre-check ``preflight:<uuid>``."""
    from app.core.credvault import CredentialSealer

    opener = CredentialOpener({CRED_KID: CRED_PRIV})
    pub = CRED_PRIV.public_key()
    sealer = CredentialSealer({CRED_KID: pub}, CRED_KID)
    n = uuid.uuid4()
    blob = sealer.seal("x" * 80, tenant_id="t", order_id=f"preflight:{n}")
    assert opener.open(blob, tenant_id="t", order_id=f"preflight:{n}") == "x" * 80
    with pytest.raises(Exception):  # noqa: B017  (any vault error)
        opener.open(blob, tenant_id="t", order_id=str(n))


async def test_showcase_simulator_delivers_an_order_for_the_pasted_account(env, dup):
    """Demo regression (owner test 2026-11-18): in showcase mode the simulator's preflight reports
    the pasted account, so its order must report the same account, or every order is held as
    ``order_mismatch``."""
    vendor = _vendor()
    vendor.catalog_full = True
    await _zovo(dup, vendor, GPT)
    buyer, order = await dup.paid_direct(gpt_session(new_marker(), email="someone.real@gmail.com"))
    await _drive(env, dup, order, buyer)
    job = await _job(dup, order)
    assert job.state == "fulfilled" and job.account_mask == "s***@gmail.com"
