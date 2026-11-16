"""review-v12 M3 part 2: ``upstream_direct`` (the supplier subscribes the BUYER's account with the
account session the buyer supplied at checkout) end to end through the real ZovoCard adapter against
``app/devtools/mock_zovocard.py`` and the engine.

Covers: the sealed buyer credential (who can read it, when it is deleted), the vendor's enum lock,
202 -> pending -> success, preflight refresh, every ambiguous point, vendor failure, refund before /
after success, cross-tenant isolation, the migration, and a strict leak sweep of the session."""

from __future__ import annotations

import base64
import json
import logging
import secrets
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.devtools.mock_zovocard import MockZovo
from app.procurement.credopen import CredentialOpener
from tests.conftest import CRED_KID, CRED_PRIV
from tests.helpers import buy, pay_order
from tests.test_aftersales import open_ticket
from tests.test_procurement_m2 import Up, _status
from tests.test_procurement_vcc import _job, _posts, _rows, _sql, _zovo

JOBS = "/api/v1/admin/procurement/jobs"
CLAUDE = {"product": "claude", "plan": "pro", "card_id": 4242}
GPT = {"product": "gpt", "plan": "plus", "card_id": 4242}


# ------------------------------------------------------------------ helpers
def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def gpt_session(marker: str, *, pad: int = 0, days: int = 10, email: str | None = None) -> str:
    """A ``chatgpt.com/api/auth/session`` document that passes the offline check."""
    now = datetime.now(timezone.utc)
    claims = {
        "exp": int(now.timestamp()) + days * 86400,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct-1"},
    }
    data = {
        "user": {"id": "user-1", "email": email or f"real.person.{marker.lower()}@example.com"},
        "account": {"id": "acct-1", "planType": "free"},
        "accessToken": f"eyJhbGciOiJub25lIn0.{_b64(claims)}.sig",
        "sessionToken": f"{marker}-session-token",
        "expires": (now + timedelta(days=max(days, 1))).isoformat(),
    }
    if pad:
        data["extra"] = "x" * pad
    return json.dumps(data)


def claude_session(marker: str) -> str:
    return f"sk-ant-sid01-{marker}-" + "A" * 40


def new_marker() -> str:
    return "SMK" + secrets.token_hex(6).upper()  # letters + digits: collides with nothing else


class DUp(Up):
    """Platform tenant with an ``upstream_direct`` product and the procurement credential key."""

    def __init__(self, env) -> None:
        super().__init__(env)
        self.mode, self.kind = "upstream_direct", "direct"

    async def start(self) -> None:
        await super().start()
        # the sweeper runs on every pass in tests; the credential PRIVATE key lives only here
        self.ctx.settings = replace(self.ctx.settings, cred_sweep_interval_s=0.0)
        self.ctx.cred_opener = CredentialOpener({CRED_KID: CRED_PRIV})
        # the console's calls into procurement (hand-fulfil guard) go to the real internal app
        import httpx

        from app.procurement.internal import create_procurement_app

        self._old = (self.env.res.settings, self.env.res.procurement)
        self.env.res.settings = self._old[0].model_copy(
            update={"procurement_token": self.ctx.settings.procurement_token}
        )
        self.env.res.procurement = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_procurement_app(self.ctx)),
            base_url="http://procurement",
        )

    async def close(self) -> None:
        await self.env.res.procurement.aclose()
        self.env.res.settings, self.env.res.procurement = self._old
        await super().close()

    async def supplier(self, supplier, **kw):
        sid, oid = await super().supplier(supplier, **kw)
        # a realistic budget: the preflight quote (USD 20 for Claude Pro) must fit it
        async with self.env.owner_session() as db:
            await db.execute(
                text("UPDATE supplier_offers SET cost_estimate = 25 WHERE id = :o"), {"o": oid}
            )
            await db.commit()
        return sid, oid

    async def paid_direct(self, session: str, *, product=None):
        buyer, _user = await self.env.login("buyer")
        order = await buy(buyer, product or self.product, 1, session=session)
        await pay_order(self.env, buyer, order)
        assert await self.env.drain() >= 1  # delivery: order.paid -> procure.requested
        return buyer, order


@pytest.fixture
async def dup(env):
    u = DUp(env)
    await u.start()
    yield u
    await u.close()


def _vendor() -> MockZovo:
    v = MockZovo(scenarios=True)  # scenarios: a 5000 USD wallet, so no balance breaker
    v.add_funding_card(4242)  # the funding card of the offers below (ours, ACTIVE)
    return v


async def _drive(env, up: DUp, order, buyer, *, want: str = "delivered", rounds: int = 10) -> None:
    for _ in range(rounds):
        await up.run()
        await env.drain()
        if await _status(buyer, order) == want:
            return
        await up.make_due()
    raise AssertionError(f"order stuck in {await _status(buyer, order)}")


async def _settle(up: DUp, rounds: int = 6) -> None:
    for _ in range(rounds):
        await up.make_due()
        await up.run()


async def _creds(env, order=None) -> int:
    sql = "SELECT count(*) FROM procurement_credentials"
    if order is None:
        return await env.scalar(sql)
    return await env.scalar(sql + " WHERE order_id = :o", o=uuid.UUID(order["id"]))


async def _refund(env, order):
    admin, _ = await env.login("admin")
    return await admin.post(
        f"/api/v1/admin/orders/{order['id']}/refund",
        json={"reference": "REF-" + uuid.uuid4().hex[:8].upper(), "acknowledge_irreversible": True},
    )


async def _refund_delivered(env, buyer, order):
    """A delivered order is refunded through the after-sales flow."""
    t = await open_ticket(buyer, order)
    assert t.status_code in (200, 201), t.text
    admin, _ = await env.login("admin")
    r = await admin.post(
        f"/api/v1/admin/after-sales/{t.json()['id']}/action",
        json={"action": "refund", "note": "refunded", "reference": "REF-" + uuid.uuid4().hex[:8],
              "acknowledge_irreversible": True},
    )  # fmt: skip
    assert r.status_code == 200, r.text
    return admin


def _order_of(vendor: MockZovo) -> dict:
    (o,) = vendor.direct_orders.values()
    return o


