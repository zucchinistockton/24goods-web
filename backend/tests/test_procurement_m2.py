"""review-v12 M2: upstream_cdk end to end (delivery -> procurement -> supplier -> delivery)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import asyncpg
import httpx
import pytest
from sqlalchemy import select, text

from app.devtools.mock_zovocard import OK_KEY, MockZovo, create_mock_zovocard
from app.suppliers.base import JobSpec, Money, Outcome, Result
from app.suppliers.mock import MockSupplier
from tests.conftest import (
    ALEMBIC_INI,
    FP_KEY,
    OWNER_PW,
    VAULT_KID,
    VAULT_PUB_JSON,
    _with_db,
)
from tests.helpers import buy, pay_order

WEBHOOK_SECRET = "whsec_test_" + "a" * 32


class LosingMock(MockSupplier):
    """Charges, then 'loses' the answer ``lose`` times (timeout after send)."""

    def __init__(self, *a, lose: int = 1, pending: bool = False, **kw):
        super().__init__(*a, **kw)
        self.lose = lose
        self.pending = pending

    async def submit(self, job: JobSpec) -> Result:
        res = await super().submit(job)
        if res.outcome is Outcome.OK and self.pending:
            self.pending = False
            return Result(Outcome.ACCEPTED_PENDING, upstream_ref=res.upstream_ref)
        if res.outcome is Outcome.OK and self.lose > 0:
            self.lose -= 1
            return Result(Outcome.AMBIGUOUS, error_code="timeout_after_send")
        return res


class Up:
    """Platform tenant: an upstream_cdk product, mock suppliers + offers, a procurement ctx."""

    def __init__(self, env) -> None:
        self.env = env
        self.mocks: dict[uuid.UUID, object] = {}
        self.engine = None
        self.ctx = None
        self.product = None
        self.vendor_http: httpx.AsyncClient | None = None

    async def start(self) -> None:
        from app.core.db import make_engine, make_sessionmaker
        from app.procurement.config import ProcurementSettings
        from app.procurement.context import ProcurementContext
        from app.procurement.keys import build_sealer

        self.product = await self.env.product(mode="upstream_cdk", title="GPT Plus CDK", price=5000)
        self.engine = make_engine(self.env.s.procurement_url, pool_size=3, max_overflow=2)
        self.ctx = ProcurementContext(
            ProcurementSettings(env="test", procurement_token="t" * 40),
            make_sessionmaker(self.engine),
            self.env.s.redis,
            environ={},
            sealer=build_sealer(
                public_keys_json=VAULT_PUB_JSON, active_kid=VAULT_KID, fp_key=FP_KEY
            ),
            supplier_factory=lambda ref: self.mocks[ref.id],
        )

    async def supplier(
        self,
        supplier,
        *,
        code: str = "mock_a",
        priority: int = 10,
        adapter: str = "mock",
        tenant_id: uuid.UUID | None = None,
        params: dict | None = None,
        product=None,
    ) -> tuple[uuid.UUID, uuid.UUID]:
        from app.models import SupplierAccount, SupplierOffer

        tid = tenant_id or self.env.tenant_id
        sid, oid = uuid.uuid4(), uuid.uuid4()
        async with self.env.owner_session(tid) as db:
            db.add(
                SupplierAccount(
                    id=sid, tenant_id=tid, code=code, adapter=adapter, label=code,
                    env="sandbox", enabled=True, priority=priority, settings={},
                )
            )  # fmt: skip
            await db.flush()
            db.add(
                SupplierOffer(
                    id=oid, tenant_id=tid, supplier_id=sid,
                    product_id=(product or self.product).id, variant_id=None, kind="cdk",
                    params=params or {"plan": "plus"}, priority=priority, enabled=True,
                    last_check_ok=True,
                )
            )  # fmt: skip
            await db.commit()
        self.mocks[sid] = supplier
        return sid, oid

    async def close(self) -> None:
        if self.vendor_http is not None:
            await self.vendor_http.aclose()
        await self.engine.dispose()

    async def run(self) -> int:
        from app.procurement.engine import drain

        return await drain(self.ctx)

    async def make_due(self) -> None:
        async with self.env.owner_session() as db:
            await db.execute(
                text(
                    "UPDATE procurement_jobs SET next_poll_at = now() - interval '1 second' "
                    "WHERE state IN ('queued', 'submitting', 'accepted_pending')"
                )
            )
            await db.commit()

    async def jobs(self, order_id: str | None = None):
        from app.models import ProcurementJob

        async with self.env.owner_session() as db:
            stmt = select(ProcurementJob).order_by(ProcurementJob.created_at)
            if order_id:
                stmt = stmt.where(ProcurementJob.order_id == uuid.UUID(order_id))
            return list(await db.scalars(stmt))

    async def paid_order(self, qty: int = 1):
        buyer, _user = await self.env.login("buyer")
        order = await buy(buyer, self.product, qty)
        await pay_order(self.env, buyer, order)
        assert await self.env.drain() >= 1  # delivery: order.paid -> procure.requested
        return buyer, order


@pytest.fixture
async def up(env):
    u = Up(env)
    await u.start()
    yield u
    await u.close()


async def _status(buyer, order) -> str:
    return (await buyer.get(f"/api/v1/orders/{order['id']}")).json()["status"]


# ------------------------------------------------------------------ happy path


async def test_upstream_cdk_end_to_end(env, up):
    mock = MockSupplier("mock_a")
    await up.supplier(mock)
    detail = (await env.client().get(f"/api/v1/products/{up.product.id}")).json()
    assert detail["delivery_mode"] == "upstream_cdk" and detail["stock"] == 5
    buyer, order = await up.paid_order()
    assert await _status(buyer, order) == "paid"
    assert await up.run() >= 2  # job created, then submitted
    (job,) = await up.jobs(order["id"])
    assert job.state == "fulfilled" and len(job.card_stock_ids) == 1
    assert job.idempotency_key == f"pj:{job.id.hex}:submit"
    assert await env.drain() >= 1  # delivery: procure.fulfilled -> delivered
    assert await _status(buyer, order) == "delivered"
    cards = (await buyer.post(f"/api/v1/orders/{order['id']}/reveal")).json()["cards"]
    (code,) = next(iter(mock.charged.values())).secrets
    assert [c["code"] for c in cards] == [code]
    # the plaintext code is nowhere in job rows, audit rows or outbox payloads
    for sql in (
        "SELECT count(*) FROM procurement_jobs WHERE row_to_json(procurement_jobs)::text LIKE :c",
        "SELECT count(*) FROM audit_log WHERE row_to_json(audit_log)::text LIKE :c",
        "SELECT count(*) FROM outbox WHERE payload::text LIKE :c",
    ):
        assert await env.scalar(sql, c=f"%{code}%") == 0
    states = await env.scalar(
        "SELECT array_agg(after->>'state' ORDER BY id) FROM audit_log "
        "WHERE action = 'procurement.job_state'"
    )
    assert states == ["submitting", "fulfilled"]
    assert mock.spendable == Decimal("99.00")


async def test_upstream_order_never_claims_local_pool(env, up):
    """Even with available pool cards on the same product, an upstream order is filled only by
    procurement; concurrent delivery and engine passes do not change that."""
    mock = MockSupplier("mock_a")
    await up.supplier(mock)
    # put cards in the pool of this product (imported while it was auto_card)
    async with env.owner_session() as db:
        await db.execute(
            text("UPDATE products SET delivery_mode = 'auto_card' WHERE id = :p"),
            {"p": up.product.id},
        )
        await db.commit()
    up.product.delivery_mode = "auto_card"  # the helper checks the object it is given
    await env.import_codes(up.product, ["POOL-1", "POOL-2", "POOL-3"])
    up.product.delivery_mode = "upstream_cdk"
    async with env.owner_session() as db:
        await db.execute(
            text("UPDATE products SET delivery_mode = 'upstream_cdk' WHERE id = :p"),
            {"p": up.product.id},
        )
        await db.commit()
    buyers = []
    for _ in range(3):
        b, o = await up.paid_order()
        buyers.append((b, o))
    for _ in range(4):
        await asyncio.gather(up.run(), env.drain())
    for b, o in buyers:
        assert await _status(b, o) == "delivered"
        cards = (await b.post(f"/api/v1/orders/{o['id']}/reveal")).json()["cards"]
        assert not cards[0]["code"].startswith("POOL-")
    pool = await env.scalar(
        "SELECT count(*) FROM card_stock WHERE product_id = :p AND status = 'available'",
        p=up.product.id,
    )
    assert pool == 3 and len(mock.charged) == 3


# ------------------------------------------------------------------ outcomes


async def test_timeout_after_send_reconciles_with_the_same_key(env, up):
    mock = LosingMock("mock_a", lose=1)
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()
    (job,) = await up.jobs(order["id"])
    assert job.state == "submitting" and job.step == "reconcile"
    key = job.idempotency_key
    await up.make_due()
    await up.run()
    (job,) = await up.jobs(order["id"])
    assert job.state == "fulfilled" and job.idempotency_key == key
    assert [op for op, _ in mock.calls] == ["submit", "query"]
    assert len(mock.charged) == 1 and mock.spendable == Decimal("99.00")  # charged once


async def test_pending_is_polled_never_resubmitted(env, up):
    mock = LosingMock("mock_a", lose=0, pending=True)
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()
    (job,) = await up.jobs(order["id"])
    assert job.state == "accepted_pending"
    async with env.owner_session() as db:
        wait = await db.scalar(
            text("SELECT extract(epoch FROM next_poll_at - now()) FROM procurement_jobs")
        )
    assert 5 < float(wait) <= 16  # first poll after ~15 s (spec §6)
    await up.make_due()
    await up.run()
    (job,) = await up.jobs(order["id"])
    assert job.state == "fulfilled"
    assert [op for op, _ in mock.calls] == ["submit", "query"]


async def test_unavailable_queries_then_fails_over(env, up):
    a = MockSupplier("mock_a", outcomes=[Outcome.UNAVAILABLE])
    b = MockSupplier("mock_b")
    _, offer_a = await up.supplier(a, code="mock_a", priority=10)
    await up.supplier(b, code="mock_b", priority=20)
    _buyer, order = await up.paid_order()
    await up.drain_all() if hasattr(up, "drain_all") else None
    for _ in range(3):
        await up.run()
    (job,) = await up.jobs(order["id"])
    assert job.state == "fulfilled"
    assert job.tried_offer_ids == [offer_a]
    assert job.idempotency_key == f"pj:{job.id.hex}:s2"
    assert [op for op, _ in a.calls] == ["submit", "query"]  # query BEFORE failover
    assert not a.charged and len(b.charged) == 1


async def test_fatal_goes_to_manual_review(env, up):
    mock = MockSupplier("mock_a", outcomes=[Outcome.FATAL])
    await up.supplier(mock)
    buyer, order = await up.paid_order()
    await up.run()
    (job,) = await up.jobs(order["id"])
    assert job.state == "needs_manual" and job.error_code == "mock_fatal"
    assert await env.drain() >= 1  # delivery: procure.failed
    assert await _status(buyer, order) == "manual_review"
    assert await env.scalar("SELECT count(*) FROM card_stock WHERE order_id = :o",
                            o=uuid.UUID(order["id"])) == 0  # fmt: skip


async def test_supplier_stop_opens_breaker_and_hides_stock(env, up):
    class Stopping(MockSupplier):
        async def submit(self, job):
            self.calls.append(("submit", job.idempotency_key))
            return Result(Outcome.UNAVAILABLE, error_code="insufficient_balance",
                          supplier_stop=True)  # fmt: skip

        async def query(self, job):
            self.calls.append(("query", job.idempotency_key))
            return Result(Outcome.UNAVAILABLE, error_code="not_found")

    sid, _ = await up.supplier(Stopping("mock_a"))
    _buyer, order = await up.paid_order()
    for _ in range(3):
        await up.run()
    assert await env.scalar("SELECT breaker_open FROM suppliers WHERE id = :s", s=sid) is True
    (job,) = await up.jobs(order["id"])
    assert job.state == "needs_manual" and job.error_code.startswith("no_source")
    detail = (await env.client().get(f"/api/v1/products/{up.product.id}")).json()
    assert detail["stock"] == 0
    other, _ = await env.login("buyer")
    r = await other.post("/api/v1/orders", json={"product_id": str(up.product.id), "qty": 1})
    assert r.status_code == 409  # out of stock while the breaker is open


async def test_no_offer_means_out_of_stock_and_qty_cap(env, up):
    buyer, _ = await env.login("buyer")
    r = await buyer.post("/api/v1/orders", json={"product_id": str(up.product.id), "qty": 1})
    assert r.status_code == 409
    await up.supplier(MockSupplier("mock_a"))
    r = await buyer.post("/api/v1/orders", json={"product_id": str(up.product.id), "qty": 6})
    assert r.status_code == 422


# -------------------------------------------------------------- ZovoCard adapter through the engine


async def _zovo(up, quirk: str | None = None) -> MockZovo:
    from app.suppliers.zovocard import TokenBucket, ZovoCardSupplier, ZovoClient, ZovoConfig

    vendor = MockZovo()
    up.vendor_http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_mock_zovocard(vendor))
    )
    client = ZovoClient(
        ZovoConfig(base_url="http://mockzovo/openapi/v1", app_secret=OK_KEY, env="sandbox"),
        http=up.vendor_http,
        bucket=TokenBucket(6000, 1000),
    )
    await up.supplier(ZovoCardSupplier("zovocard", client), code="zovocard", adapter="zovocard",
                      params={"plan": "plus", "payment_country": "PH"})  # fmt: skip
    if quirk:
        vendor.force("/gpt-direct/cdks", quirk)
    return vendor


@pytest.mark.parametrize("quirk", ["409", "lost_response", "500"])
async def test_zovocard_ambiguous_issue_is_replayed_once(env, up, quirk):
    vendor = await _zovo(up, quirk)
    buyer, order = await up.paid_order()
    await up.run()
    (job,) = await up.jobs(order["id"])
    assert job.state == "submitting" and job.step == "reconcile"
    await up.make_due()
    await up.run()
    (job,) = await up.jobs(order["id"])
    assert job.state == "fulfilled" and job.cost == Decimal("0.15")
    assert vendor.executions == 1  # never two codes for one unit
    keys = {h["idempotency_key"] for m, p, h in vendor.requests if m == "POST"}
    assert keys == {job.idempotency_key}
    assert await env.drain() >= 1
    assert await _status(buyer, order) == "delivered"


async def test_zovocard_channel_unavailable_without_alternative(env, up):
    vendor = await _zovo(up)
    vendor.force("/gpt-direct/cdks", "503", times=2)  # submit + verifying replay
    _buyer, order = await up.paid_order()
    for _ in range(3):
        await up.run()
    (job,) = await up.jobs(order["id"])
    assert job.state == "needs_manual" and job.error_code.startswith("no_source")
    assert vendor.executions == 0


async def test_zovocard_void_and_rejected_void():
    from app.suppliers.base import OfferSpec
    from app.suppliers.zovocard import TokenBucket, ZovoCardSupplier, ZovoClient, ZovoConfig

    vendor = MockZovo()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_mock_zovocard(vendor))
    ) as h:
        s = ZovoCardSupplier(
            "zovocard",
            ZovoClient(ZovoConfig(base_url="http://m/openapi/v1", app_secret=OK_KEY), http=h,
                       bucket=TokenBucket(6000, 100)),
        )  # fmt: skip
        job = JobSpec("j", "submit", "pj:j:submit", OfferSpec("cdk", {"plan": "plus"}))
        res = await s.submit(job)
        assert res.outcome is Outcome.OK and res.cost == Money(Decimal("0.15"), "USD")
        void = JobSpec("j", "void", "pj:j:void", job.offer, upstream_ref=res.upstream_ref)
        assert (await s.void(void)).outcome is Outcome.OK
        again = JobSpec("j", "void", "pj:j:void2", job.offer, upstream_ref=res.upstream_ref)
        r = await s.void(again)
        assert r.outcome is Outcome.FATAL and r.error_code == "CDK_DISABLE_REJECTED"
        bad = await s.submit(
            JobSpec("k", "submit", "pj:k:submit", OfferSpec("cdk", {"plan": "x y"}))
        )
        assert bad.outcome is Outcome.FATAL
        prod = ZovoCardSupplier(
            "zovocard",
            ZovoClient(ZovoConfig(base_url="http://m/openapi/v1", app_secret=OK_KEY,
                                  env="production"), http=h, bucket=TokenBucket(6000, 100)),
        )  # fmt: skip
        before = len(vendor.requests)
        blocked = await prod.submit(job)
        assert blocked.outcome is Outcome.UNAVAILABLE
        assert blocked.error_code == "production_writes_disabled"
        assert len(vendor.requests) == before  # the production guard stays


# ------------------------------------------------------------------ webhooks


def _signed(body: dict | list, secret: str = WEBHOOK_SECRET) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Signature": sig, "Content-Type": "application/json"}


@pytest.fixture
async def hook(env):
    from app.models import SupplierAccount

    env.res.settings = env.res.settings.model_copy(
        update={"zovocard_sandbox_webhook_secret": WEBHOOK_SECRET}
    )
    sid = uuid.uuid4()
    async with env.owner_session() as db:
        db.add(
            SupplierAccount(
                id=sid,
                tenant_id=env.tenant_id,
                code="zovocard",
                adapter="zovocard",
                label="z",
                env="sandbox",
                settings={},
            )
        )
        await db.commit()
    return sid


async def test_webhook_dedup_masking_and_forgery(env, hook):
    c = env.client(csrf=False)
    ev = {"type": "cdk.consumed", "event_id": "cdk.consumed:77", "cdk_id": 1001,
          "cdk_status": "consumed", "plan": "plus", "card_number": "4147000000001234"}  # fmt: skip
    raw, headers = _signed(ev)
    assert (
        await c.post("/api/v1/webhooks/zovocard", content=raw, headers=headers)
    ).status_code == 200
    assert (
        await c.post("/api/v1/webhooks/zovocard", content=raw, headers=headers)
    ).status_code == 200
    assert await env.scalar("SELECT count(*) FROM supplier_events") == 1
    assert await env.scalar("SELECT count(*) FROM outbox WHERE topic = 'supplier.event'") == 1
    # forged: wrong secret, tampered body, missing signature
    bad_raw, bad_h = _signed(ev, secret="whsec_wrong_" + "b" * 32)
    for body, h in ((bad_raw, bad_h), (raw + b" ", headers), (raw, {})):
        r = await c.post("/api/v1/webhooks/zovocard", content=body, headers=h)
        assert r.status_code == 401 and r.content == b""
    # balance_change: card number masked, remark (with the full PAN) never stored
    bc = {"event": "balance_change", "balance_log_id": 9, "type": "open_card", "amount": -1.5,
          "currency": "USD", "balance_after": 10.77, "card_number": "4147000000005678",
          "remark": "card 4147000000005678"}  # fmt: skip
    raw2, h2 = _signed(bc)
    assert (await c.post("/api/v1/webhooks/zovocard", content=raw2, headers=h2)).status_code == 200
    assert (await c.post("/api/v1/webhooks/zovocard", content=raw2, headers=h2)).status_code == 200
    payload = await env.scalar(
        "SELECT payload FROM supplier_events WHERE dedup_key = 'balance_change:9'"
    )
    assert payload["card_number"] == "414700****5678" and "remark" not in payload
    assert payload["amount"] == "-1.5"
    assert await env.scalar("SELECT count(*) FROM supplier_events") == 2
    dump = await env.scalar("SELECT string_agg(payload::text, '|') FROM supplier_events")
    assert "4147000000005678" not in dump and "4147000000001234" not in dump
    # oversized
    big = b"{" + b" " * (70 * 1024) + b"}"
    r = await c.post("/api/v1/webhooks/zovocard", content=big, headers=headers)
    assert r.status_code == 413


async def test_webhook_out_of_order_and_never_fulfils(env, up, hook):
    """cdk.consumed may arrive before cdk.reserved: the terminal status is kept; and an event
    for an unknown/active job only makes it due, it never fulfils anything."""
    from app.models import ProcurementJob

    mock = MockSupplier("mock_a")
    await up.supplier(mock)
    _buyer, order = await up.paid_order()
    await up.run()
    (job,) = await up.jobs(order["id"])
    # rebind the job to the zovocard account the webhook resolves to
    async with env.owner_session() as db:
        await db.execute(
            text(
                "UPDATE procurement_jobs SET supplier_id = :s, upstream_ref = '555' WHERE id = :j"
            ),
            {"s": hook, "j": job.id},
        )
        await db.commit()
    c = env.client(csrf=False)
    for etype, status in (("cdk.consumed", "consumed"), ("cdk.reserved", "reserved")):
        raw, h = _signed({"type": etype, "event_id": f"{etype}:555", "cdk_id": 555,
                          "cdk_status": status})  # fmt: skip
        assert (
            await c.post("/api/v1/webhooks/zovocard", content=raw, headers=h)
        ).status_code == 200
    await up.run()
    async with env.owner_session() as db:
        fresh = await db.get(ProcurementJob, job.id)
        assert fresh.upstream_status == "consumed"
        assert fresh.state == "fulfilled"
    assert await env.scalar("SELECT count(*) FROM supplier_events WHERE processed_at IS NULL") == 0


async def test_webhook_from_other_tenant_host_is_not_stored(env, hook):
    """The signed event resolves only the platform tenant's account."""
    from app.models import CustomDomain

    async with env.owner_session(env.other_tenant_id) as db:
        db.add(CustomDomain(tenant_id=env.other_tenant_id, hostname="shop.other.test",
                            verify_token="x", status="active",
                            verified_at=datetime.now(timezone.utc)))  # fmt: skip
        await db.commit()
    c = env.client(host="shop.other.test", csrf=False)
    raw, h = _signed({"type": "cdk.consumed", "event_id": "x:1", "cdk_id": 1})
    r = await c.post("/api/v1/webhooks/zovocard", content=raw, headers=h)
    assert r.status_code == 200
    assert await env.scalar("SELECT count(*) FROM supplier_events") == 0
    async with env.owner_session(env.other_tenant_id) as db:
        n = await db.scalar(text("SELECT count(*) FROM supplier_events"))
    assert n == 0


