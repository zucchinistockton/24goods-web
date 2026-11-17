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


async def _order(client, hp: DUp, session: str, check_id: str | None, confirm: bool = True):
    body = {"product_id": str(hp.product.id), "qty": 1, "session": session}
    if check_id:
        body["half_check_id"] = check_id
        body["half_check_confirm"] = confirm
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
    assert c2["status"] == "ineligible" and c2["reason"] == "not_eligible"
    assert (await _status(env, c2["id"]))[1] == "not_free_account"
    # a free account with no listed promotion
    answer.update(json=_acct("free"))
    b3, _ = await env.login("buyer")
    c3 = (await _check(b3, hp, session)).json()
    assert (await _status(env, c3["id"]))[1] == "no_promotion"
    # ChatGPT refuses (challenge page / 403 / 429): "unavailable", never a workaround
    for status in (403, 429, 503):
        answer.update(status=status, json=None)
        b, _ = await env.login("buyer")
        c = (await _check(b, hp, session)).json()
        assert c["status"] == "unavailable" and c["reason"] == "check_unavailable"
        assert (await _status(env, c["id"]))[1] == "chatgpt_refused"
    answer.update(status=401)
    b, _ = await env.login("buyer")
    assert (await _check(b, hp, session)).json()["status"] == "session_invalid"
    answer.update(status=200, json={"nope": 1})
    b, _ = await env.login("buyer")
    cu = (await _check(b, hp, session)).json()
    assert (await _status(env, cu["id"]))[1] == "unreadable_answer"
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
    # without a check or options an order is unpinned (existing behaviour: both sources)
    other = uuid.uuid4()
    async with tenant_session(hp.ctx.maker, hp.env.tenant_id) as db:
        assert len(await engine.candidates(hp.ctx, db, _order_row(hp, other))) == 2


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
                "options": {"plan": "plus_half", "region": "PH"},
            },
        )
    ).json()
    base = await env.scalar("SELECT id FROM supplier_offers LIMIT 1")
    await _clone_offer(env, base, "plus_half", 10, v["id"], payment_country="PH")
    await _clone_offer(env, base, "plus", 20, v["id"], payment_country="PH")  # other plan
    await _clone_offer(env, base, "plus_half", 30, v["id"], payment_country="EG")  # other region
    await _clone_offer(env, base, "plus_half", 40, v["id"])  # no country, region is not US
    row = _order_row(hp, uuid.uuid4(), uuid.UUID(v["id"]))
    async with tenant_session(hp.ctx.maker, hp.env.tenant_id) as db:
        got = await engine.candidates(hp.ctx, db, row)
    assert [(c.params["plan"], c.params.get("payment_country")) for c in got] == [
        ("plus_half", "PH")
    ]


async def test_console_rejects_sources_that_differ_from_the_variant_options(env, hp):
    await _setup(env, hp)
    admin, _ = await env.login("admin")
    v = (
        await admin.post(
            f"{API}/admin/products/{hp.product.id}/variants",
            json={
                "name_i18n": {"en": "PH"},
                "price": "20.00",
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
    session = gpt_session(new_marker())
    out = []
    for _ in range(4):  # four different buyers share the tenant's budget of 2 real reads
        b, _ = await env.login("buyer")
        out.append((await _check(b, hp, session)).json())
    assert [c["status"] for c in out] == ["passed", "passed", "unavailable", "unavailable"]
    assert out[2]["reason"] == "rate_limited" and len(calls) == 2
    # staff get ONE warning (not one per refused buyer) telling them to switch the method
    assert (
        await env.scalar("SELECT count(*) FROM audit_log WHERE action = 'half_check.limit_hit'")
        == 1
    )