# ------------------------------------------------------------------ happy path
async def test_direct_order_202_pending_then_success_and_the_session_is_gone(env, dup):
    marker = new_marker()
    vendor = _vendor()
    await _zovo(dup, vendor, CLAUDE)
    detail = (await env.client().get(f"/api/v1/products/{dup.product.id}")).json()
    assert detail["delivery_mode"] == "upstream_direct" and detail["stock"] == 5
    buyer, order = await dup.paid_direct(claude_session(marker))
    assert order["buyer_input"] is None  # the session is NOT in the plaintext buyer_input
    assert await _creds(env, order) == 1  # sealed at checkout
    await dup.run()
    job = await _job(dup, order)
    # 202 -> accepted_pending: the order is only queued at the vendor
    assert job.state == "accepted_pending" and job.step == "pending" and job.maybe_sent
    assert (
        job.upstream_ref == str(_order_of(vendor)["id"]) and job.account_mask == "b***@example.test"
    )
    assert job.preflight_expires_at is not None
    assert vendor.direct_executions == 1 and vendor.preflights == 1
    assert vendor.direct_product_seen == ["claude"]  # the enum literal, sent explicitly
    assert await _status(buyer, order) == "paid"  # a 202 is never "delivered"
    await _drive(env, dup, order, buyer)
    job = await _job(dup, order)
    assert job.state == "fulfilled" and job.upstream_status == "completed"
    assert job.idempotency_key == f"pj:{job.id.hex}:submit" and len(job.card_stock_ids) == 1
    assert vendor.direct_executions == 1  # one order, never re-posted
    assert len(set(_posts(vendor, "/gpt-direct/orders"))) == 1
    # the buyer sees a short non-secret text: plan, masked account, supplier order
    cards = (await buyer.post(f"/api/v1/orders/{order['id']}/reveal")).json()["cards"]
    assert [c["code"] for c in cards] == [f"claude/pro----b***@example.test----{job.upstream_ref}"]
    assert await _creds(env, order) == 0  # deleted at the terminal state
    states = await env.scalar(
        "SELECT array_agg(after->>'state' ORDER BY id) FROM audit_log "
        "WHERE action = 'procurement.job_state'"
    )
    assert states == ["submitting", "accepted_pending", "fulfilled"]
    # the vendor never got a wrong amount from us: the price is the vendor's own preflight/plan
    assert dup.ctx.suppliers  # (the adapter was built through the factory)


async def test_gpt_session_of_realistic_size_is_accepted_and_delivered(env, dup):
    marker = new_marker()
    vendor = _vendor()
    await _zovo(dup, vendor, GPT)
    session = gpt_session(marker, pad=37_000)  # a real session text is about 37 KB
    assert len(session) > 36_000
    buyer, order = await dup.paid_direct(session)
    await _drive(env, dup, order, buyer)
    job = await _job(dup, order)
    assert job.state == "fulfilled" and vendor.direct_product_seen == ["gpt"]
    cards = (await buyer.post(f"/api/v1/orders/{order['id']}/reveal")).json()["cards"]
    assert cards[0]["code"] == f"gpt/plus----b***@example.test----{job.upstream_ref}"
    # the vendor received the session once per call and our database never held it
    assert vendor.direct_orders[int(job.upstream_ref)]["cred"]  # a digest only
    assert await _creds(env) == 0


# ------------------------------------------------------------------ enum lock
async def test_enum_lock_refuses_any_product_outside_the_vendor_list(env, dup):
    from app.suppliers.base import OfferSpec
    from app.suppliers.zovocard.direct import direct_spec

    for bad in ("cluade", "x", "GPT", "", None, "gpt ", 1):
        params = {**CLAUDE, "product": bad}
        assert direct_spec(OfferSpec(kind="direct", params=params)) is None, bad
    no_product = {"plan": "pro", "card_id": 4242}  # a MISSING product is not a silent "gpt"
    assert direct_spec(OfferSpec(kind="direct", params=no_product)) is None
    for plan in ("pro_20x_renew", "credit250", "UPPER", "a b"):
        assert direct_spec(OfferSpec(kind="direct", params={**GPT, "plan": plan})) is None
    assert direct_spec(OfferSpec(kind="direct", params={**CLAUDE, "payment_country": "PH"})) is None
    assert direct_spec(OfferSpec(kind="direct", params={**GPT, "card_id": 0})) is None
    ok = direct_spec(OfferSpec(kind="direct", params=CLAUDE))
    assert ok and (ok.country, ok.currency) == ("US", "USD")  # Claude / Grok are pinned US / USD
    # the console refuses it as well, server side
    admin, _ = await env.login("admin")
    sid, _oid = await _zovo(dup, _vendor(), CLAUDE)
    for params in ({**CLAUDE, "product": "cluade"}, {"plan": "pro", "card_id": 1}):
        r = await admin.post(
            "/api/v1/admin/procurement/sources",
            json={"product_id": str(dup.product.id), "supplier_id": str(sid), "params": params,
                  "cost_estimate": "1", "enabled": False},
        )  # fmt: skip
        assert r.status_code in (409, 422), (params, r.text)
    r = await admin.post(
        "/api/v1/admin/procurement/sources",
        json={"product_id": str(dup.product.id), "supplier_id": str(sid),
              "params": {"product": "grok", "plan": "premium_monthly", "card_id": 1},
              "cost_estimate": "1", "enabled": False},
    )  # fmt: skip
    assert r.status_code == 422 and r.json()["error"]["code"] == "mixed_products"


async def test_availability_reads_the_closed_catalog_and_names_a_usd_cost(env):
    from tests.test_procurement_vcc_audit import _adapter

    vendor = _vendor()
    sup = _adapter(vendor)
    from app.suppliers.base import OfferSpec

    av = await sup.availability(OfferSpec(kind="direct", params=CLAUDE))
    # 2000 USD cents + 5% buffer (100) + 15 fee = 21.15
    assert av.available and str(av.cost_estimate.amount) == "21.15"
    off = await sup.availability(OfferSpec(kind="direct", params={**CLAUDE, "plan": "nope"}))
    assert not off.available and off.reason == "not_purchasable"
    off = await sup.availability(OfferSpec(kind="direct", params={**CLAUDE, "product": "claud"}))
    assert not off.available and off.reason == "invalid_offer"