# ------------------------------------------------------------------ cross-tenant / grants


async def test_engine_never_uses_another_tenants_offer(env, up):
    """A paid upstream order of tenant B with offers only in tenant A: no source (RLS)."""
    mock = MockSupplier("mock_a")
    await up.supplier(mock)  # platform tenant
    other = await env.product(mode="upstream_cdk", tenant_id=env.other_tenant_id)
    buyer = await env.user("buyer", tenant_id=env.other_tenant_id)
    oid = uuid.uuid4()
    async with env.owner_session(env.other_tenant_id) as db:
        await db.execute(
            text(
                "INSERT INTO orders (id, tenant_id, order_no, buyer_id, product_id, title, qty, "
                "amount_minor, subtotal_minor, currency, status, delivery_mode) VALUES "
                "(:i, :t, :n, :b, :p, 'x', 1, 100, 100, 'CNY', 'paid', 'upstream_cdk')"
            ),
            {"i": oid, "t": env.other_tenant_id, "n": "G" + uuid.uuid4().hex[:12],
             "b": buyer.id, "p": other.id},
        )  # fmt: skip
        await db.execute(
            text("INSERT INTO outbox (tenant_id, topic, key, payload) VALUES "
                 "(:t, 'procure.requested', 'k', :p)"),
            {"t": env.other_tenant_id, "p": json.dumps({"order_id": str(oid)})},
        )  # fmt: skip
        await db.commit()
    await up.run()
    async with env.owner_session(env.other_tenant_id) as db:
        state, code = (
            await db.execute(text("SELECT state, error_code FROM procurement_jobs"))
        ).one()
    assert (state, code) == ("needs_manual", "no_source")
    assert not mock.charged


