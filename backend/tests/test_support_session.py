"""Buyer-consented session sharing with support (owner request 2026-11-18, review-v12 §26).

The consent travels with the sealed session; when the direct order goes to staff the sealed blob is
copied for support (only with consent), staff read it through procurement with a reason (audited,
critical), and it is swept after 72 h or once the order was delivered."""

from __future__ import annotations

# ruff: noqa: F811  (the ``dup`` fixture is imported from the direct tests)
import uuid

from sqlalchemy import text

from tests.helpers import buy, pay_order
from tests.test_procurement_direct import (
    GPT,
    _job,
    _vendor,
    dup,  # noqa: F401
    gpt_session,
    new_marker,
)
from tests.test_procurement_vcc import _rows, _sql, _zovo

ADMIN = "/api/v1/admin/orders"


async def _paid(env, dup, *, share: bool):
    marker = new_marker()
    buyer, _ = await env.login("buyer")
    order = await buy(buyer, dup.product, 1, session=gpt_session(marker), share_session=share)
    dup.forget_checkout_preflight()
    await pay_order(env, buyer, order)
    assert await env.drain() >= 1
    return buyer, order, marker


async def _held(env, dup, vendor, *, share: bool):
    buyer, order, marker = await _paid(env, dup, share=share)
    vendor.direct_current_plan = "plus"  # the execution refuses: the job goes to staff
    await dup.run()
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "plan_already_active"
    return buyer, order, marker


async def _kept(env) -> int:
    return await env.scalar("SELECT count(*) FROM support_sessions")


async def test_consent_is_stored_with_the_sealed_session(env, dup):
    await _zovo(dup, _vendor(), GPT)
    _b, yes, _m = await _paid(env, dup, share=True)
    _b, no, _m = await _paid(env, dup, share=False)
    rows = dict(
        (str(r[0]), r[1])
        for r in await _rows(
            env, "SELECT order_id, share_with_support FROM procurement_credentials"
        )
    )
    assert rows == {yes["id"]: True, no["id"]: False}


async def test_a_held_order_keeps_a_consented_session_for_support_and_staff_read_it(env, dup):
    vendor = _vendor()
    await _zovo(dup, vendor, GPT)
    _buyer, order, marker = await _held(env, dup, vendor, share=True)
    assert await _kept(env) == 1
    admin, _ = await env.login("admin")
    st = (await admin.get(f"{ADMIN}/{order['id']}/support-session")).json()
    assert st["available"] is True and st["reason"] == "plan_already_active"
    assert st["revealed_count"] == 0 and "session" not in st
    url = f"{ADMIN}/{order['id']}/support-session/reveal"
    r = await admin.post(url, json={"reason": "short"})
    assert r.status_code == 422
    r = await admin.post(url, json={"reason": "buyer says the plan is not active"})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert marker in r.json()["session"]
    (row,) = await _rows(
        env,
        "SELECT severity, detail::text FROM audit_log "
        "WHERE action = 'order.support_session_revealed'",
    )
    assert row[0] == "critical" and "buyer says the plan" in row[1] and marker not in row[1]
    st = (await admin.get(f"{ADMIN}/{order['id']}/support-session")).json()
    assert st["revealed_count"] == 1 and st["last_revealed_at"]
    # an agent may read it too; a buyer may not
    agent, _ = await env.login("agent")
    assert (
        await agent.post(url, json={"reason": "checking the account for the buyer"})
    ).status_code == 200
    buyer2, _ = await env.login("buyer")
    assert (await buyer2.post(url, json={"reason": "let me see someone else"})).status_code in (
        401,
        403,
    )


async def test_without_consent_nothing_is_kept_or_shown(env, dup):
    vendor = _vendor()
    await _zovo(dup, vendor, GPT)
    _buyer, order, _m = await _held(env, dup, vendor, share=False)
    await dup.run()  # the sweeper's copy pass as well
    assert await _kept(env) == 0
    admin, _ = await env.login("admin")
    st = (await admin.get(f"{ADMIN}/{order['id']}/support-session")).json()
    assert st == {
        "available": False,
        "consented": False,
        "account": None,
        "reason": None,
        "kept_since": None,
        "revealed_count": 0,
        "last_revealed_at": None,
    }
    r = await admin.post(
        f"{ADMIN}/{order['id']}/support-session/reveal", json={"reason": "trying anyway here"}
    )
    assert r.status_code == 404


async def test_the_copy_is_swept_after_the_window_and_outlives_the_credential(env, dup):
    vendor = _vendor()
    await _zovo(dup, vendor, GPT)
    await _held(env, dup, vendor, share=True)
    # the credential's own ceiling passes: the support copy stays
    await _sql(env, "UPDATE procurement_credentials SET created_at = now() - interval '7 hours'")
    await dup.run()
    assert await env.scalar("SELECT count(*) FROM procurement_credentials") == 0
    assert await _kept(env) == 1
    await _sql(env, "UPDATE support_sessions SET created_at = now() - interval '73 hours'")
    await dup.run()
    assert await _kept(env) == 0


async def test_another_tenant_sees_nothing(env, dup):
    vendor = _vendor()
    await _zovo(dup, vendor, GPT)
    _buyer, order, _m = await _held(env, dup, vendor, share=True)
    other, _ = await env.login("admin", tenant_id=env.other_tenant_id)
    assert (await other.get(f"{ADMIN}/{order['id']}/support-session")).status_code in (
        401,
        403,
        404,
    )
    r = await other.post(
        f"{ADMIN}/{order['id']}/support-session/reveal", json={"reason": "cross tenant attempt"}
    )
    assert r.status_code in (401, 403, 404)
    assert uuid.UUID(order["id"])
    assert (
        await env.scalar(
            "SELECT count(*) FROM audit_log WHERE action = 'order.support_session_revealed'"
        )
        == 0
    )


async def test_the_api_role_cannot_read_the_sealed_copy(env, dup):
    vendor = _vendor()
    await _zovo(dup, vendor, GPT)
    await _held(env, dup, vendor, share=True)
    from sqlalchemy.exc import DBAPIError

    async with env.api_session(env.tenant_id) as db:
        try:
            await db.execute(text("SELECT blob FROM support_sessions"))
        except DBAPIError as exc:
            assert "permission denied" in str(exc).lower()
        else:
            raise AssertionError("the API role read the sealed session")