# ------------------------------------------------------------------ preflight refresh
async def test_stale_preflight_token_is_refreshed_under_the_same_key(env, dup):
    vendor = _vendor()
    vendor.direct_expire_next = 1  # the first token is already expired when the order arrives
    await _zovo(dup, vendor, CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    job = await _job(dup, order)
    assert job.state == "fulfilled"
    assert vendor.preflights == 2  # re-quoted once, inline
    keys = _posts(vendor, "/gpt-direct/orders")
    assert len(keys) == 2 and len(set(keys)) == 1  # the refused POST and the retry: ONE key
    assert vendor.direct_executions == 1  # and exactly one order


async def test_plan_already_active_is_stopped_before_any_order(env, dup):
    vendor = _vendor()
    vendor.direct_current_plan = "pro"  # the account already is on the plan being sold
    await _zovo(dup, vendor, CLAUDE)
    _buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "plan_already_active"
    assert vendor.direct_executions == 0 and _posts(vendor, "/gpt-direct/orders") == []
    assert job.account_mask == "b***@example.test"  # the masked account is still recorded
    assert not job.maybe_sent  # nothing reached the order endpoint


# ------------------------------------------------------------------ ambiguity
async def test_timeout_after_submit_is_found_by_request_id_and_never_reposted(env, dup):
    vendor = _vendor()
    vendor.force("/gpt-direct/orders", "lost_response")  # executed, cached, the answer is lost
    await _zovo(dup, vendor, CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    job = await _job(dup, order)
    assert job.state == "fulfilled" and vendor.direct_executions == 1
    assert len(_posts(vendor, "/gpt-direct/orders")) == 1  # found by READS: no second POST
    # the reconcile went through the order list (our client_request_id), then the order read
    assert any(p == "/gpt-direct/orders" and m == "GET" for m, p, _ in vendor.requests)


async def test_a_post_the_vendor_never_ran_is_resent_under_the_same_key(env, dup):
    vendor = _vendor()
    vendor.force("/gpt-direct/orders", "500")  # a write answered 500: it may have run (it did not)
    await _zovo(dup, vendor, CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    job = await _job(dup, order)
    assert job.state == "fulfilled" and vendor.direct_executions == 1
    keys = _posts(vendor, "/gpt-direct/orders")
    assert len(keys) == 2 and len(set(keys)) == 1 and keys[0] == job.idempotency_key
    assert vendor.preflights == 2  # each POST attempt gets a fresh single-use token


async def test_pending_forever_is_bounded_and_ends_in_needs_manual(env, dup):
    vendor = _vendor()
    vendor.direct_end = "running"  # the order never reaches a terminal state
    await _zovo(dup, vendor, CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    for _ in range(3):
        await _settle(dup, 1)
    job = await _job(dup, order)
    assert job.state == "accepted_pending" and vendor.direct_executions == 1
    await _sql(env, "UPDATE procurement_jobs SET first_sent_at = now() - interval '3 hours'")
    await _settle(dup, 2)
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "pending_too_long"
    assert job.maybe_sent and job.upstream_ref  # still flagged: the supplier holds an order
    assert vendor.direct_executions == 1 and len(_posts(vendor, "/gpt-direct/orders")) == 1
    await env.drain()
    assert await _status(buyer, order) == "manual_review"
    # staff can ask again: the engine only READS the known order, it never creates another
    admin, _ = await env.login("admin")
    view = (await admin.get(f"{JOBS}/{job.id}")).json()
    assert view["account_mask"] == "b***@example.test" and view["plan"] == "claude/pro"
    assert {"requery", "retry", "fulfil", "fail"} <= set(view["actions"])
    r = await admin.post(f"{JOBS}/{job.id}/retry", json={})
    assert r.status_code == 200, r.text
    await _settle(dup, 3)
    assert vendor.direct_executions == 1 and len(_posts(vendor, "/gpt-direct/orders")) == 1


async def test_a_declined_order_holds_the_order_and_is_never_recreated(env, dup):
    vendor = _vendor()
    vendor.direct_end = "declined"
    await _zovo(dup, vendor, CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    for _ in range(4):
        await _settle(dup, 1)
        await env.drain()
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "order_declined"
    assert job.upstream_ref and job.maybe_sent and vendor.direct_executions == 1
    assert await _status(buyer, order) == "manual_review"
    admin, _ = await env.login("admin")
    r = await admin.post(f"{JOBS}/{job.id}/retry", json={})
    assert r.status_code == 200
    await _settle(dup, 4)
    job = await _job(dup, order)
    assert job.state == "needs_manual" and vendor.direct_executions == 1  # still ONE order
    assert len(_posts(vendor, "/gpt-direct/orders")) == 1
    # staff end it; the credential goes at the terminal state
    r = await admin.post(f"{JOBS}/{job.id}/fail", json={"reason": "vendor declined the payment"})
    assert r.status_code == 200
    await dup.run()
    assert await _creds(env, order) == 0


async def test_session_the_vendor_rejects_is_fatal_without_an_order(env, dup):
    vendor = _vendor()
    await _zovo(dup, vendor, CLAUDE)
    _buyer, order = await dup.paid_direct(claude_session("INVALID" + new_marker()))
    await dup.run()
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "GPT_SESSION_INVALID"
    assert vendor.direct_executions == 0 and not job.maybe_sent and job.upstream_ref is None


# ------------------------------------------------------------------ refund / void
async def test_refund_before_success_cancels_the_vendor_order(env, dup):
    vendor = _vendor()
    await _zovo(dup, vendor, CLAUDE)
    _buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()  # the order is queued at the vendor
    assert (await _job(dup, order)).state == "accepted_pending"
    r = await _refund(env, order)
    assert r.status_code == 200, r.text
    await _settle(dup, 5)
    job = await _job(dup, order)
    assert job.state == "voided" and job.upstream_status == "cancelled"
    assert vendor.direct_cancelled == 1 and _order_of(vendor)["status"] == "cancelled"
    assert await _creds(env, order) == 0


async def test_cancel_too_late_is_a_void_failure_for_staff(env, dup):
    vendor = _vendor()
    vendor.direct_cancel_409 = True  # the payment started: cancel answers 409
    await _zovo(dup, vendor, CLAUDE)
    _buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    await _refund(env, order)
    await _settle(dup, 5)
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "void_failed"
    assert job.maybe_sent and job.upstream_ref  # flagged: the supplier may charge for it
    n = await env.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'procurement.void_failed' "
        "AND severity = 'critical'"
    )
    assert n == 1


async def test_refund_after_success_is_a_staff_decision_with_a_critical_audit(env, dup):
    vendor = _vendor()
    await _zovo(dup, vendor, CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    admin = await _refund_delivered(env, buyer, order)
    await _settle(dup, 3)
    job = await _job(dup, order)
    # the subscription cannot be undone through the API: nothing was cancelled or voided
    assert job.state == "fulfilled" and job.error_code == "refunded_after_success"
    assert vendor.direct_cancelled == 0 and vendor.direct_executions == 1
    row = (
        await _rows(
            env,
            "SELECT severity, detail->>'order_id' FROM audit_log "
            "WHERE action = 'procurement.direct_refunded_after_success'",
        )
    )[0]
    assert row[0] == "critical" and row[1] == order["id"]
    # staff may still try the void: the supplier says the order is completed -> void_failed alert
    r = await admin.post(f"{JOBS}/{job.id}/void", json={"reason": "try to recover it"})
    assert r.status_code == 200, r.text
    await _settle(dup, 4)
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "void_failed"
    assert vendor.direct_cancelled == 0  # a completed order is never cancelled


# ------------------------------------------------------------------ the sealed credential
async def test_api_role_cannot_read_the_sealed_session_back(env, dup):
    from sqlalchemy.exc import DBAPIError

    await _zovo(dup, _vendor(), CLAUDE)
    marker = new_marker()
    _buyer, order = await dup.paid_direct(claude_session(marker))
    assert await _creds(env, order) == 1
    for sql in (
        "SELECT blob FROM procurement_credentials",
        "SELECT * FROM procurement_credentials",
        "SELECT kid FROM procurement_credentials",
        "SELECT length(blob) FROM procurement_credentials",
        "UPDATE procurement_credentials SET email_masked = 'x'",
    ):
        async with env.api_session() as db:
            with pytest.raises(DBAPIError) as exc:
                await db.execute(text(sql))
            assert "permission denied" in str(exc.value).lower(), sql
    # DELETE is a backstop only (migration 0030): a fresh row survives it, one past 6 h does not
    async with env.api_session() as db:
        gone = (await db.execute(text("DELETE FROM procurement_credentials"))).rowcount
        await db.commit()
    assert gone == 0 and await _creds(env, order) == 1
    await _sql(env, "UPDATE procurement_credentials SET created_at = now() - interval '7 hours'")
    async with env.api_session() as db:
        gone = (await db.execute(text("DELETE FROM procurement_credentials"))).rowcount
        await db.commit()
    assert gone == 1 and await _creds(env, order) == 0
    _b2, order = await dup.paid_direct(claude_session(marker))  # a fresh one for the rest
    async with env.api_session() as db:  # the non-secret columns ARE readable (the buyer's view)
        rows = (
            await db.execute(
                text("SELECT order_id, product, email_masked FROM procurement_credentials")
            )
        ).all()
    assert [(str(r[0]), r[1]) for r in rows] == [(order["id"], "claude")]
    # the delivery role has no grant at all
    async with env.s.delivery_engine.connect() as conn:
        with pytest.raises(DBAPIError):
            await conn.execute(text("SELECT order_id FROM procurement_credentials"))
    # procurement can read and delete, but never write
    from app.core.db import tenant_session

    async with tenant_session(dup.ctx.maker, env.tenant_id) as db:
        assert (
            await db.execute(text("SELECT count(*) FROM procurement_credentials"))
        ).scalar() == 1
        for sql in (
            "UPDATE procurement_credentials SET email_masked = 'x'",
            "INSERT INTO procurement_credentials (id, tenant_id, order_id, product, kid, blob) "
            "VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), 'gpt', 'k', 'x')",
        ):
            with pytest.raises(DBAPIError):
                async with db.begin_nested():
                    await db.execute(text(sql))


async def test_the_blob_is_bound_to_its_order_and_to_the_credential_domain(env, dup):
    from app.core.cardvault import CardContext, CardOpener
    from tests.conftest import VAULT_KID, VAULT_PRIV

    await _zovo(dup, _vendor(), CLAUDE)
    marker = new_marker()
    _buyer, order = await dup.paid_direct(claude_session(marker))
    (blob,) = [bytes(r[0]) for r in await _rows(env, "SELECT blob FROM procurement_credentials")]
    assert marker.encode() not in blob  # sealed, not merely encoded
    opener = CredentialOpener({CRED_KID: CRED_PRIV})
    ok = opener.open(blob, tenant_id=str(env.tenant_id), order_id=order["id"])
    assert marker in ok
    from app.core.credvault import VaultError

    with pytest.raises(VaultError):  # another order, another tenant: no
        opener.open(blob, tenant_id=str(env.tenant_id), order_id=str(uuid.uuid4()))
    with pytest.raises(VaultError):
        opener.open(blob, tenant_id=str(uuid.uuid4()), order_id=order["id"])
    # the card vault's private key (delivery) cannot open it: a different keypair
    with pytest.raises(Exception):  # noqa: B017
        CardOpener({CRED_KID: VAULT_PRIV}).open(
            blob, CardContext(str(env.tenant_id), "buyer-credential", order["id"])
        )
    assert VAULT_KID != CRED_KID


async def test_credential_is_deleted_after_its_maximum_age_and_for_a_closed_order(env, dup):
    from app.procurement.engine import sweep_credentials

    vendor = _vendor()
    await _zovo(dup, vendor, CLAUDE)
    _buyer, order = await dup.paid_direct(claude_session(new_marker()))
    assert await _creds(env) == 1
    assert order
    await sweep_credentials(dup.ctx, force=True)
    assert await _creds(env) == 1  # a live order keeps it
    await _sql(env, "UPDATE procurement_credentials SET created_at = now() - interval '7 hours'")
    assert await sweep_credentials(dup.ctx, force=True) == 1  # older than cred_max_age_s
    assert await _creds(env) == 0
    # an unpaid order that is cancelled: the credential goes with the order
    buyer2, _ = await env.login("buyer")
    o2 = await buy(buyer2, dup.product, 1, session=claude_session(new_marker()))
    assert await _creds(env, o2) == 1
    r = await buyer2.post(f"/api/v1/orders/{o2['id']}/cancel", json={})
    assert r.status_code == 200, r.text
    await sweep_credentials(dup.ctx, force=True)
    assert await _creds(env, o2) == 0


async def test_missing_session_waits_for_the_buyer_and_resumes_after_resupply(env, dup):
    vendor = _vendor()
    await _zovo(dup, vendor, CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _sql(env, "DELETE FROM procurement_credentials")  # expired / swept before it was used
    await dup.run()
    job = await _job(dup, order)
    assert job.state == "needs_manual" and job.error_code == "credential_missing"
    assert not job.maybe_sent and vendor.preflights == 0 and vendor.direct_executions == 0
    await env.drain()
    assert await _status(buyer, order) == "manual_review"
    # the buyer supplies a new session (owner only), staff retry
    other, _ = await env.login("buyer")
    r = await other.post(
        f"/api/v1/orders/{order['id']}/direct-session",
        json={"session": claude_session(new_marker())},
    )
    assert r.status_code == 404
    fresh = claude_session(new_marker())
    r = await buyer.post(f"/api/v1/orders/{order['id']}/direct-session", json={"session": fresh})
    assert r.status_code == 200, r.text
    r2 = await buyer.post(f"/api/v1/orders/{order['id']}/direct-session", json={"session": fresh})
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "session_exists"
    admin, _ = await env.login("admin")
    assert (await admin.post(f"{JOBS}/{job.id}/retry", json={})).status_code == 200
    await _drive(env, dup, order, buyer)
    assert (await _job(dup, order)).state == "fulfilled" and await _creds(env, order) == 0


# ------------------------------------------------------------------ checkout
async def test_checkout_validates_the_session_before_any_order_exists(env, dup):
    await _zovo(dup, _vendor(), CLAUDE)
    buyer, _ = await env.login("buyer")

    async def attempt(**body):
        return await buyer.post("/api/v1/orders", json={"product_id": str(dup.product.id), **body})

    cases = [
        ({"qty": 1}, "session_required"),
        ({"qty": 1, "session": "   "}, "session_required"),
        ({"qty": 2, "session": claude_session("A")}, "direct_qty_one"),
        ({"qty": 1, "session": "not a claude session"}, "session_invalid_format"),
        ({"qty": 1, "session": "sk-ant-x\x00" + "y" * 40}, "session_invalid_format"),
        ({"qty": 1, "session": "x" * 69_000}, "session_too_large"),
        ({"qty": 1, "session": "x" * 71_000}, "session_too_large"),
        ({"qty": 1, "buyer_input": "sk-ant-sid01-" + "z" * 40}, "session_required"),
    ]
    for body, code in cases:
        r = await attempt(**body)
        assert r.status_code == 422 and r.json()["error"]["code"] == code, (code, r.text[:200])
        assert "sk-ant" not in r.text  # never echoed
    assert await env.scalar("SELECT count(*) FROM orders") == 0
    assert await _creds(env) == 0
    # the cart and the chat quote cannot carry a session: buy-now only
    r = await buyer.post(
        "/api/v1/checkout", json={"items": [{"product_id": str(dup.product.id), "qty": 1}]}
    )
    assert r.status_code in (409, 422), r.text
    assert r.json()["error"]["code"] == "direct_buy_now_only"
    # a good one
    r = await attempt(qty=1, session=claude_session(new_marker()))
    assert r.status_code == 200 and r.json()["qty"] == 1
    assert await _creds(env) == 1


async def test_gpt_session_problems_have_specific_codes(env, dup):
    await _zovo(dup, _vendor(), GPT)
    buyer, _ = await env.login("buyer")
    marker = new_marker()
    token_only = "eyJhbGciOiJub25lIn0." + "q" * 60 + ".sig"
    expired = gpt_session(marker, days=-3)
    cases = [
        (token_only, "session_access_token_only"),
        ("hello world, no json here", "session_not_json"),
        (expired, "session_expired"),
        (json.dumps({"accessToken": "x", "sessionToken": ""}), "session_missing_session_token"),
    ]
    for session, code in cases:
        r = await buyer.post(
            "/api/v1/orders", json={"product_id": str(dup.product.id), "qty": 1, "session": session}
        )
        assert r.status_code == 422 and r.json()["error"]["code"] == code, (code, r.text[:200])
    assert await env.scalar("SELECT count(*) FROM orders") == 0


async def test_no_key_configured_means_the_product_cannot_be_bought(env, dup):
    await _zovo(dup, _vendor(), CLAUDE)
    buyer, _ = await env.login("buyer")
    saved = env.res.cred_sealer
    env.res.cred_sealer = None
    try:
        r = await buyer.post(
            "/api/v1/orders",
            json={"product_id": str(dup.product.id), "qty": 1, "session": claude_session("A")},
        )
    finally:
        env.res.cred_sealer = saved
    assert r.status_code == 503 and r.json()["error"]["code"] == "direct_unavailable"
    assert await env.scalar("SELECT count(*) FROM orders") == 0


async def test_buyer_sees_the_masked_account_and_plan_only(env, dup):
    vendor = _vendor()
    await _zovo(dup, vendor, GPT)
    marker = new_marker()
    buyer, order = await dup.paid_direct(gpt_session(marker))
    r = await buyer.get(f"/api/v1/orders/{order['id']}/direct")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["account"] == "r***@example.com" and body["plan"] == "gpt/plus"
    await dup.run()
    body = (await buyer.get(f"/api/v1/orders/{order['id']}/direct")).json()
    assert body["account"] == "b***@example.test" and body["state"] == "accepted_pending"
    assert marker not in r.text and "real.person" not in r.text
    other, _ = await env.login("buyer")
    assert (await other.get(f"/api/v1/orders/{order['id']}/direct")).status_code == 404
    plain = await env.product(mode="manual")
    o2 = await buy(buyer, plain, 1)
    assert (await buyer.get(f"/api/v1/orders/{o2['id']}/direct")).status_code == 404


# ------------------------------------------------------------------ staff
async def test_hand_fulfil_is_admin_only_needs_a_reason_and_leaves_a_critical_row(env, dup):
    vendor = _vendor()
    vendor.direct_end = "running"
    await _zovo(dup, vendor, CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    job = await _job(dup, order)
    agent, _ = await env.login("agent")
    r = await agent.post(f"{JOBS}/{job.id}/fulfil", json={"reason": "I did it at the supplier"})
    assert r.status_code == 403
    admin, _ = await env.login("admin")
    # M4: the supplier's order is still moving: a hand fulfil would subscribe the account twice
    r = await admin.post(f"{JOBS}/{job.id}/fulfil", json={"reason": "subscribed by hand, sure"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "order_in_flight"
    vendor.direct_end = "declined"  # the vendor gave up on it: now staff may take over
    for _ in range(3):
        await _settle(dup, 1)
    job = await _job(dup, order)
    assert job.state == "needs_manual"
    for body, code in (({}, "reason_required"), ({"reason": "short"}, "reason_required"),
                       ({"code": "AAAA-BBBB", "reason": "x" * 20}, "direct_no_code")):  # fmt: skip
        r = await admin.post(f"{JOBS}/{job.id}/fulfil", json=body)
        assert r.status_code == 422 and r.json()["error"]["code"] == code, r.text
    r = await admin.post(
        f"{JOBS}/{job.id}/fulfil", json={"reason": "subscribed by hand at the supplier"}
    )
    assert r.status_code == 200, r.text
    await env.drain()
    assert await _status(buyer, order) == "delivered"
    cards = (await buyer.post(f"/api/v1/orders/{order['id']}/reveal")).json()["cards"]
    assert cards[0]["code"] == f"claude/pro----b***@example.test----{job.upstream_ref}"
    row = (
        await _rows(
            env,
            "SELECT severity, actor_role FROM audit_log "
            "WHERE action = 'procurement.direct_manual_fulfil'",
        )
    )[0]
    assert tuple(row) == ("critical", "admin")
    await dup.run()
    assert await _creds(env, order) == 0  # terminal: the sweeper took the session


async def test_balance_breaker_reserves_the_in_flight_direct_order(env, dup):
    vendor = MockZovo()  # 12.34 USD: less than one Claude Pro order (21.15) while it is in flight
    vendor.add_funding_card(4242)
    vendor.direct_end = "running"
    sid, _ = await _zovo(dup, vendor, CLAUDE)
    _buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    row = (
        await _rows(env, "SELECT breaker_open, breaker_kind FROM suppliers WHERE id = :s", s=sid)
    )[0]
    assert tuple(row) == (True, "balance")  # the pending order is reserved against the balance
    assert (await _job(dup, order)).state == "accepted_pending"  # and it is still polled


# ------------------------------------------------------------------ tenants
async def test_cross_tenant_direct_job_and_credential_are_invisible(env, dup):
    from tests.test_procurement_jobs import _foreign_job

    vendor = _vendor()
    await _zovo(dup, vendor, CLAUDE)
    _buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    job = await _job(dup, order)
    fj = await _foreign_job(env)
    await _sql(env, "UPDATE procurement_jobs SET kind = 'direct', account_mask = 'f***@other.test'"
                    " WHERE id = :j", j=fj)  # fmt: skip
    admin, _ = await env.login("admin")
    listing = (await admin.get(JOBS)).json()
    assert listing["total"] == 1 and "f***@other.test" not in str(listing)
    detail = (await admin.get(f"{JOBS}/{job.id}")).json()
    assert detail["account_mask"] == "b***@example.test" and detail["kind"] == "direct"
    assert (await admin.get(f"{JOBS}/{fj}")).status_code == 404
    for act, body in (("retry", {}), ("requery", {}), ("fulfil", {"reason": "x" * 20}),
                      ("fail", {"reason": "abc"}), ("void", {"reason": "abc"})):  # fmt: skip
        assert (await admin.post(f"{JOBS}/{fj}/{act}", json=body)).status_code == 404, act
    # row level security on the credential table: a foreign tenant's row (planted by the owner)
    # is invisible to this tenant's pinned sessions, and ours to theirs
    other = env.other_tenant_id
    async with env.owner_session(other) as db:
        foreign_order = (
            await db.execute(text("SELECT order_id FROM procurement_jobs WHERE id = :j"), {"j": fj})
        ).scalar_one()
        await db.execute(
            text(
                "INSERT INTO procurement_credentials (id, tenant_id, order_id, product, kid, blob)"
                " VALUES (gen_random_uuid(), :t, :o, 'gpt', 'k', decode(repeat('ab', 80), 'hex'))"
            ),
            {"t": other, "o": foreign_order},
        )
        await db.commit()
    from app.core.db import tenant_session

    async with tenant_session(dup.ctx.maker, env.tenant_id) as db:
        mine = (await db.execute(text("SELECT order_id FROM procurement_credentials"))).all()
    assert [str(r[0]) for r in mine] == [order["id"]]
    async with env.api_session() as db:
        n = (await db.execute(text("SELECT count(*) FROM procurement_credentials"))).scalar_one()
    assert n == 1
    async with tenant_session(dup.ctx.maker, other) as db:
        theirs = (await db.execute(text("SELECT order_id FROM procurement_credentials"))).all()
    assert [str(r[0]) for r in theirs] == [str(foreign_order)]
    # and a buyer of another tenant cannot supply / read a session for this order
    stranger, _ = await env.login("buyer")
    assert (await stranger.get(f"/api/v1/orders/{order['id']}/direct")).status_code == 404


# ------------------------------------------------------------------ the strict leak sweep
def _leaks(text_: str, marker: str, email_local: str) -> list[str]:
    found = []
    if marker in text_:
        found.append("session")
    if email_local in text_:
        found.append("email")
    if "sessionToken" in text_ or "sk-ant-sid01" in text_:
        found.append("shape")
    return found


async def _all_dumps(env) -> dict[str, str]:
    names = [
        r[0]
        for r in await _rows(
            env,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'",
        )
    ]
    out: dict[str, str] = {}
    for name in names:
        sql = f"SELECT coalesce(string_agg(row_to_json(t)::text, '|'), '') FROM \"{name}\" t"  # noqa: S608
        out[name] = await env.scalar(sql)
    return out


async def test_buyer_session_never_leaves_the_vault_strict_sweep(env, dup, caplog):
    """The session reaches the vendor and nowhere else: every public table (row_to_json), admin
    and buyer HTTP bodies, Redis keys and values, pub/sub, DEBUG logs, across two failure paths
    (an ambiguous POST, a vendor decline) and a success with a refund."""
    caplog.set_level(logging.DEBUG)
    marker = new_marker()
    local = f"real.person.{marker.lower()}"
    vendor = _vendor()
    vendor.force("/gpt-direct/orders", "lost_response")  # failure path 1: ambiguous after submit
    sid, _ = await _zovo(dup, vendor, GPT)
    buyer, user = await env.login("buyer")
    session = gpt_session(marker, email=f"{local}@example.com")
    order = await buy(buyer, dup.product, 1, session=session)
    pubsub = env.s.redis.pubsub()
    await pubsub.psubscribe("rt:*")
    await pay_order(env, buyer, order)
    await env.drain()
    await _drive(env, dup, order, buyer)
    assert (await _job(dup, order)).state == "fulfilled"
    # failure path 2: a second order the vendor declines
    vendor.direct_end = "declined"
    buyer2, _ = await env.login("buyer")
    o2 = await buy(
        buyer2, dup.product, 1, session=gpt_session(marker, email=f"{local}@example.com")
    )
    await pay_order(env, buyer2, o2)
    await env.drain()
    for _ in range(4):
        await _settle(dup, 1)
        await env.drain()
    states = {j.state for j in await dup.jobs()}
    assert states == {"fulfilled", "needs_manual"}
    # failure path 3: a rejected session at checkout
    bad = await buyer.post(
        "/api/v1/orders",
        json={"product_id": str(dup.product.id), "qty": 1, "session": gpt_session(marker, days=-2)},
    )
    assert bad.status_code == 422 and marker not in bad.text
    # the HTTP surface
    admin, _ = await env.login("admin")
    bodies: list[str] = []
    jobs = await dup.jobs()
    urls = [
        JOBS, f"/api/v1/admin/procurement/sources?product_id={dup.product.id}",
        f"/api/v1/admin/orders/{order['id']}", f"/api/v1/admin/orders/{o2['id']}",
        f"/api/v1/orders/{order['id']}", f"/api/v1/orders/{o2['id']}",
        f"/api/v1/orders/{order['id']}/direct",
        *[f"{JOBS}/{j.id}" for j in jobs],
    ]  # fmt: skip
    for url in urls:
        bodies.append((await admin.get(url)).text)
    bodies.append((await buyer.post(f"/api/v1/orders/{order['id']}/reveal")).text)
    for i, body in enumerate(bodies):
        assert _leaks(body, marker, local) == [], f"http response {i}"
    # a refund of the success (critical audit row, no secrets in it either)
    await _refund_delivered(env, buyer, order)
    await _settle(dup, 2)
    # every public table, row by row
    for name, dump in (await _all_dumps(env)).items():
        assert _leaks(dump, marker, local) == [], f"table {name}"
    # Redis: every key and every value
    redis = env.s.redis
    blob = []
    async for key in redis.scan_iter(match="*", count=500):
        k = key.decode() if isinstance(key, bytes) else key
        blob.append(k)
        t = (
            (await redis.type(key)).decode()
            if isinstance(await redis.type(key), bytes)
            else await redis.type(key)
        )
        if t == "string":
            blob.append(str(await redis.get(key)))
        elif t == "hash":
            blob.append(str(await redis.hgetall(key)))
        elif t == "list":
            blob.append(str(await redis.lrange(key, 0, -1)))
        elif t == "set":
            blob.append(str(await redis.smembers(key)))
        elif t == "zset":
            blob.append(str(await redis.zrange(key, 0, -1)))
    assert _leaks("\n".join(blob), marker, local) == []
    # pub/sub: nothing published carries it
    seen: list[str] = []
    while (m := await pubsub.get_message(timeout=0.3)) is not None:
        if m["type"] == "pmessage":
            seen.append(m["data"].decode())
    assert all(_leaks(x, marker, local) == [] for x in seen)
    await pubsub.aclose()
    # DEBUG logs of the API, the engine and the supplier client, both failure paths included
    assert _leaks(caplog.text, marker, local) == []
    # the vendor, on the other hand, did receive it (digest only: the mock keeps no plaintext)
    assert vendor.direct_orders and all(o["cred"] for o in vendor.direct_orders.values())
    assert user is not None and sid is not None


# ------------------------------------------------------------------ units
def test_credential_sealer_roundtrip_and_generated_keypair(tmp_path):
    import json as _json

    from app.core.credvault import CredentialSealer
    from app.devtools.keygen import generate, generate_cred
    from app.procurement.credopen import load_credential_opener

    kid, paths = generate_cred(tmp_path / "only")
    pub = _json.loads(paths["cred_public"].read_text())
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

    sealer = CredentialSealer(
        {k: X25519PublicKey.from_public_bytes(base64.b64decode(v)) for k, v in pub.items()}, kid
    )
    blob = sealer.seal("sk-ant-secret", tenant_id="t", order_id="o")
    opener = load_credential_opener(str(paths["cred_private"]))
    assert opener.open(blob, tenant_id="t", order_id="o") == "sk-ant-secret"
    assert oct(paths["cred_private"].stat().st_mode & 0o777) == "0o600"
    assert load_credential_opener("") is None
    # the full keygen writes the pair next to (and different from) the card vault's
    full = generate(tmp_path / "full")
    assert full["cred_public"].read_text() != full["public"].read_text()
    assert "APP_CRED_ACTIVE_KID=" in full["env"].read_text()
    with pytest.raises(ValueError):
        sealer.seal("", tenant_id="t", order_id="o")
    with pytest.raises(ValueError):
        sealer.seal("x" * (64 * 1024 + 1), tenant_id="t", order_id="o")


def test_procurement_never_accepts_the_card_vault_private_key():
    from app.procurement.config import ProcurementSettings

    with pytest.raises(ValueError):
        ProcurementSettings.from_env({"APP_VAULT_PRIVATE_KEYS_FILE": "/x"})
    ok = ProcurementSettings.from_env({"APP_CRED_PRIVATE_KEYS_FILE": "/run/secrets/c"})
    assert ok.cred_private_keys_file == "/run/secrets/c" and ok.cred_max_age_s == 18000.0


def test_credential_checks_by_product():
    from app.suppliers.direct_credential import CredentialError, validate_credential

    m = new_marker()
    assert validate_credential("claude", claude_session(m)).email_masked is None
    assert validate_credential("grok", "eyJ" + "a" * 60).product == "grok"
    assert validate_credential("grok", "foo=1; sso=" + "b" * 40).product == "grok"
    ok = validate_credential("gpt", gpt_session(m))
    assert ok.email_masked == "r***@example.com" and m in ok.canonical
    assert m not in repr(ok)  # the credential is repr-hidden
    for product, bad, code in (
        ("claude", "sk-ant-short", "invalid_format"),
        ("grok", "plain text cookie value here", "invalid_format"),
        ("gpt", "x" * 70_000, "too_large"),
        ("claude", "a", "too_short"),
        ("nope", "whatever-long-enough", "unsupported_product"),
    ):
        with pytest.raises(CredentialError) as exc:
            validate_credential(product, bad)
        assert exc.value.code == code


def test_jobspec_and_result_hide_the_credential():
    from app.suppliers.base import JobSpec, OfferSpec, Result

    spec = JobSpec(
        job_id="ab" * 16,
        step="sent",
        idempotency_key="pj:" + "ab" * 16 + ":submit",
        offer=OfferSpec(kind="direct", params=CLAUDE),
        credential="sk-ant-SECRETVALUE",
    )
    assert "SECRETVALUE" not in repr(spec) and "SECRETVALUE" not in str(spec)
    assert "SECRETVALUE" not in repr(
        Result(
            outcome=__import__("app.suppliers.base").suppliers.base.Outcome.OK,
            secrets=("SECRETVALUE",),
        )
    )


async def test_production_supplier_never_sees_a_session_without_the_write_gate():
    """The preflight carries the buyer's session: it obeys the production-write gate."""
    import httpx

    from app.devtools.mock_zovocard import OK_KEY, create_mock_zovocard
    from app.suppliers.base import JobSpec, OfferSpec
    from app.suppliers.zovocard import TokenBucket, ZovoCardSupplier, ZovoClient, ZovoConfig

    vendor = _vendor()
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_mock_zovocard(vendor)))
    sup = ZovoCardSupplier(
        "zovocard",
        ZovoClient(
            ZovoConfig(base_url="http://m/openapi/v1", app_secret=OK_KEY, env="production"),
            http=http,
            bucket=TokenBucket(6000, 100),
        ),
    )
    jid = "cd" * 16
    spec = JobSpec(
        job_id=jid, step="sent", idempotency_key=f"pj:{jid}:submit",
        offer=OfferSpec(kind="direct", params=CLAUDE), credential=claude_session("A"),
    )  # fmt: skip
    res = await sup.submit(spec)
    assert res.outcome.value == "unavailable" and res.error_code == "production_writes_disabled"
    assert not any(m == "POST" for m, _p, _h in vendor.requests)  # nothing left the process
    await http.aclose()


async def test_migration_0029_up_and_down(services):
    import asyncio
    import os

    import asyncpg
    from alembic import command
    from alembic.config import Config

    from tests.conftest import ALEMBIC_INI, OWNER_PW, _with_db

    db_name = "g24_mig0029"
    dsn = os.environ["TEST_DATABASE_URL"]
    plain = dsn.replace("postgresql+asyncpg://", "postgresql://")
    admin = await asyncpg.connect(plain, timeout=5)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {db_name} OWNER app_owner")
    finally:
        await admin.close()
    owner_url = _with_db(dsn, "app_owner", OWNER_PW, db_name)

    async def alembic(target: str, down: bool = False) -> None:
        prev = os.environ.get("APP_MIGRATE_DATABASE_URL")
        os.environ["APP_MIGRATE_DATABASE_URL"] = owner_url
        try:
            fn = command.downgrade if down else command.upgrade
            await asyncio.to_thread(fn, Config(str(ALEMBIC_INI)), target)
        finally:
            if prev is None:
                os.environ.pop("APP_MIGRATE_DATABASE_URL", None)
            else:
                os.environ["APP_MIGRATE_DATABASE_URL"] = prev

    async def q(sql: str, *args):
        conn = await asyncpg.connect(owner_url.replace("+asyncpg", ""), timeout=5)
        try:
            return await conn.fetchval(sql, *args)
        finally:
            await conn.close()

    try:
        await alembic("head")
        assert await q("SELECT to_regclass('procurement_credentials') IS NOT NULL")
        assert await q(
            "SELECT relforcerowsecurity FROM pg_class WHERE relname = 'procurement_credentials'"
        )
        assert await q(
            "SELECT count(*) FROM pg_policies WHERE tablename = 'procurement_credentials' "
            "AND policyname = 'tenant_select'"
        ) == 1  # fmt: skip
        for col, want in (("blob", False), ("kid", False), ("email_masked", True),
                          ("order_id", True)):  # fmt: skip
            got = await q(
                "SELECT has_column_privilege('app_api', 'procurement_credentials', $1, 'SELECT')",
                col,
            )
            assert got is want, col
        assert await q("SELECT has_table_privilege('app_api', 'procurement_credentials', 'INSERT')")
        assert not await q(
            "SELECT has_table_privilege('app_api', 'procurement_credentials', 'UPDATE')"
        )
        assert await q(  # the age-bounded backstop of 0030
            "SELECT has_table_privilege('app_api', 'procurement_credentials', 'DELETE')"
        )
        assert await q(
            "SELECT count(*) FROM pg_policies WHERE tablename = 'procurement_credentials' "
            "AND policyname = 'api_expired_delete'"
        ) == 1  # fmt: skip
        for priv in ("INSERT", "UPDATE"):
            assert not await q(
                "SELECT has_table_privilege('app_procurement', 'procurement_credentials', $1)",
                priv,
            )
        for priv in ("SELECT", "DELETE"):
            assert await q(
                "SELECT has_table_privilege('app_procurement', 'procurement_credentials', $1)",
                priv,
            )
        assert not await q(
            "SELECT has_table_privilege('app_delivery', 'procurement_credentials', 'SELECT')"
        )
        assert "upstream_direct" in await q(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'orders_delivery_mode_check'"
        )
        await alembic("0029_procurement_direct", down=True)
        assert not await q(  # 0030 undone: the API can no longer delete at all
            "SELECT has_table_privilege('app_api', 'procurement_credentials', 'DELETE')"
        )
        await alembic("0028_procurement_vcc", down=True)
        assert await q("SELECT to_regclass('procurement_credentials') IS NULL")
        assert "upstream_direct" not in await q(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'products_delivery_mode_check'"
        )
        assert not await q(
            "SELECT count(*) > 0 FROM information_schema.columns "
            "WHERE table_name = 'procurement_jobs' AND column_name = 'account_mask'"
        )
        await alembic("head")
        assert await q("SELECT to_regclass('procurement_credentials') IS NOT NULL")
    finally:
        admin = await asyncpg.connect(plain, timeout=5)
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")
        finally:
            await admin.close()


_ = time  # (kept for clarity: no test depends on wall-clock dates)


async def test_admin_can_create_a_direct_product_with_a_forced_separator(env):
    admin, _ = await env.login("admin")
    r = await admin.post(
        "/api/v1/admin/products",
        json={
            "title_i18n": {"en": "Claude Pro", "zh-CN": "Claude Pro"},
            "price": "25.00",
            "floor_price": "0",
            "currency": "CNY",
            "delivery_mode": "upstream_direct",
            "category": "gift_cards",
            "status": "active",
            "delivery_separator": "|",
        },
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body["delivery_mode"] == "upstream_direct" and body["delivery_separator"] == "----"
    assert body["delivery_format"] == "Plan----Account----Supplier order"