async def test_write_grants_are_narrow(env, up):
    from app.core.db import make_engine, make_sessionmaker, tenant_session

    p = up.product
    async with tenant_session(up.ctx.maker, env.tenant_id) as db:
        for sql, params in (
            ("INSERT INTO card_stock (tenant_id, product_id, blob, fingerprint, kid, status) "
             "VALUES (:t, :p, 'x', 'fp1', 'k', 'available')", {"t": env.tenant_id, "p": p.id}),
            ("INSERT INTO outbox (tenant_id, topic, key, payload) VALUES "
             "(:t, 'order.paid', 'k', '{}')", {"t": env.tenant_id}),
            ("UPDATE orders SET status = status", {}),
            ("SELECT id FROM card_stock", {}),
        ):  # fmt: skip
            with pytest.raises(Exception, match=r"row-level security|permission denied"):
                await db.execute(text(sql), params)
            await db.rollback()
        # an order.paid row of this tenant is invisible to procurement
        await db.commit()
    async with env.owner_session() as db:
        await db.execute(
            text("INSERT INTO outbox (tenant_id, topic, key, payload) VALUES "
                 "(:t, 'order.paid', 'vis', '{}')"),
            {"t": env.tenant_id},
        )  # fmt: skip
        await db.commit()
    async with tenant_session(up.ctx.maker, env.tenant_id) as db:
        assert await db.scalar(text("SELECT count(*) FROM outbox WHERE key = 'vis'")) == 0
    deng = make_engine(env.s.delivery_url, pool_size=1, max_overflow=0)
    try:
        async with tenant_session(make_sessionmaker(deng), env.tenant_id) as db:
            with pytest.raises(Exception, match="row-level security"):
                await db.execute(
                    text("INSERT INTO outbox (tenant_id, topic, key, payload) VALUES "
                         "(:t, 'procure.fulfilled', 'k', '{}')"),
                    {"t": env.tenant_id},
                )  # fmt: skip
    finally:
        await deng.dispose()


# ------------------------------------------------------------------ migration


async def test_migration_0024_up_and_down(services):
    from alembic import command
    from alembic.config import Config

    db_name = "g24_mig0024"
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

    try:
        await alembic("0024_procurement_cdk")
        conn = await asyncpg.connect(owner_url.replace("+asyncpg", ""), timeout=5)
        try:
            pols = {r["policyname"] for r in await conn.fetch("SELECT policyname FROM pg_policies")}
            assert {"procurement_insert_sold", "procurement_emits", "delivery_emits"} <= pols
            cols = await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns WHERE column_name = "
                "'procurement_routing'"
            )
            assert cols == 2
        finally:
            await conn.close()
        await alembic("0023_procurement", down=True)
        conn = await asyncpg.connect(owner_url.replace("+asyncpg", ""), timeout=5)
        try:
            pols = {r["policyname"] for r in await conn.fetch("SELECT policyname FROM pg_policies")}
            assert not {"procurement_insert_sold", "procurement_emits", "delivery_emits"} & pols
        finally:
            await conn.close()
        await alembic("head")
    finally:
        admin = await asyncpg.connect(plain, timeout=5)
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")
        finally:
            await admin.close()
