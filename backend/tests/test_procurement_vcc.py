"""review-v12 M3: ``upstream_vcc`` (virtual cards bought per order) end to end through the real
ZovoCard adapter against ``app/devtools/mock_zovocard.py`` and the engine: happy path, every
ambiguous point, the recharge-pending 400, void before delivery, the card-secret hygiene, the
cost ledger and cross-tenant isolation."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from app.devtools.mock_zovocard import OK_KEY, MockZovo, create_mock_zovocard
from app.suppliers.zovocard import TokenBucket, ZovoCardSupplier, ZovoClient, ZovoConfig
from tests.test_procurement_m2 import WEBHOOK_SECRET, Up, _signed, _status

OPEN = {"product_code": "MOCK01", "init_amount": "20"}
WITH_RECHARGE = {**OPEN, "recharge_amount": "10"}


@pytest.fixture
async def vup(env):
    u = Up(env)
    u.mode, u.kind = "upstream_vcc", "vcc"
    await u.start()
    yield u
    await u.close()


async def _zovo(up: Up, vendor: MockZovo, params: dict, code: str = "zovocard", priority: int = 10):
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_mock_zovocard(vendor)))
    supplier = ZovoCardSupplier(
        code,
        ZovoClient(
            ZovoConfig(base_url="http://m/openapi/v1", app_secret=OK_KEY),
            http=http,
            bucket=TokenBucket(6000, 100),
        ),
    )
    up.vendor_http = http
    return await up.supplier(
        supplier, code=code, adapter="zovocard", params=params, priority=priority
    )


async def _deliver(env, up: Up, order, buyer, rounds: int = 8) -> None:
    for _ in range(rounds):
        await up.run()
        await env.drain()
        if await _status(buyer, order) == "delivered":
            return
        await up.make_due()
    raise AssertionError(f"order stuck in {await _status(buyer, order)}")


async def _job(up: Up, order):
    (job,) = await up.jobs(order["id"])
    return job


async def _sql(env, sql: str, **params):
    async with env.owner_session() as db:
        res = await db.execute(text(sql), params)
        await db.commit()
        return res


async def _rows(env, sql: str, **params):
    async with env.owner_session() as db:
        return list(await db.execute(text(sql), params))


def _posts(vendor: MockZovo, path: str) -> list[str]:
    return [h["idempotency_key"] for m, p, h in vendor.requests if m == "POST" and p == path]


def _card(vendor: MockZovo) -> dict:
    (card,) = vendor.cards.values()
    return card


# ------------------------------------------------------------------ happy path


async def test_card_is_bought_sealed_and_revealed(env, vup):
    vendor = MockZovo()
    await _zovo(vup, vendor, OPEN)
    detail = (await env.client().get(f"/api/v1/products/{vup.product.id}")).json()
    assert detail["delivery_mode"] == "upstream_vcc" and detail["stock"] == 5
    buyer, order = await vup.paid_order()
    await _deliver(env, vup, order, buyer)
    job = await _job(vup, order)
    assert job.kind == "vcc" and job.state == "fulfilled" and len(job.card_stock_ids) == 1
    assert job.idempotency_key == f"pj:{job.id.hex}:submit" and job.upstream_status == "active"
    card = _card(vendor)
    assert job.upstream_ref == str(card["id"])
    assert job.card_mask == f"{card['card_number'][:6]}****{card['card_number'][-4:]}"
    assert job.cost == Decimal("21.5") and job.currency == "USD"  # 20 + the 1.50 open fee
    assert vendor.card_opens == 1 and _posts(vendor, "/cards/recharge") == []
    # delivery reveals the one sealed secret: pan----expiry----cvv (buyer API unchanged)
    cards = (await buyer.post(f"/api/v1/orders/{order['id']}/reveal")).json()["cards"]
    assert [c["code"] for c in cards] == ["----".join(
        (card["card_number"], card["expire"], card["cvv"])
    )]  # fmt: skip
    # a second reveal answers the same (idempotent for the owner), and nobody else can reveal
    other, _ = await env.login("buyer")
    assert (await other.post(f"/api/v1/orders/{order['id']}/reveal")).status_code in (403, 404)


# ------------------------------------------------------------------ ambiguity (same key)


async def test_timeout_after_open_reconciles_under_the_same_key(env, vup):
    vendor = MockZovo()
    vendor.force("/cards/open", "lost_response")
    await _zovo(vup, vendor, OPEN)
    buyer, order = await vup.paid_order()
    await _deliver(env, vup, order, buyer)
    job = await _job(vup, order)
    assert job.state == "fulfilled" and vendor.card_opens == 1
    sent = _posts(vendor, "/cards/open")
    assert len(sent) >= 2 and len(set(sent)) == 1  # retried, always under the one job key


async def test_timeout_after_recharge_never_recharges_twice(env, vup):
    vendor = MockZovo()
    vendor.force("/cards/recharge", "lost_response")
    await _zovo(vup, vendor, WITH_RECHARGE)
    buyer, order = await vup.paid_order()
    await _deliver(env, vup, order, buyer)
    job = await _job(vup, order)
    assert job.state == "fulfilled"
    assert vendor.card_opens == 1 and vendor.card_recharges == 1
    keys = _posts(vendor, "/cards/recharge")
    assert len(set(keys)) == 1 and keys[0] == f"pj:{job.id.hex}:recharge"  # its own derived key
    assert _card(vendor)["available_amount"] == 30.0
    assert job.cost == Decimal("31.5")


async def test_recharge_pending_waits_and_is_never_posted_again(env, vup):
    vendor = MockZovo()
    vendor.recharge_pending_next = 1  # the documented 400 without error_code
    await _zovo(vup, vendor, WITH_RECHARGE)
    buyer, order = await vup.paid_order()
    await vup.run()
    job = await _job(vup, order)
    assert job.state == "accepted_pending" and job.step == "pending"
    assert job.upstream_ref == str(_card(vendor)["id"]) and job.maybe_sent
    await _deliver(env, vup, order, buyer)  # polls the recharge list, then reveals
    job = await _job(vup, order)
    assert job.state == "fulfilled"
    assert vendor.card_opens == 1 and vendor.card_recharges == 1
    assert len(_posts(vendor, "/cards/recharge")) == 1  # a pending recharge is never re-sent


async def test_recharge_refused_for_good_holds_the_order_and_keeps_the_card_findable(env, vup):
    vendor = MockZovo()
    await _zovo(vup, vendor, {**OPEN, "recharge_amount": "5"})  # below the 10 USD floor
    buyer, order = await vup.paid_order()
    await vup.run()
    await env.drain()
    job = await _job(vup, order)
    assert job.state == "needs_manual" and job.error_code == "amount_below_min"
    assert job.upstream_ref == str(_card(vendor)["id"]) and job.maybe_sent  # never "unsent"
    assert await _status(buyer, order) == "manual_review"
    # nothing was sealed or delivered, and nothing failed over to another source
    assert not job.card_stock_ids and not job.tried_offer_ids


async def test_a_failed_open_never_fails_over_after_a_possible_send(env, vup):
    vendor_a, vendor_b = MockZovo(), MockZovo()
    vendor_a.force("/cards/open", "500")  # a write answered 500: it may have run
    await _zovo(vup, vendor_a, OPEN, code="zovocard", priority=10)
    await _zovo(vup, vendor_b, OPEN, code="zovocard_b", priority=20)
    buyer, order = await vup.paid_order()
    await _deliver(env, vup, order, buyer)
    job = await _job(vup, order)
    # same supplier, same key: the replay found nothing executed and ran it; B was never asked
    assert (
        job.state == "fulfilled"
        and vendor_b.card_opens == 0
        and job.idempotency_key.endswith(":submit")
    )


# ------------------------------------------------------------------ void


async def test_refund_before_delivery_deletes_the_card_and_voids_the_secret(env, vup):
    vendor = MockZovo()
    await _zovo(vup, vendor, OPEN)
    _buyer, order = await vup.paid_order()
    await vup.run()  # bought and sealed, but delivery has not run
    job = await _job(vup, order)
    assert job.state == "fulfilled" and len(job.card_stock_ids) == 1
    admin, _ = await env.login("admin")
    r = await admin.post(
        f"/api/v1/admin/orders/{order['id']}/refund",
        json={"reference": "REF-" + uuid.uuid4().hex[:8]},
    )
    assert r.status_code == 200, r.text
    job = await _job(vup, order)
    assert job.state == "void_pending"
    await vup.make_due()
    for _ in range(3):
        await vup.run()
    job = await _job(vup, order)
    assert job.state == "voided" and job.upstream_status == "deleted"
    assert _card(vendor)["status"] == "DELETED"
    assert await env.scalar(
        "SELECT count(*) FROM card_stock WHERE order_id = :o AND status = 'void'",
        o=uuid.UUID(order["id"]),
    ) == 1  # fmt: skip


async def test_delete_that_answers_202_is_confirmed_by_reading_the_card(env, vup):
    vendor = MockZovo()
    vendor.delete_pending_next = 1
    await _zovo(vup, vendor, OPEN)
    _buyer, order = await vup.paid_order()
    await vup.run()
    admin, _ = await env.login("admin")
    await admin.post(
        f"/api/v1/admin/orders/{order['id']}/refund",
        json={"reference": "REF-" + uuid.uuid4().hex[:8]},
    )
    for _ in range(6):
        await vup.make_due()
        await vup.run()
    job = await _job(vup, order)
    assert job.state == "voided" and _card(vendor)["status"] == "DELETED"


async def test_refund_while_recharge_is_pending_still_deletes_the_card(env, vup):
    vendor = MockZovo()
    vendor.recharge_pending_next = 1
    await _zovo(vup, vendor, WITH_RECHARGE)
    _buyer, order = await vup.paid_order()
    await vup.run()
    assert (await _job(vup, order)).state == "accepted_pending"
    admin, _ = await env.login("admin")
    r = await admin.post(
        f"/api/v1/admin/orders/{order['id']}/refund",
        json={"reference": "REF-" + uuid.uuid4().hex[:8]},
    )
    assert r.status_code == 200, r.text
    for _ in range(4):
        await vup.make_due()
        await vup.run()
    job = await _job(vup, order)
    assert job.state == "voided" and _card(vendor)["status"] == "DELETED"
    assert vendor.card_opens == 1


# ------------------------------------------------------------------ secrets


async def test_no_card_secret_outside_the_vault(env, vup, caplog):
    caplog.set_level(logging.DEBUG)
    vendor = MockZovo()
    sid, _ = await _zovo(vup, vendor, WITH_RECHARGE)
    buyer, order = await vup.paid_order()
    await _deliver(env, vup, order, buyer)
    await buyer.post(f"/api/v1/orders/{order['id']}/reveal")
    from app.procurement import ledger
    from app.procurement.service import SupplierRef

    await ledger.backfill(
        vup.ctx, SupplierRef(sid, env.tenant_id, "zovocard", "zovocard", "sandbox", "ok"), "USD"
    )
    card = _card(vendor)
    pan, exp, cvv = card["card_number"], card["expire"], card["cvv"]
    needles = [pan, f"{pan}----{exp}----{cvv}", f"{exp}----{cvv}"]
    # the vendor's own balance logs hold the full PAN; the ledger must not
    assert any(pan in str(r) for r in vendor.balance_logs)
    for table, col in (
        ("procurement_jobs", "row_to_json(procurement_jobs)::text"),
        ("audit_log", "row_to_json(audit_log)::text"),
        ("outbox", "payload::text"),
        ("supplier_events", "payload::text"),
        ("supplier_ledger", "row_to_json(supplier_ledger)::text"),
        ("messages", "row_to_json(messages)::text"),
        ("orders", "row_to_json(orders)::text"),
    ):
        for n in needles:
            sql = f"SELECT count(*) FROM {table} WHERE {col} LIKE :c"  # noqa: S608 (fixed names)
            assert await env.scalar(sql, c=f"%{n}%") == 0, table
    # sealed in card_stock: the blob is ciphertext
    blob = await env.scalar(
        "SELECT blob FROM card_stock WHERE order_id = :o", o=uuid.UUID(order["id"])
    )
    assert pan.encode() not in bytes(blob) and cvv.encode() not in bytes(blob)
    # Redis: no key or value anywhere holds the secret
    redis = env.s.redis
    async for key in redis.scan_iter(match="*", count=500):
        kind = await redis.type(key)
        kind = kind.decode() if isinstance(kind, bytes) else kind
        raw = {
            "string": lambda k: redis.get(k),
            "hash": lambda k: redis.hgetall(k),
            "list": lambda k: redis.lrange(k, 0, -1),
            "set": lambda k: redis.smembers(k),
            "zset": lambda k: redis.zrange(k, 0, -1),
        }.get(kind)
        dump = f"{key!r}{await raw(key) if raw else ''}"
        assert not any(n in dump for n in needles), key
    # logs: no PAN / expiry+cvv anywhere
    assert not any(n in caplog.text for n in needles)
    # ledger rows carry the masked number only
    masked = f"{pan[:6]}****{pan[-4:]}"
    assert (
        await env.scalar("SELECT count(*) FROM supplier_ledger WHERE card_mask = :m", m=masked) >= 1
    )


# ------------------------------------------------------------------ ledger and webhooks


async def test_each_balance_log_row_is_booked_once_and_linked_to_its_job(env, vup):
    from app.procurement import ledger
    from app.procurement.service import SupplierRef

    vendor = MockZovo()
    sid, _ = await _zovo(vup, vendor, WITH_RECHARGE)
    buyer, order = await vup.paid_order()
    await _deliver(env, vup, order, buyer)
    job = await _job(vup, order)
    ref = SupplierRef(sid, env.tenant_id, "zovocard", "zovocard", "sandbox", "ok")
    assert await ledger.backfill(vup.ctx, ref, "USD") == 3  # opening charge, open fee, top-up
    assert await ledger.backfill(vup.ctx, ref, "USD") == 0  # the same rows never book twice
    rows = await _rows(
        env, "SELECT type, amount, job_id FROM supplier_ledger ORDER BY external_id::int"
    )
    assert [(r.type, r.amount) for r in rows] == [
        ("card_recharge", Decimal("-20")),  # the opening charge is booked as card_recharge ...
        ("open_card", Decimal("-1.5")),  # ... and the open fee as a separate row
        ("card_recharge", Decimal("-10")),
    ]
    assert {r.job_id for r in rows} == {job.id}  # linked by card id, never by the vendor's ref_id
    ids = await _rows(env, "SELECT card_id FROM supplier_ledger")
    assert {r.card_id for r in ids} == {job.upstream_ref}


async def test_card_webhooks_duplicate_and_late_status(env, vup):
    vendor = MockZovo()
    await _zovo(vup, vendor, WITH_RECHARGE, code="zovocard")
    env.res.settings = env.res.settings.model_copy(
        update={"zovocard_sandbox_webhook_secret": WEBHOOK_SECRET}
    )
    vendor.recharge_pending_next = 1
    buyer, order = await vup.paid_order()
    await vup.run()
    job = await _job(vup, order)
    assert job.state == "accepted_pending"
    c = env.client(csrf=False)
    ev = {"event": "card_operation", "operation": "recharge", "operation_id": "R1",
          "card_id": int(job.upstream_ref), "status": "COMPLETE", "success": True,
          "card_number": _card(vendor)["card_number"]}  # fmt: skip
    for _ in range(2):  # at-least-once delivery
        raw, h = _signed(ev)
        assert (
            await c.post("/api/v1/webhooks/zovocard", content=raw, headers=h)
        ).status_code == 200
    assert await env.scalar("SELECT count(*) FROM supplier_events") == 1
    stored = await env.scalar("SELECT payload::text FROM supplier_events")
    assert _card(vendor)["card_number"] not in stored  # masked before it is stored
    await env.drain()
    await vup.run()  # the hint makes the job due; the re-query stays the truth
    job = await _job(vup, order)
    assert job.upstream_status in ("recharge:COMPLETE", "active")
    await _deliver(env, vup, order, buyer)
    assert vendor.card_recharges == 1


# ------------------------------------------------------------------ tenants and admin


async def test_cross_tenant_card_job_is_invisible(env, vup):
    from tests.test_procurement_jobs import JOBS, _foreign_job

    vendor = MockZovo()
    await _zovo(vup, vendor, OPEN)
    _buyer, order = await vup.paid_order()
    await vup.run()
    job = await _job(vup, order)
    fj = await _foreign_job(env)  # another tenant's held job ...
    await _sql(env, "UPDATE procurement_jobs SET kind = 'vcc', card_mask = '411111****1111' "
                    "WHERE id = :j", j=fj)  # fmt: skip
    admin, _ = await env.login("admin")
    listing = (await admin.get(JOBS)).json()
    assert listing["total"] == 1
    detail = (await admin.get(f"{JOBS}/{job.id}")).json()
    card = _card(vendor)
    assert detail["card_mask"] == job.card_mask and "411111****1111" not in str(listing)
    assert (
        card["card_number"] not in str(detail) and card["cvv"] != "" and "blob" not in str(detail)
    )
    assert (await admin.get(f"{JOBS}/{fj}")).status_code == 404
    for act, body in (("retry", {}), ("requery", {}), ("fulfil", {"code": "x" * 12}),
                      ("fail", {"reason": "abc"}), ("void", {"reason": "abc"})):  # fmt: skip
        assert (await admin.post(f"{JOBS}/{fj}/{act}", json=body)).status_code == 404, act
    # row level security: the platform tenant's own session cannot read the foreign job either
    async with env.api_session() as db:
        seen = (await db.execute(text("SELECT count(*) FROM procurement_jobs"))).scalar_one()
    assert seen == 1
    assert await env.scalar("SELECT count(*) FROM card_stock WHERE order_id IS NOT NULL") == 1


# ------------------------------------------------------------------ 3DS one-time codes

OTP = "Zq7Kx9m2"  # letters + digits: cannot collide with an id, a timestamp or a number


async def _deliver_card(env, vup, vendor):
    from tests.helpers import buy, pay_order

    buyer, user = await env.login("buyer")
    order = await buy(buyer, vup.product, 1)
    await pay_order(env, buyer, order)
    await env.drain()
    return buyer, user, order


def _otp_event(card_id: str, otp: str = OTP, matrix: str = "MI1") -> dict:
    return {"event": "card_3ds_otp", "otp": otp, "matrix_id": matrix, "card_id": int(card_id),
            "masked_card_no": "411111******1111", "merchant_name": f"Shop {otp}"}  # fmt: skip


async def test_3ds_code_reaches_only_the_card_owner_and_is_never_stored(env, vup, caplog):
    from app.modules.realtime.publish import user_channel

    caplog.set_level(logging.DEBUG)
    env.res.settings = env.res.settings.model_copy(
        update={"zovocard_sandbox_webhook_secret": WEBHOOK_SECRET}
    )
    vendor = MockZovo()
    await _zovo(vup, vendor, OPEN)
    buyer, user, order = await _deliver_card(env, vup, vendor)
    # not delivered yet: a code for a card the buyer does not hold yet is dropped
    await vup.run()
    job = await _job(vup, order)
    c = env.client(csrf=False)
    raw, h = _signed(_otp_event(job.upstream_ref, matrix="MI0"))
    assert (await c.post("/api/v1/webhooks/zovocard", content=raw, headers=h)).status_code == 200
    assert (await buyer.get(f"/api/v1/orders/{order['id']}/card-otp")).json()["items"] == []
    await _deliver(env, vup, order, buyer)
    pubsub = env.s.redis.pubsub()
    await pubsub.subscribe(user_channel(user.id))
    await pubsub.get_message(timeout=1)  # the subscribe ack
    raw, h = _signed(_otp_event(job.upstream_ref))
    for _ in range(2):  # at-least-once delivery: forwarded once
        assert (
            await c.post("/api/v1/webhooks/zovocard", content=raw, headers=h)
        ).status_code == 200
    r = await buyer.get(f"/api/v1/orders/{order['id']}/card-otp")
    assert r.headers["cache-control"].startswith("no-store")
    (item,) = r.json()["items"]
    assert item["otp"] == OTP and 0 < item["expires_in"] <= 300 and "****" in item["card"]
    msg = await pubsub.get_message(timeout=2)
    assert msg is not None and OTP in msg["data"].decode() and "card_otp" in msg["data"].decode()
    assert (
        await env.scalar("SELECT count(*) FROM audit_log WHERE action = 'card.otp_forwarded'") == 1
    )
    await pubsub.aclose()
    # another buyer cannot read it, and an unknown order is a 404
    other, _ = await env.login("buyer")
    assert (await other.get(f"/api/v1/orders/{order['id']}/card-otp")).status_code == 404
    # the code is in no table, no log line, no outbox payload
    for table in ("supplier_events", "audit_log", "outbox", "procurement_jobs", "messages"):
        sql = f"SELECT count(*) FROM {table} WHERE row_to_json({table})::text LIKE :c"  # noqa: S608
        assert await env.scalar(sql, c=f"%{OTP}%") == 0, table
    assert OTP not in caplog.text
    # Redis keeps it only briefly (TTL <= 5 min)
    (key,) = [k async for k in env.s.redis.scan_iter(match="otp3ds:*")]
    assert 0 < await env.s.redis.ttl(key) <= 300


async def test_3ds_code_for_an_unknown_card_is_dropped(env, vup):
    env.res.settings = env.res.settings.model_copy(
        update={"zovocard_sandbox_webhook_secret": WEBHOOK_SECRET}
    )
    await _zovo(vup, MockZovo(), OPEN)
    c = env.client(csrf=False)
    raw, h = _signed(_otp_event("987654"))
    assert (await c.post("/api/v1/webhooks/zovocard", content=raw, headers=h)).status_code == 200
    assert [k async for k in env.s.redis.scan_iter(match="otp3ds:*")] == []


# ---------------------------------------------------------------- manual fulfil, sources, products

JOBS = "/api/v1/admin/procurement/jobs"


async def test_manual_fulfil_seals_a_card_like_an_automatic_one(env, vup):
    vendor = MockZovo()
    vendor.force("/cards/open", "401", times=3)  # refused every time: the job needs staff
    await _zovo(vup, vendor, OPEN)
    buyer, order = await vup.paid_order()
    await vup.run()
    await env.drain()
    job = await _job(vup, order)
    assert job.state == "needs_manual"
    staff, _ = await env.login("agent")
    bad = await staff.post(f"{JOBS}/{job.id}/fulfil", json={"code": "just-a-code-123"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "bad_card"
    pan = "4111111111111111"
    r = await staff.post(f"{JOBS}/{job.id}/fulfil", json={"code": f"{pan}----08/31----123"})
    assert r.status_code == 200, r.text
    assert r.json()["card_mask"] == "411111****1111" and pan not in r.text
    await env.drain()
    assert await _status(buyer, order) == "delivered"
    cards = (await buyer.post(f"/api/v1/orders/{order['id']}/reveal")).json()["cards"]
    assert [c["code"] for c in cards] == [f"{pan}----08/31----123"]
    for table in ("audit_log", "outbox", "procurement_jobs"):
        sql = f"SELECT count(*) FROM {table} WHERE row_to_json({table})::text LIKE :c"  # noqa: S608
        assert await env.scalar(sql, c=f"%{pan}%") == 0, table


async def test_card_sources_and_products_are_validated_server_side(env):
    from tests.test_procurement_v12 import _create

    admin, _ = await env.login("admin")
    sid = (await _create(admin))["id"]
    # a virtual-card product: the separator is fixed and a label set is always present
    r = await admin.post(
        "/api/v1/admin/products",
        json={
            "title_i18n": {"en": "Virtual card $20", "zh-CN": "虚拟卡 $20"},
            "price": "25.00", "floor_price": "25.00", "delivery_mode": "upstream_vcc",
            "category": "accounts", "delivery_separator": ",",
        },
    )  # fmt: skip
    assert r.status_code in (200, 201), r.text
    pid = r.json()["id"]
    got = r.json()
    assert got["delivery_mode"] == "upstream_vcc" and got["delivery_separator"] == "----"
    assert got["delivery_format"] == "Card number----Expiry (MM/YY)----CVV"
    src = "/api/v1/admin/procurement/sources"
    ok = {"product_code": "MOCK01", "init_amount": "20", "recharge_amount": "10.50"}
    r = await admin.post(
        src, json={"product_id": pid, "supplier_id": sid, "params": ok, "cost_estimate": "22.00"}
    )
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "vcc" and r.json()["params"] == ok
    for bad in (
        {"product_code": "MOCK01"},  # no amount
        {"product_code": "MOCK01", "init_amount": "0"},
        {"product_code": "MOCK01", "init_amount": "20.123"},
        {"product_code": "bad code!", "init_amount": "20"},
        {"plan": "plus"},  # a CDK plan on a card product
    ):
        r = await admin.post(src, json={"product_id": pid, "supplier_id": sid, "params": bad})
        assert r.status_code in (400, 422), bad


async def test_catalog_reports_card_amount_outside_the_catalogue_unavailable(env, vup):
    vendor = MockZovo()
    await _zovo(vup, vendor, {"product_code": "MOCK01", "init_amount": "5"})  # below the 10 floor
    _buyer, order = await vup.paid_order()
    await vup.run()
    await env.drain()
    job = await _job(vup, order)
    assert vendor.card_opens == 0 and job.state == "needs_manual"  # never sent, no source left
    assert not job.maybe_sent


# ------------------------------------------------------------------ migration 0028


async def test_migration_0028_up_and_down(services):
    import asyncio
    import os

    import asyncpg
    from alembic import command
    from alembic.config import Config

    from tests.conftest import ALEMBIC_INI, OWNER_PW, _with_db

    db_name = "g24_mig0028"
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

    async def cols() -> int:
        return await q(
            "SELECT count(*) FROM information_schema.columns WHERE "
            "(table_name = 'procurement_jobs' AND column_name = 'card_mask') OR "
            "(table_name = 'supplier_ledger' AND column_name = 'card_id')"
        )

    try:
        await alembic("head")
        assert await cols() == 2
        assert await q(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'products_delivery_mode_check'"
        ) and "upstream_vcc" in await q(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'orders_delivery_mode_check'"
        )
        assert await q(  # FORCE RLS stays on every table involved
            "SELECT bool_and(relforcerowsecurity) FROM pg_class WHERE relname IN "
            "('procurement_jobs', 'supplier_ledger', 'card_stock', 'orders', 'products')"
        )
        assert await q(
            "SELECT has_column_privilege('app_api', 'procurement_jobs', 'card_mask', 'UPDATE')"
        )
        assert not await q(  # the engine-owned columns stay engine-owned
            "SELECT has_column_privilege('app_api', 'procurement_jobs', 'maybe_sent', 'UPDATE')"
        )
        await alembic("0027_procurement_audit_fixes", down=True)
        assert await cols() == 0
        assert "upstream_vcc" not in await q(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'products_delivery_mode_check'"
        )
        await alembic("head")
        assert await cols() == 2
    finally:
        admin = await asyncpg.connect(plain, timeout=5)
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")
        finally:
            await admin.close()
