"""issue #27 (review-v12 §20.4): the half-price eligibility check BEFORE payment.

Methods: ``vendor_preflight`` (mock vendor), ``chatgpt_direct`` (a stub transport, never the real
ChatGPT) and ``manual`` (staff decide, the order is held before the supplier is called)."""

from __future__ import annotations

import json
import logging
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.devtools.mock_zovocard import MockZovo
from tests.helpers import pay_order
from tests.test_procurement_direct import (
    JOBS,
    DUp,
    _all_dumps,
    _job,
    _leaks,
    _refund,
    _settle,
    _vendor,
    gpt_session,
    new_marker,
)
from tests.test_procurement_vcc import _rows, _zovo

API = "/api/v1"
HALF = {"product": "gpt", "plan": "plus_half", "card_id": 4242}
OPT = f"{API}/admin/procurement/options"


@pytest.fixture(autouse=True)
def _no_account_age_gate(monkeypatch):
    """The 24 h account-age gate of ``chatgpt_direct`` has its own test; the rest use new buyers."""
    from datetime import timedelta

    from app.modules.orders import halfcheck as oh

    monkeypatch.setattr(oh, "MIN_ACCOUNT_AGE", timedelta(0))


@pytest.fixture
async def hp(env):
    u = DUp(env)
    await u.start()
    yield u
    await u.close()


async def _method(env, method: str) -> None:
    admin, _ = await env.login("admin")
    r = await admin.put(OPT, json={"half_price_check": method})
    assert r.status_code == 200 and r.json()["half_price_check"] == method


async def _setup(env, hp: DUp, method: str = "vendor_preflight", vendor: MockZovo | None = None):
    vendor = vendor or _vendor()
    await _zovo(hp, vendor, HALF)
    await _method(env, method)
    return vendor


async def _check(client, hp: DUp, session: str | None = None, variant=None):
    body = {"product_id": str(hp.product.id), "variant_id": variant}
    if session is not None:
        body["session"] = session
    return await client.post(f"{API}/half-checks", json=body)


async def _order(client, hp: DUp, session: str, check_id: str | None, confirm: bool = True):
    body = {"product_id": str(hp.product.id), "qty": 1, "session": session}
    if check_id:
        body["half_check_id"] = check_id
        body["half_check_confirm"] = confirm
    r = await client.post(f"{API}/orders", json=body)
    hp.forget_checkout_preflight()  # the account pre-check at checkout is not counted here
    return r


async def _status(env, check_id: str) -> tuple[str, str | None, bool]:
    from tests.test_procurement_vcc import _rows

    (r,) = await _rows(
        env,
        "SELECT status, reason, blob IS NOT NULL FROM half_price_checks WHERE id = :i",
        i=uuid.UUID(check_id),
    )
    return r[0], r[1], r[2]


