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
from tests.test_procurement_vcc import _zovo

API = "/api/v1"
HALF = {"product": "gpt", "plan": "plus_half", "card_id": 4242}
OPT = f"{API}/admin/procurement/options"


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


async def _order(client, hp: DUp, session: str, check_id: str | None):
    body = {"product_id": str(hp.product.id), "qty": 1, "session": session}
    if check_id:
        body["half_check_id"] = check_id
    return await client.post(f"{API}/orders", json=body)


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
    assert chk["account"] == "b***@example.test" and chk["quote"]["currency"] == "PHP"
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
    assert chk["status"] == "ineligible" and chk["reason"] == "plan_not_quoted"
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
        assert c["status"] == "ineligible" and c["reason"] == reason
    vendor.preflight_over = {}
    vendor.direct_quote_error = "no_promo"
    b3, _ = await env.login("buyer")
    assert (await _check(b3, hp, session)).json()["reason"] == "quote_error"


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
            await halfcheck.claim(db, u, None, product_id=hp.product.id, variant_id=None)
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
    async with env.api_session() as db:  # the staff decision it IS allowed to make
        await db.execute(
            text("UPDATE half_price_checks SET status = 'manual_approved' WHERE id = :i"),
            {"i": uuid.UUID(chk["id"])},
        )
        await db.commit()
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
    c2 = (await _check(b2, hp, session)).json()
    assert c2["status"] == "ineligible" and c2["reason"] == "not_free_account"
    # a free account with no listed promotion
    answer.update(json=_acct("free"))
    b3, _ = await env.login("buyer")
    assert (await _check(b3, hp, session)).json()["reason"] == "no_promotion"
    # ChatGPT refuses (challenge page / 403 / 429): "unavailable", never a workaround
    for status in (403, 429, 503):
        answer.update(status=status, json=None)
        b, _ = await env.login("buyer")
        c = (await _check(b, hp, session)).json()
        assert c["status"] == "unavailable" and c["reason"] == "chatgpt_refused"
    answer.update(status=401)
    b, _ = await env.login("buyer")
    assert (await _check(b, hp, session)).json()["status"] == "session_invalid"
    answer.update(status=200, json={"nope": 1})
    b, _ = await env.login("buyer")
    assert (await _check(b, hp, session)).json()["reason"] == "unreadable_answer"
    # nothing was retried: one request per check
    assert len(seen) == 8


async def test_chatgpt_direct_is_rate_limited_per_buyer_and_never_proxied(env, hp):
    calls: list[int] = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json=_acct("free", {"plus": {"id": "c", "metadata": {}}}))

    await _setup(env, hp, method="chatgpt_direct")
    hp.ctx.chatgpt_http = _chatgpt(handler)
    buyer, _ = await env.login("buyer")
    session = gpt_session(new_marker())
    statuses = [(await _check(buyer, hp, session)).json()["status"] for _ in range(4)]
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