# --------------------------------------------------------------------------- vendor preflight
async def test_required_flag_and_check_then_order_with_the_vendor_preflight(env, hp):
    vendor = await _setup(env, hp)
    buyer, _ = await env.login("buyer")
    req = (
        await buyer.get(f"{API}/half-checks/required", params={"product_id": str(hp.product.id)})
    ).json()
    assert req == {"required": True, "method": "vendor_preflight", "plan": "plus_half"}
    detail = (await buyer.get(f"{API}/products/{hp.product.id}")).json()
    assert detail["half_price"] is True
    session = gpt_session(new_marker())
    # no check, no order: the funnel refuses
    r = await _order(buyer, hp, session, None)
    assert r.status_code == 422 and r.json()["error"]["code"] == "half_check_required"
    # the check: eligible, quote shown, the sealed session is gone afterwards
    r = await _check(buyer, hp, session)
    assert r.status_code == 200, r.text
    chk = r.json()
    assert (
        chk["status"] == "passed" and chk["usable"] is True and chk["method"] == "vendor_preflight"
    )
    assert chk["account"] == "r***@example.com" and chk["quote"]["currency"] == "PHP"
    assert await _status(env, chk["id"]) == ("passed", None, False)  # blob nulled after the read
    assert vendor.preflights == 1 and vendor.direct_executions == 0
    r = await _order(buyer, hp, session, chk["id"])
    assert r.status_code == 200, r.text
    order = r.json()
    # the check is consumed: another order with it is refused
    r = await _order(buyer, hp, session, chk["id"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "half_check_invalid"
    # and the order pays and runs (the vendor enforces it once more at execution)
    await pay_order(env, buyer, order)
    await env.drain()
    await hp.run()
    assert (await _job(hp, order)).state in ("accepted_pending", "fulfilled")


async def test_ineligible_account_is_refused_before_payment(env, hp):
    vendor = _vendor()
    vendor.direct_half_eligible = False  # the vendor quotes no *_half plan to this account
    await _setup(env, hp, vendor=vendor)
    buyer, _ = await env.login("buyer")
    session = gpt_session(new_marker())
    r = await _check(buyer, hp, session)
    chk = r.json()
    # the buyer gets a coarse reason only: no account detail, no quote (no oracle); staff keep
    # the vendor's reason in the row
    assert chk["status"] == "ineligible" and chk["reason"] == "not_eligible"
    assert chk["account"] is None and chk["quote"] is None
    assert (await _status(env, chk["id"]))[:2] == ("ineligible", "plan_not_quoted")
    assert chk["usable"] is False
    r = await _order(buyer, hp, session, chk["id"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "half_check_invalid"
    assert await env.scalar("SELECT count(*) FROM orders") == 0
    # an active subscription / a quote_error / a blocking reason are refusals as well
    for over, reason in (
        ({"subscription_has_active": True}, "subscription_active"),
        ({"blocking_reason": "cooling_down"}, "preflight_blocked"),
    ):
        vendor.direct_half_eligible = True
        vendor.preflight_over = over
        b2, _ = await env.login("buyer")
        c = (await _check(b2, hp, session)).json()
        assert c["status"] == "ineligible" and c["reason"] == "not_eligible"
        assert (await _status(env, c["id"]))[1] == reason
    vendor.preflight_over = {}
    vendor.direct_quote_error = "no_promo"
    b3, _ = await env.login("buyer")
    c3 = (await _check(b3, hp, session)).json()
    assert c3["reason"] == "not_eligible" and (await _status(env, c3["id"]))[1] == "quote_error"


async def test_session_rejected_and_vendor_down_and_procurement_down(env, hp):
    await _setup(env, hp)
    buyer, _ = await env.login("buyer")
    c = (await _check(buyer, hp, gpt_session("INVALIDX"))).json()  # the mock rejects the session
    assert c["status"] == "session_invalid" and c["reason"] == "session_rejected"
    # a session that cannot work is refused at once, nothing is stored
    n = await env.scalar("SELECT count(*) FROM half_price_checks")
    r = await _check(buyer, hp, gpt_session(new_marker(), days=-2))
    assert r.status_code == 422 and r.json()["error"]["code"].startswith("session_")
    r = await _check(buyer, hp, None)
    assert r.status_code == 422 and r.json()["error"]["code"] == "session_required"
    assert await env.scalar("SELECT count(*) FROM half_price_checks") == n
    # procurement unreachable: 503 and the pending row (with its sealed session) is removed
    real = env.res.procurement
    env.res.procurement = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("x"))),
        base_url="http://procurement",
    )
    try:
        r = await _check(buyer, hp, gpt_session(new_marker()))
    finally:
        await env.res.procurement.aclose()
        env.res.procurement = real
    assert r.status_code == 503 and r.json()["error"]["code"] == "half_check_unavailable"
    assert await env.scalar("SELECT count(*) FROM half_price_checks") == n


async def test_a_product_without_a_half_plan_needs_no_check(env, hp):
    await _setup(env, hp)
    plain = await env.product(mode="upstream_direct", title="Plain", price=900, floor=100)
    async with env.owner_session() as db:
        await db.execute(
            text(
                "INSERT INTO supplier_offers (id, tenant_id, supplier_id, product_id, kind, "
                "params, priority, enabled, last_check_ok, cost_estimate, currency) "
                "SELECT gen_random_uuid(), tenant_id, supplier_id, :p, kind, "
                "jsonb_build_object('product', 'gpt', 'plan', 'plus', 'card_id', 4242), "
                "priority, enabled, last_check_ok, cost_estimate, currency "
                "FROM supplier_offers LIMIT 1"
            ),
            {"p": plain.id},
        )
        await db.commit()
    buyer, _ = await env.login("buyer")
    req = await buyer.get(f"{API}/half-checks/required", params={"product_id": str(plain.id)})
    assert req.json()["required"] is False
    r = await buyer.post(f"{API}/half-checks", json={"product_id": str(plain.id)})
    assert r.status_code == 409 and r.json()["error"]["code"] == "half_check_not_required"


async def test_a_check_belongs_to_its_buyer_product_and_tenant(env, hp):
    await _setup(env, hp)
    alice, _ = await env.login("buyer")
    mallory, _ = await env.login("buyer")
    session = gpt_session(new_marker())
    chk = (await _check(alice, hp, session)).json()
    assert chk["status"] == "passed"
    # another buyer cannot use it
    r = await _order(mallory, hp, session, chk["id"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "half_check_invalid"
    # nor a check that does not exist
    r = await _order(alice, hp, session, str(uuid.uuid4()))
    assert r.status_code == 422 and r.json()["error"]["code"] == "half_check_invalid"
    # an expired check is refused
    async with env.owner_session() as db:
        await db.execute(
            text("UPDATE half_price_checks SET created_at = now() - interval '1 hour'")
        )
        await db.commit()
    r = await _order(alice, hp, session, chk["id"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "half_check_invalid"
    # a foreign tenant's session / admin cannot see or decide it
    foreign, _ = await env.login("admin", tenant_id=env.other_tenant_id)
    assert (await foreign.get(f"{API}/admin/procurement/half-checks")).status_code in (401, 403)
    admin, _ = await env.login("admin")
    r = await admin.post(
        f"{API}/admin/procurement/half-checks/{chk['id']}/decision", json={"approve": True}
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "check_not_pending"
    r = await admin.post(
        f"{API}/admin/procurement/half-checks/{uuid.uuid4()}/decision", json={"approve": True}
    )
    assert r.status_code == 404


async def test_cart_and_quote_paths_cannot_buy_a_half_price_plan(env, hp):
    await _setup(env, hp)
    buyer, _ = await env.login("buyer")
    # the funnel refuses any caller that carries no check (cart, quotes): direct without session
    # is refused first, so use the service directly for the CDK-style path
    from app.modules.orders import halfcheck

    async with env.api_session() as db:
        from sqlalchemy import select

        from app.models import User

        u = await db.scalar(select(User).where(User.role == "buyer").limit(1))
        with pytest.raises(Exception) as exc:
            await halfcheck.claim(
                db,
                u,
                None,
                product_id=hp.product.id,
                variant_id=None,
                qty=1,
                target=halfcheck.TargetPlan("gpt", "plus_half", None, "direct"),
                account_masked=None,
            )
        assert getattr(exc.value, "code", "") == "half_check_required"
    assert buyer is not None


# --------------------------------------------------------------------------- DB guarantees
async def test_the_api_role_cannot_read_the_session_or_forge_an_outcome(env, hp):
    await _setup(env, hp, method="manual")
    buyer, user = await env.login("buyer")
    chk = (await _check(buyer, hp)).json()
    assert chk["status"] == "manual_pending"
    from sqlalchemy.exc import DBAPIError

    async with env.api_session() as db:
        with pytest.raises(DBAPIError):  # column privilege: blob / kid are not readable
            await db.execute(text("SELECT blob FROM half_price_checks"))
        await db.rollback()
    async with env.api_session() as db:
        with pytest.raises(DBAPIError):  # the trigger: pending -> passed is not the API's to do
            await db.execute(
                text("UPDATE half_price_checks SET status = 'passed' WHERE id = :i"),
                {"i": uuid.UUID(chk["id"])},
            )
        await db.rollback()
    async with env.api_session() as db:  # nor the staff columns directly (grant: order_id only)
        with pytest.raises(DBAPIError):
            await db.execute(
                text("UPDATE half_price_checks SET status = 'manual_approved' WHERE id = :i"),
                {"i": uuid.UUID(chk["id"])},
            )
        await db.rollback()
    async with env.api_session() as db:  # the staff decision goes through the DEFINER function
        got = await db.scalar(
            text("SELECT half_price_checks_decide(:i, true)"), {"i": uuid.UUID(chk["id"])}
        )
        await db.commit()
    assert got == "manual_approved"
    assert user is not None


# --------------------------------------------------------------------------- chatgpt_direct
ACCOUNT_CHECK = "/backend-api/accounts/check/v4-2023-04-27"


def _chatgpt(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _acct(plan: str, campaigns: dict | None = None) -> dict:
    return {
        "accounts": {
            "default": {
                "account": {"account_id": "acct-1", "plan_type": plan},
                "entitlement": {"subscription_plan": f"chatgpt{plan}plan"},
                "eligible_promo_campaigns": campaigns or {},
            }
        }
    }


async def test_chatgpt_direct_method_eligible_ineligible_and_refused(env, hp):
    seen: list[httpx.Request] = []
    answer: dict = {"status": 200, "json": _acct("free", {"plus": {"id": "c1", "metadata": {}}})}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(answer["status"], json=answer.get("json"))

    vendor = await _setup(env, hp, method="chatgpt_direct")
    hp.ctx.chatgpt_http = _chatgpt(handler)
    session = gpt_session(new_marker())
    buyer, _ = await env.login("buyer")
    chk = (await _check(buyer, hp, session)).json()
    assert chk["status"] == "passed" and chk["method"] == "chatgpt_direct"
    # the SAME session is not read from ChatGPT again within 6 h: the earlier outcome is returned
    again = (await _check(buyer, hp, session)).json()
    assert again["status"] == "passed" and len(seen) == 1
    # only a keyed hash of it is in Redis (never the token)
    keys = [k async for k in env.res.redis.scan_iter(match="rl:proc:halfcheck-tok:*")]
    assert len(keys) == 1 and "SMK" not in keys[0].decode() and "eyJ" not in keys[0].decode()
    assert await _status(env, chk["id"]) == ("passed", None, False)
    (req,) = seen
    # a plain read-only request: documented endpoint, bearer token, honest user agent, no more
    assert req.method == "GET" and req.url.host == "chatgpt.com" and req.url.path == ACCOUNT_CHECK
    assert req.headers["authorization"].startswith("Bearer eyJ")
    assert req.headers["user-agent"].startswith("24goods-eligibility-check")
    assert not any(h.startswith(("oai-", "sec-ch", "openai-")) for h in req.headers)
    assert vendor.preflights == 0  # the vendor was not asked
    # not eligible: a paid plan has no free-plan promotion
    answer.update(json=_acct("plus"))
    b2, _ = await env.login("buyer")
    c2 = (await _check(b2, hp, gpt_session(new_marker()))).json()
    assert c2["status"] == "ineligible" and c2["reason"] == "not_eligible"
    assert (await _status(env, c2["id"]))[1] == "not_free_account"
    # a free account with no listed promotion
    answer.update(json=_acct("free"))
    b3, _ = await env.login("buyer")
    c3 = (await _check(b3, hp, gpt_session(new_marker()))).json()
    assert (await _status(env, c3["id"]))[1] == "no_promotion"
    # ChatGPT refuses (challenge page / 403 / 429): "unavailable", never a workaround
    for status in (403, 429, 503):
        answer.update(status=status, json=None)
        b, _ = await env.login("buyer")
        c = (await _check(b, hp, gpt_session(new_marker()))).json()
        assert c["status"] == "unavailable" and c["reason"] == "check_unavailable"
        assert (await _status(env, c["id"]))[1] == "chatgpt_refused"
    answer.update(status=401)
    b, _ = await env.login("buyer")
    assert (await _check(b, hp, gpt_session(new_marker()))).json()["status"] == "session_invalid"
    answer.update(status=200, json={"nope": 1})
    b, _ = await env.login("buyer")
    cu = (await _check(b, hp, gpt_session(new_marker()))).json()
    assert (await _status(env, cu["id"]))[1] == "unreadable_answer"
    # nothing was retried: one request per distinct session
    assert len(seen) == 8


async def test_chatgpt_direct_is_rate_limited_per_buyer_and_never_proxied(env, hp):
    calls: list[int] = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json=_acct("free", {"plus": {"id": "c", "metadata": {}}}))

    await _setup(env, hp, method="chatgpt_direct")
    hp.ctx.chatgpt_http = _chatgpt(handler)
    buyer, _ = await env.login("buyer")
    statuses = [
        (await _check(buyer, hp, gpt_session(new_marker()))).json()["status"] for _ in range(4)
    ]
    assert statuses == ["passed", "passed", "passed", "unavailable"]  # 3 per buyer / 10 min
    assert len(calls) == 3


# --------------------------------------------------------------------------- manual
async def test_manual_method_holds_the_order_before_the_supplier_until_staff_approve(env, hp):
    vendor = await _setup(env, hp, method="manual")
    buyer, _ = await env.login("buyer")
    # no session is needed for the check itself
    chk = (await _check(buyer, hp)).json()
    assert chk["status"] == "manual_pending" and chk["usable"] is True
    assert await _status(env, chk["id"]) == ("manual_pending", None, False)
    session = gpt_session(new_marker())
    order = (await _order(buyer, hp, session, chk["id"])).json()
    await pay_order(env, buyer, order)
    await env.drain()
    await hp.run()
    job = await _job(hp, order)
    assert job.state == "needs_manual" and job.error_code == "half_price_check_manual"
    assert vendor.preflights == 0 and vendor.direct_executions == 0  # the supplier was NOT called
    admin, _ = await env.login("admin")
    items = (await admin.get(f"{API}/admin/procurement/half-checks")).json()["items"]
    assert [i["id"] for i in items] == [chk["id"]] and items[0]["order_id"] == order["id"]
    # staff approve, then retry the held job: it goes through
    r = await admin.post(
        f"{API}/admin/procurement/half-checks/{chk['id']}/decision", json={"approve": True}
    )
    assert r.json()["status"] == "manual_approved"
    assert (await admin.post(f"{JOBS}/{job.id}/retry", json={})).status_code == 200
    await _settle(hp, 4)
    assert vendor.direct_executions == 1
    assert (
        await env.scalar(
            "SELECT count(*) FROM audit_log "
            "WHERE action IN ('half_check.started','half_check.approved')"
        )
        == 2
    )


async def test_manual_refusal_keeps_the_supplier_away_and_the_order_refundable(env, hp):
    vendor = await _setup(env, hp, method="manual")
    buyer, _ = await env.login("buyer")
    chk = (await _check(buyer, hp)).json()
    order = (await _order(buyer, hp, gpt_session(new_marker()), chk["id"])).json()
    admin, _ = await env.login("admin")
    r = await admin.post(
        f"{API}/admin/procurement/half-checks/{chk['id']}/decision", json={"approve": False}
    )
    assert r.json()["status"] == "manual_refused"
    # payment is refused for an order whose check staff refused
    r = await buyer.post(f"{API}/orders/{order['id']}/pay", json={"channel": "zpay_alipay"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "half_check_refused"
    assert vendor.preflights == 0 and vendor.direct_executions == 0


# --------------------------------------------------------------------------- second line
async def test_vendor_enforces_half_price_at_execution_and_we_refuse_without_a_charge(env, hp):
    vendor = await _setup(env, hp, method="manual")
    buyer, _ = await env.login("buyer")
    chk = (await _check(buyer, hp)).json()
    order = (await _order(buyer, hp, gpt_session(new_marker()), chk["id"])).json()
    admin, _ = await env.login("admin")
    await admin.post(
        f"{API}/admin/procurement/half-checks/{chk['id']}/decision", json={"approve": True}
    )
    vendor.direct_half_eligible = False  # staff approved, the vendor does not quote it anyway
    await pay_order(env, buyer, order)
    await env.drain()
    await hp.run()
    job = await _job(hp, order)
    assert job.state == "needs_manual" and job.error_code == "half_price_ineligible"
    assert vendor.direct_executions == 0  # failed before any order / charge
    assert (await _refund(env, order)).status_code == 200  # the refund path stays open


# --------------------------------------------------------------------------- leaks
async def test_the_check_session_never_leaves_the_vault_strict_sweep(env, hp, caplog):
    caplog.set_level(logging.DEBUG)
    marker = new_marker()
    local = f"real.person.{marker.lower()}"
    await _setup(env, hp)
    buyer, _ = await env.login("buyer")
    session = gpt_session(marker, email=f"{local}@example.com")
    pubsub = env.s.redis.pubsub()
    await pubsub.psubscribe("rt:*")
    chk = (await _check(buyer, hp, session)).json()
    assert chk["status"] == "passed"
    # a second check through the chatgpt method, a refusal and a rejected session
    await _method(env, "chatgpt_direct")
    hp.ctx.chatgpt_http = _chatgpt(lambda req: httpx.Response(403))
    b2, _ = await env.login("buyer")
    assert (await _check(b2, hp, session)).json()["status"] == "unavailable"
    body = await _check(b2, hp, gpt_session(marker, days=-2))
    assert body.status_code == 422 and marker not in body.text
    admin, _ = await env.login("admin")
    texts = [
        json.dumps(chk),
        (await admin.get(f"{API}/admin/procurement/half-checks?status=all")).text,
        (await buyer.get(f"{API}/half-checks/required?product_id={hp.product.id}")).text,
    ]
    assert all(_leaks(t, marker, local) == [] for t in texts)
    for name, dump in (await _all_dumps(env)).items():
        assert _leaks(dump, marker, local) == [], f"table {name}"
    redis = env.s.redis
    blob = []
    async for key in redis.scan_iter(match="*", count=500):
        k = key.decode() if isinstance(key, bytes) else key
        blob.append(k)
        if await redis.type(key) in (b"string", "string"):
            blob.append(str(await redis.get(key)))
    assert _leaks("\n".join(blob), marker, local) == []
    seen = []
    while (m := await pubsub.get_message(timeout=0.3)) is not None:
        if m["type"] == "pmessage":
            seen.append(m["data"].decode())
    assert all(_leaks(x, marker, local) == [] for x in seen)
    await pubsub.aclose()
    assert _leaks(caplog.text, marker, local) == []
    assert "half-check check=" in caplog.text  # the outcome IS logged, without the session


async def test_stale_sealed_sessions_are_wiped_by_the_sweeper(env, hp):
    from app.procurement.halfcheck import wipe_stale

    await _setup(env, hp)
    buyer, _ = await env.login("buyer")
    # simulate procurement dying between the API's insert and the read: a pending row with a blob
    from app.core.credvault import CredentialSealer  # noqa: F401

    sealed = env.res.cred_sealer.seal("x" * 40, tenant_id=str(env.tenant_id), order_id="a")
    cid = uuid.uuid4()
    async with env.owner_session() as db:
        await db.execute(
            text(
                "INSERT INTO half_price_checks (id, tenant_id, user_id, product_id, method, "
                "vendor_product, plan, status, blob, kid, created_at) SELECT :i, :t, u.id, :p, "
                "'vendor_preflight', 'gpt', 'plus_half', 'pending', :b, 'k', "
                "now() - interval '1 hour' FROM users u LIMIT 1"
            ),
            {"i": cid, "t": env.tenant_id, "p": hp.product.id, "b": sealed},
        )
        await db.commit()
    assert await wipe_stale(hp.ctx) >= 1
    assert await _status(env, str(cid)) == ("unavailable", "check_expired", False)
    assert buyer is not None


# --------------------------------------------------------------------------- audit / review fixes
async def _manual_order(env, hp, buyer, confirm: bool = True):
    chk = (await _check(buyer, hp)).json()
    r = await _order(buyer, hp, gpt_session(new_marker()), chk["id"], confirm=confirm)
    return chk, r


async def test_the_check_needs_a_registered_account_and_open_manual_checks_are_capped(env, hp):
    await _setup(env, hp, method="manual")
    guest, gu = await env.login("buyer")
    async with env.owner_session() as db:
        await db.execute(text("UPDATE users SET is_instant = true WHERE id = :i"), {"i": gu.id})
        await db.commit()
    r = await _check(guest, hp)
    assert r.status_code == 403 and r.json()["error"]["code"] == "half_check_register"
    assert await env.scalar("SELECT count(*) FROM half_price_checks") == 0
    buyer, _ = await env.login("buyer")
    for _ in range(3):
        assert (await _check(buyer, hp)).status_code == 200
    r = await _check(buyer, hp)
    assert r.status_code == 409 and r.json()["error"]["code"] == "half_check_manual_limit"
    # the cap is per buyer
    other, _ = await env.login("buyer")
    assert (await _check(other, hp)).status_code == 200


async def test_required_flag_needs_an_active_product(env, hp):
    await _setup(env, hp)
    buyer, _ = await env.login("buyer")
    async with env.owner_session() as db:
        await db.execute(text("UPDATE products SET status = 'draft'"))
        await db.commit()
    r = await buyer.get(f"{API}/half-checks/required", params={"product_id": str(hp.product.id)})
    assert r.status_code == 404


async def test_order_account_must_be_the_checked_account(env, hp):
    await _setup(env, hp)
    buyer, _ = await env.login("buyer")
    chk = (await _check(buyer, hp, gpt_session(new_marker()))).json()
    other_session = gpt_session(new_marker(), email="zed@example.com")  # another account
    r = await _order(buyer, hp, other_session, chk["id"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "half_check_account_mismatch"
    assert await env.scalar("SELECT count(*) FROM orders") == 0
    assert (await _status(env, chk["id"]))[0] == "passed"  # not consumed by the refusal


async def test_manual_check_needs_the_buyers_confirmation_before_the_order(env, hp):
    await _setup(env, hp, method="manual")
    buyer, _ = await env.login("buyer")
    chk, r = await _manual_order(env, hp, buyer, confirm=False)
    assert r.status_code == 422 and r.json()["error"]["code"] == "half_check_confirm_required"
    r = await _order(buyer, hp, gpt_session(new_marker()), chk["id"], confirm=True)
    assert r.status_code == 200, r.text


async def test_one_check_covers_one_unit_only(env, hp):
    await _setup(env, hp)
    from sqlalchemy import select

    from app.models import User
    from app.modules.orders import halfcheck

    buyer, _ = await env.login("buyer")
    chk = (await _check(buyer, hp, gpt_session(new_marker()))).json()
    async with env.api_session() as db:
        u = await db.scalar(select(User).where(User.role == "buyer").limit(1))
        target = await halfcheck.requirement(db, u.tenant_id, hp.product.id, None)
        assert target is not None
        with pytest.raises(Exception) as exc:  # a CDK-style caller: no session, qty 10
            await halfcheck.claim(
                db,
                u,
                uuid.UUID(chk["id"]),
                product_id=hp.product.id,
                variant_id=None,
                qty=10,
                target=target,
                account_masked=None,
            )
        assert getattr(exc.value, "code", "") == "half_check_qty"
    # the order API refuses qty > 1 before it ever reaches the check
    body = {"product_id": str(hp.product.id), "qty": 2, "session": gpt_session(new_marker())}
    r = await buyer.post(f"{API}/orders", json={**body, "half_check_id": chk["id"]})
    assert r.status_code == 422
    assert (await _status(env, chk["id"]))[0] == "passed"


async def test_two_concurrent_orders_cannot_share_one_check(env, hp):
    import asyncio

    await _setup(env, hp)
    buyer, _ = await env.login("buyer")
    session = gpt_session(new_marker())
    chk = (await _check(buyer, hp, session)).json()
    rs = await asyncio.gather(
        _order(buyer, hp, session, chk["id"]), _order(buyer, hp, session, chk["id"])
    )
    assert sorted(r.status_code for r in rs) == [200, 422], [r.text for r in rs]
    assert await env.scalar("SELECT count(*) FROM orders") == 1
    assert (
        await env.scalar("SELECT count(*) FROM half_price_checks WHERE order_id IS NOT NULL") == 1
    )


async def test_check_expiry_boundary_is_thirty_minutes(env, hp):
    await _setup(env, hp)
    buyer, _ = await env.login("buyer")
    session = gpt_session(new_marker())
    inside = (await _check(buyer, hp, session)).json()
    outside = (await _check(buyer, hp, session)).json()
    async with env.owner_session() as db:
        for cid, secs in ((inside["id"], 29 * 60 + 30), (outside["id"], 30 * 60 + 5)):
            await db.execute(
                text(
                    "UPDATE half_price_checks SET created_at = now() - make_interval(secs => :s) "
                    "WHERE id = :i"
                ),
                {"i": uuid.UUID(cid), "s": secs},
            )
        await db.commit()
    r = await _order(buyer, hp, session, outside["id"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "half_check_invalid"
    r = await _order(buyer, hp, session, inside["id"])
    assert r.status_code == 200, r.text


async def test_staff_decision_is_once_tenant_pinned_and_the_held_job_follows_it(env, hp):
    vendor = await _setup(env, hp, method="manual")
    buyer, _ = await env.login("buyer")
    chk, r = await _manual_order(env, hp, buyer)
    order = r.json()
    await pay_order(env, buyer, order)
    await env.drain()
    await hp.run()
    job = await _job(hp, order)
    assert job.state == "needs_manual" and job.error_code == "half_price_check_manual"
    # the hold raised ONE visible warning for staff
    assert (
        await env.scalar("SELECT count(*) FROM audit_log WHERE action = 'procurement.half_hold'")
        == 1
    )
    admin, _ = await env.login("admin")
    dec = f"{API}/admin/procurement/half-checks/{chk['id']}/decision"
    items = (await admin.get(f"{API}/admin/procurement/half-checks")).json()["items"]
    assert items[0]["overdue"] is False and items[0]["waiting_minutes"] == 0
    # another tenant's pinned session cannot decide it (the function checks the pin)
    async with env.api_session(env.other_tenant_id) as db:
        got = await db.scalar(
            text("SELECT half_price_checks_decide(:i, true)"), {"i": uuid.UUID(chk["id"])}
        )
        await db.commit()
    assert got is None and (await _status(env, chk["id"]))[0] == "manual_pending"
    # past the SLA a paid, held check is flagged
    async with env.owner_session() as db:
        await db.execute(
            text("UPDATE half_price_checks SET created_at = now() - interval '2 hours'")
        )
        await db.commit()
    items = (await admin.get(f"{API}/admin/procurement/half-checks")).json()["items"]
    assert items[0]["overdue"] is True and items[0]["waiting_minutes"] >= 120
    # refuse, then retry: the job stays held with the refusal reason, nothing reaches the vendor
    assert (await admin.post(dec, json={"approve": False})).json()["status"] == "manual_refused"
    r = await admin.post(dec, json={"approve": True})  # decided once
    assert r.status_code == 409 and r.json()["error"]["code"] == "check_not_pending"
    assert (await admin.post(f"{JOBS}/{job.id}/retry", json={})).status_code == 200
    await hp.run()
    job = await _job(hp, order)
    assert job.state == "needs_manual" and job.error_code == "half_price_check_refused"
    assert vendor.direct_executions == 0


# --------------------------------------------------------------------------- source pinning
async def _clone_offer(env, offer_id, plan: str, priority: int, variant_id=None, **extra):
    """A second source of the same target with other params (bypassing the console on purpose:
    the engine must hold even when the data is wrong)."""
    import json as _json

    await _sql_exec(
        env,
        "INSERT INTO supplier_offers (id, tenant_id, supplier_id, product_id, variant_id, kind, "
        "params, cost_estimate, currency, priority, enabled, cost_known) "
        "SELECT gen_random_uuid(), tenant_id, supplier_id, product_id, :v, kind, "
        "params || CAST(:p AS jsonb), cost_estimate, currency, :pr, true, cost_known "
        "FROM supplier_offers WHERE id = :o",
        v=variant_id,
        p=_json.dumps({"plan": plan, **extra}),
        pr=priority,
        o=offer_id,
    )


async def _sql_exec(env, sql: str, **params):
    async with env.owner_session() as db:
        await db.execute(text(sql), params)
        await db.commit()


def _order_row(hp, order_id, variant_id=None):
    from app.procurement.engine import OrderRow

    return OrderRow(
        order_id, hp.env.tenant_id, "paid", "upstream_direct", 1, hp.product.id, variant_id
    )


async def test_failover_and_round_robin_never_leave_the_checked_plan(env, hp):
    from app.core.db import tenant_session
    from app.procurement import engine

    vendor = await _setup(env, hp)
    del vendor
    offer = await env.scalar("SELECT id FROM supplier_offers LIMIT 1")
    await _clone_offer(env, offer, "plus", 200)  # a FULL-price plan, lower priority
    buyer, _ = await env.login("buyer")
    session = gpt_session(new_marker())
    chk = (await _check(buyer, hp, session)).json()
    order = (await _order(buyer, hp, session, chk["id"])).json()
    row = _order_row(hp, uuid.UUID(order["id"]))
    async with tenant_session(hp.ctx.maker, hp.env.tenant_id) as db:
        got = await engine.candidates(hp.ctx, db, row)
        assert [c.params["plan"] for c in got] == ["plus_half"]
        # the first source failed: failover finds NOTHING else (never the full-price plan)
        assert await engine.pick_candidates(hp.ctx, db, row, [got[0].id]) == []
    await _sql_exec(env, "UPDATE products SET procurement_routing = 'round_robin'")
    async with tenant_session(hp.ctx.maker, hp.env.tenant_id) as db:
        for _ in range(6):
            got = await engine.candidates(hp.ctx, db, row)
            assert [c.params["plan"] for c in got] == ["plus_half"]
    # an order with NO attached check never reaches a half-price source (fail closed): only the
    # full-price source is left for it, and with nothing else left there is no candidate at all
    other = uuid.uuid4()
    async with tenant_session(hp.ctx.maker, hp.env.tenant_id) as db:
        got = await engine.candidates(hp.ctx, db, _order_row(hp, other))
        assert [c.params["plan"] for c in got] == ["plus"]
    await _sql_exec(env, "DELETE FROM supplier_offers WHERE params->>'plan' = 'plus'")
    async with tenant_session(hp.ctx.maker, hp.env.tenant_id) as db:
        assert await engine.pick_candidates(hp.ctx, db, _order_row(hp, other)) == []


async def test_variant_options_pin_sources_to_plan_and_region(env, hp):
    from app.core.db import tenant_session
    from app.procurement import engine

    await _setup(env, hp)
    admin, _ = await env.login("admin")
    v = (
        await admin.post(
            f"{API}/admin/products/{hp.product.id}/variants",
            json={
                "name_i18n": {"en": "PH"},
                "price": "20.00",
                "status": "archived",  # an active options variant needs costed sources first
                "options": {"plan": "plus", "region": "PH"},
            },
        )
    ).json()
    base = await env.scalar("SELECT id FROM supplier_offers LIMIT 1")
    await _clone_offer(env, base, "plus", 10, v["id"], payment_country="PH")
    await _clone_offer(env, base, "pro_20x", 20, v["id"], payment_country="PH")  # other plan
    await _clone_offer(env, base, "plus", 30, v["id"], payment_country="EG")  # other region
    await _clone_offer(env, base, "plus", 40, v["id"])  # no country, region is not US
    row = _order_row(hp, uuid.uuid4(), uuid.UUID(v["id"]))
    async with tenant_session(hp.ctx.maker, hp.env.tenant_id) as db:
        got = await engine.candidates(hp.ctx, db, row)
    assert [(c.params["plan"], c.params.get("payment_country")) for c in got] == [("plus", "PH")]


async def test_console_rejects_sources_that_differ_from_the_variant_options(env, hp):
    await _setup(env, hp)
    admin, _ = await env.login("admin")
    v = (
        await admin.post(
            f"{API}/admin/products/{hp.product.id}/variants",
            json={
                "name_i18n": {"en": "PH"},
                "price": "20.00",
                "status": "archived",  # an active options variant needs costed sources first
                "options": {"plan": "plus_half", "region": "PH"},
            },
        )
    ).json()
    sid = await env.scalar("SELECT id FROM suppliers LIMIT 1")
    src = f"{API}/admin/procurement/sources"
    good = {"product": "gpt", "plan": "plus_half", "card_id": 4242, "payment_country": "PH",
            "payment_currency": "PHP"}  # fmt: skip

    async def add(params, **kw):
        return await admin.post(
            src,
            json={"product_id": str(hp.product.id), "variant_id": v["id"],
                  "supplier_id": str(sid), "params": params, **kw},
        )  # fmt: skip

    for bad in ({**good, "plan": "plus"}, {**good, "payment_country": "EG"}):
        r = await add(bad)
        assert r.status_code == 422 and r.json()["error"]["code"] == "source_options_mismatch"
    r = await add(good)
    assert r.status_code == 201, r.text
    oid = r.json()["id"]
    # patch to another plan / country is refused, and so is ENABLING one that drifted
    r = await admin.patch(f"{src}/{oid}", json={"params": {**good, "plan": "plus"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "source_options_mismatch"
    await _sql_exec(
        env, "UPDATE supplier_offers SET params = params || '{\"plan\": \"plus\"}' WHERE id = :o",
        o=uuid.UUID(oid),
    )  # fmt: skip
    r = await admin.patch(f"{src}/{oid}", json={"enabled": True, "cost_estimate": "5"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "source_options_mismatch"
    # another tenant's variant is not even found
    foreign_p = await env.product(mode="upstream_direct", tenant_id=env.other_tenant_id)
    r = await admin.post(
        src,
        json={"product_id": str(foreign_p.id), "variant_id": v["id"], "supplier_id": str(sid),
              "params": good},
    )  # fmt: skip
    assert r.status_code == 404


async def test_region_withdrawn_after_payment_is_shown_to_staff(env, hp):
    import app.devtools.mock_zovocard as mz

    await _zovo(hp, _vendor(), {**HALF, "payment_country": "EG", "payment_currency": "EGP"})
    await _method(env, "manual")
    buyer, _ = await env.login("buyer")
    chk, r = await _manual_order(env, hp, buyer)
    order = r.json()
    admin, _ = await env.login("admin")
    await admin.post(
        f"{API}/admin/procurement/half-checks/{chk['id']}/decision", json={"approve": True}
    )
    await pay_order(env, buyer, order)
    await env.drain()
    old = mz.MOCK_GPT_REGIONS
    mz.MOCK_GPT_REGIONS = tuple(x for x in old if x[0] != "EG")  # the vendor withdraws EG
    try:
        await hp.run()
        await _settle(hp, 3)
    finally:
        mz.MOCK_GPT_REGIONS = old
    job = await _job(hp, order)
    assert job.state == "needs_manual", job.state
    assert job.error_code == "no_source:region_not_offered"


async def test_chatgpt_global_budget_is_per_tenant_and_signals_staff(env, hp, monkeypatch):
    import app.procurement.halfcheck as pc

    monkeypatch.setattr(pc, "GLOBAL", (2, 3600))
    calls: list[int] = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json=_acct("free", {"plus": {"id": "c", "metadata": {}}}))

    await _setup(env, hp, method="chatgpt_direct")
    hp.ctx.chatgpt_http = _chatgpt(handler)
    out = []
    for _ in range(4):  # four different buyers share the tenant's budget of 2 real reads
        b, _ = await env.login("buyer")
        out.append((await _check(b, hp, gpt_session(new_marker()))).json())
    assert [c["status"] for c in out] == ["passed", "passed", "unavailable", "unavailable"]
    assert out[2]["reason"] == "rate_limited" and len(calls) == 2
    # staff get ONE warning (not one per refused buyer) telling them to switch the method
    assert (
        await env.scalar("SELECT count(*) FROM audit_log WHERE action = 'half_check.limit_hit'")
        == 1
    )


# --------------------------------------------------------------------------- round 2 (audit)
def _session_for(marker: str, account_id: str, email: str = "same@example.com") -> str:
    """A valid session of ANOTHER ChatGPT account that shows the same masked e-mail."""
    import time

    from tests.test_procurement_direct import _b64

    doc = json.loads(gpt_session(marker, email=email))
    doc["account"]["id"] = account_id
    claims = {
        "exp": int(time.time()) + 10 * 86400,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }
    doc["accessToken"] = f"eyJhbGciOiJub25lIn0.{_b64(claims)}.sig"
    return json.dumps(doc)


async def _sources(env, hp):
    sid = await env.scalar("SELECT id FROM suppliers LIMIT 1")
    return f"{API}/admin/procurement/sources", sid


async def test_enabled_sources_of_a_target_never_mix_half_and_full_price_plans(env, hp):
    await _setup(env, hp)
    admin, _ = await env.login("admin")
    src, sid = await _sources(env, hp)
    full = {"product": "gpt", "plan": "plus", "card_id": 4242}

    def body(params, **kw):
        return {"product_id": str(hp.product.id), "supplier_id": str(sid), "params": params,
                "cost_estimate": "2.00", **kw}  # fmt: skip

    # create ENABLED beside the enabled half-price source: refused
    r = await admin.post(src, json=body(full, enabled=True))
    assert r.status_code == 422 and r.json()["error"]["code"] == "source_plan_mixed"
    # disabled is fine; ENABLING it (patch) is refused
    r = await admin.post(src, json=body(full))
    assert r.status_code == 201, r.text
    oid = r.json()["id"]
    r = await admin.patch(f"{src}/{oid}", json={"enabled": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "source_plan_mixed"
    # two half-price sources of another country mix too
    ph = {**HALF, "payment_country": "PH", "payment_currency": "PHP"}
    r = await admin.post(src, json=body(ph, enabled=True))
    assert r.status_code == 422 and r.json()["error"]["code"] == "source_plan_mixed"
    # patching a disabled source's params onto a mixed plan and enabling in one call: refused
    drift = HALF | {"payment_country": "EG", "payment_currency": "EGP"}
    r = await admin.patch(f"{src}/{oid}", json={"params": drift, "enabled": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "source_plan_mixed"
    # the half-price source itself can be re-saved alone
    half_id = await env.scalar("SELECT id FROM supplier_offers WHERE params->>'plan' = 'plus_half'")
    r = await admin.patch(f"{src}/{half_id}", json={"priority": 7})
    assert r.status_code == 200
    # another tenant's admin cannot reach these sources
    foreign, _ = await env.login("admin", tenant_id=env.other_tenant_id)
    r = await foreign.patch(f"{src}/{oid}", json={"enabled": True})
    assert r.status_code in (401, 403, 404)


async def test_required_when_any_enabled_source_is_half_price_and_the_engine_fails_closed(env, hp):
    from app.core.db import tenant_session
    from app.procurement import engine

    await _setup(env, hp)
    offer = await env.scalar("SELECT id FROM supplier_offers LIMIT 1")
    await _clone_offer(env, offer, "plus", 1)  # FULL price is the primary (first) source
    buyer, _ = await env.login("buyer")
    req = (
        await buyer.get(f"{API}/half-checks/required", params={"product_id": str(hp.product.id)})
    ).json()
    assert req["required"] is True and req["plan"] == "plus_half"
    detail = (await buyer.get(f"{API}/products/{hp.product.id}")).json()
    assert detail["half_price"] is True
    # an order without an attached check can only reach the full-price source ...
    row = _order_row(hp, uuid.uuid4())
    async with tenant_session(hp.ctx.maker, hp.env.tenant_id) as db:
        got = await engine.pick_candidates(hp.ctx, db, row)
        assert [c.params["plan"] for c in got] == ["plus"]
    # ... and with only the half-price source left there is none: the job needs staff
    await _sql_exec(env, "DELETE FROM supplier_offers WHERE params->>'plan' = 'plus'")
    async with tenant_session(hp.ctx.maker, hp.env.tenant_id) as db:
        assert await engine.pick_candidates(hp.ctx, db, row) == []


async def test_order_without_its_check_goes_to_needs_manual_and_nothing_is_sent(env, hp):
    vendor = await _setup(env, hp)
    buyer, _ = await env.login("buyer")
    session = gpt_session(new_marker())
    chk = (await _check(buyer, hp, session)).json()
    order = (await _order(buyer, hp, session, chk["id"])).json()
    await pay_order(env, buyer, order)
    await _sql_exec(env, "UPDATE half_price_checks SET order_id = NULL")  # staff-level accident
    await env.drain()
    await hp.run()
    job = await _job(hp, order)
    assert job.state == "needs_manual"
    assert vendor.direct_executions == 0


async def test_account_binding_is_keyed_and_compared_in_procurement_before_any_spend(env, hp):
    vendor = await _setup(env, hp)
    buyer, _ = await env.login("buyer")
    mine = _session_for(new_marker(), "acct-mine")
    chk = (await _check(buyer, hp, mine)).json()
    assert chk["status"] == "passed"
    stored = (await _rows(env, "SELECT account_hmac FROM half_price_checks"))[0][0]
    # a keyed digest: neither the account id nor the e-mail, and the API role cannot read it
    assert stored and len(stored) == 64 and "acct-mine" not in stored
    from sqlalchemy.exc import DBAPIError

    async with env.api_session() as db:
        with pytest.raises(DBAPIError):
            await db.execute(text("SELECT account_hmac FROM half_price_checks"))
        await db.rollback()
    # another account with the SAME masked e-mail: the API's mask hint cannot tell them apart
    other = _session_for(new_marker(), "acct-other")
    r = await _order(buyer, hp, other, chk["id"])
    assert r.status_code == 200, r.text
    order = r.json()
    await pay_order(env, buyer, order)
    await env.drain()
    await hp.run()
    job = await _job(hp, order)
    assert job.state == "needs_manual" and job.error_code == "half_check_account_mismatch"
    # nothing at execution: the binding refused before the engine's own preflight (the check's
    # read and the checkout pre-check came before the order and are not counted here)
    assert vendor.direct_executions == 0 and vendor.preflights == 0
    # the matching account goes through
    chk2 = (await _check(buyer, hp, mine)).json()
    ok = (await _order(buyer, hp, _session_for(new_marker(), "acct-mine"), chk2["id"])).json()
    await pay_order(env, buyer, ok)
    await env.drain()
    await hp.run()
    assert (await _job(hp, ok)).state in ("accepted_pending", "fulfilled")


async def test_a_forged_account_id_next_to_another_accounts_token_never_executes(env, hp):
    """Round 3: the binding reads the token's claims; the editable ``account.id`` is ignored."""
    vendor = await _setup(env, hp)
    buyer, _ = await env.login("buyer")
    chk = (await _check(buyer, hp, _session_for(new_marker(), "acct-mine"))).json()
    assert chk["status"] == "passed"
    forged = json.loads(_session_for(new_marker(), "acct-other"))
    forged["account"]["id"] = "acct-mine"  # the checked account's id, beside acct-other's token
    r = await _order(buyer, hp, json.dumps(forged), chk["id"])
    # the session validator already refuses a JSON account id that differs from the token's claim
    assert r.status_code == 422 and r.json()["error"]["code"] == "session_account_mismatch"
    assert vendor.direct_executions == 0
    # and the binding itself reads the claim, so the forged field could not match either
    from app.procurement.halfcheck import identity_of

    assert identity_of("gpt", json.dumps(forged)) == "acct:acct-other"


async def test_a_check_without_a_binding_never_executes(env, hp):
    vendor = await _setup(env, hp)
    buyer, _ = await env.login("buyer")
    session = gpt_session(new_marker())
    chk = (await _check(buyer, hp, session)).json()
    order = (await _order(buyer, hp, session, chk["id"])).json()
    await pay_order(env, buyer, order)
    await _sql_exec(env, "UPDATE half_price_checks SET account_hmac = NULL")
    await env.drain()
    await hp.run()
    job = await _job(hp, order)
    assert job.state == "needs_manual" and job.error_code == "half_check_account_mismatch"
    assert vendor.direct_executions == 0


async def test_claude_and_grok_checks_without_an_identity_are_refused_unless_manual(env, hp):
    await _zovo(hp, _vendor(), {"product": "grok", "plan": "super_half", "card_id": 4242})
    await _method(env, "vendor_preflight")
    buyer, _ = await env.login("buyer")
    r = await _check(buyer, hp, "sso=" + "A" * 40)
    assert r.status_code == 422 and r.json()["error"]["code"] == "half_check_unsupported"
    assert await env.scalar("SELECT count(*) FROM half_price_checks") == 0
    await _method(env, "manual")  # staff decide: no session, nothing to bind
    assert (await _check(buyer, hp)).status_code == 200


async def test_vendor_preflight_refusal_covers_every_ineligible_reason(env, hp):
    vendor = await _setup(env, hp, method="manual")
    buyer, _ = await env.login("buyer")
    chk = (await _check(buyer, hp)).json()
    order = (await _order(buyer, hp, gpt_session(new_marker()), chk["id"])).json()
    admin, _ = await env.login("admin")
    await admin.post(
        f"{API}/admin/procurement/half-checks/{chk['id']}/decision", json={"approve": True}
    )
    vendor.preflight_over = {"blocking_reason": "cooling_down"}  # not quote_error / not_quoted
    await pay_order(env, buyer, order)
    await env.drain()
    await hp.run()
    job = await _job(hp, order)
    assert job.state == "needs_manual" and job.error_code == "half_price_ineligible"
    assert vendor.direct_executions == 0


async def test_cdk_half_price_order_with_a_passed_check_succeeds(env):
    """A code carries no account: the order has no session, so no account comparison (it used to
    fail every CDK half-price order with ``half_check_account_mismatch``)."""

    class CUp(DUp):
        def __init__(self, e) -> None:
            super().__init__(e)
            self.mode, self.kind = "upstream_cdk", "cdk"

    up = CUp(env)
    await up.start()
    try:
        await _zovo(up, _vendor(), {"product": "gpt", "plan": "plus_half"})
        await _method(env, "vendor_preflight")
        buyer, _ = await env.login("buyer")
        chk = (await _check(buyer, up, gpt_session(new_marker()))).json()
        assert chk["status"] == "passed", chk
        r = await buyer.post(
            f"{API}/orders",
            json={"product_id": str(up.product.id), "qty": 1, "half_check_id": chk["id"],
                  "half_check_confirm": True},
        )  # fmt: skip
        assert r.status_code == 200, r.text
        assert await env.scalar("SELECT order_id IS NOT NULL FROM half_price_checks") is True
        # issue #54 F5: the cart flags a half-price CDK (buy-now only) instead of offering it,
        # so it never blocks the other lines of a cart
        q = (
            await buyer.post(
                f"{API}/cart/quote", json={"items": [{"product_id": str(up.product.id), "qty": 2}]}
            )
        ).json()
        (line,) = q["lines"]
        assert line["available"] is False and line["unavailable_reason"] == "half_check_buy_now"
        assert line["max_qty"] == 0
    finally:
        await up.close()


async def test_check_insert_and_consume_guards_for_the_api_role(env, hp):
    from sqlalchemy.exc import DBAPIError

    from tests.helpers import buy

    await _setup(env, hp, method="manual")
    alice, au = await env.login("buyer")
    bob, _ = await env.login("buyer")
    chk = (await _check(alice, hp)).json()
    manual = await env.product(mode="manual", title="Plain", price=900, floor=100)
    bobs = await buy(bob, manual, 1)

    async def run(sql: str, **kw):
        async with env.api_session() as db:
            try:
                await db.execute(text(sql), kw)
                await db.commit()
            finally:
                await db.rollback()

    ins = (
        "INSERT INTO half_price_checks (id, tenant_id, user_id, product_id, method, "
        "vendor_product, plan, status{extra}) SELECT gen_random_uuid(), tenant_id, user_id, "
        "product_id, method, vendor_product, plan, :s{vals} FROM half_price_checks"
    )
    # a decided / consumed / reasoned row cannot be INSERTED by the API role
    for extra, vals, kw in (
        ("", "", {"s": "passed"}),
        ("", "", {"s": "manual_approved"}),
        (", reason", ", 'x'", {"s": "pending"}),
        (", decided_at", ", now()", {"s": "pending"}),
        (", order_id", ", :o", {"s": "pending", "o": uuid.UUID(bobs["id"])}),
    ):
        with pytest.raises(DBAPIError):
            await run(ins.format(extra=extra, vals=vals), **kw)
    await run(ins.format(extra="", vals=""), s="manual_pending")  # opening one is allowed
    # a check is consumed only by an order of the SAME buyer
    with pytest.raises(DBAPIError):
        await run(
            "UPDATE half_price_checks SET order_id = :o WHERE id = :i",
            o=uuid.UUID(bobs["id"]), i=uuid.UUID(chk["id"]),
        )  # fmt: skip
    # cross-tenant: another tenant's pin cannot even see the row
    async with env.api_session(env.other_tenant_id) as db:
        n = await db.scalar(text("SELECT count(*) FROM half_price_checks"))
    assert n == 0
    assert au is not None


async def test_the_same_session_token_is_not_read_twice_and_a_new_account_is_refused(env, hp):
    seen: list[int] = []

    def handler(req):
        seen.append(1)
        return httpx.Response(200, json=_acct("free", {"plus": {"id": "c", "metadata": {}}}))

    await _setup(env, hp, method="chatgpt_direct")
    hp.ctx.chatgpt_http = _chatgpt(handler)
    # a buyer whose account is brand new (the real 24 h gate, no paid order): refused, coarse
    from datetime import timedelta

    from app.modules.orders import halfcheck as oh

    oh.MIN_ACCOUNT_AGE = timedelta(hours=24)
    try:
        newbie, _ = await env.login("buyer")
        r = await _check(newbie, hp, gpt_session(new_marker()))
        assert r.status_code == 403 and r.json()["error"]["code"] == "half_check_account_new"
        assert not seen and await env.scalar("SELECT count(*) FROM half_price_checks") == 0
        async with env.owner_session() as db:  # 25 hours old: allowed
            await db.execute(text("UPDATE users SET created_at = now() - interval '25 hours'"))
            await db.commit()
        session = gpt_session(new_marker())
        assert (await _check(newbie, hp, session)).json()["status"] == "passed"
        assert (await _check(newbie, hp, session)).json()["status"] == "passed"
        assert len(seen) == 1  # the second one came from the 6 h dedup
        # another tenant never sees the dedup entry of this one (keyed per tenant)
        keys = [k async for k in env.res.redis.scan_iter(match="rl:proc:halfcheck-tok:*")]
        assert len(keys) == 1 and str(hp.env.tenant_id) in keys[0].decode()
    finally:
        oh.MIN_ACCOUNT_AGE = timedelta(0)


async def test_required_endpoint_is_rate_limited(env, hp):
    await _setup(env, hp)
    buyer, _ = await env.login("buyer")
    codes = []
    for _ in range(62):
        r = await buyer.get(
            f"{API}/half-checks/required", params={"product_id": str(hp.product.id)}
        )
        codes.append(r.status_code)
    assert 429 in codes and codes[0] == 200


async def test_catalog_decorate_reads_the_half_flag_with_one_query_per_product(env, hp):
    from sqlalchemy import event

    await _setup(env, hp)
    admin, _ = await env.login("admin")
    for n in range(4):
        r = await admin.post(
            f"{API}/admin/products/{hp.product.id}/variants",
            json={"name_i18n": {"en": f"V{n}"}, "price": "20.00", "status": "archived"},
        )
        assert r.status_code == 200, r.text
    async with env.owner_session() as db:  # make them buyer-visible plain variants
        await db.execute(text("UPDATE product_variants SET status = 'active'"))
        await db.commit()
    from app.models import Product
    from app.modules.catalog import variants as cv

    statements: list[str] = []
    async with env.api_session() as db:
        engine = db.get_bind()
        sync = engine.sync_engine if hasattr(engine, "sync_engine") else engine

        def count(_c, _cur, stmt, *_a):
            if "supplier_offers.kind, supplier_offers.params" in stmt:  # the half-price flag read
                statements.append(stmt)

        event.listen(sync, "before_cursor_execute", count)
        try:
            from sqlalchemy import select

            p = await db.scalar(select(Product).where(Product.id == hp.product.id))
            out: dict = {}
            await cv.decorate(db, hp.env.tenant_id, [(p, out)], "en", None, None)
        finally:
            event.remove(sync, "before_cursor_execute", count)
    assert len(out["variants"]) == 4 and len(statements) == 1
