"""review-v12 M2: supplier cost ledger (balance_change only, one unique insert path)."""

# ruff: noqa: F811  (fixtures are imported from test_procurement_m2)

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx

from app.devtools.mock_zovocard import OK_KEY, MockZovo, create_mock_zovocard
from app.suppliers.zovocard import TokenBucket, ZovoCardSupplier, ZovoClient, ZovoConfig
from tests.test_procurement_m2 import WEBHOOK_SECRET, Up, _signed, hook, up  # noqa: F401

PAN = "4147000000005678"


async def _zovo(up: Up, vendor: MockZovo) -> tuple[uuid.UUID, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_mock_zovocard(vendor)))
    up.vendor_http = http
    supplier = ZovoCardSupplier(
        "zovocard",
        ZovoClient(
            ZovoConfig(base_url="http://m/openapi/v1", app_secret=OK_KEY),
            http=http,
            bucket=TokenBucket(6000, 100),
        ),
    )
    sid, _ = await up.supplier(supplier, code="zovocard", adapter="zovocard")
    return sid, http


async def _rows(env):
    from sqlalchemy import text

    async with env.owner_session() as db:
        res = await db.execute(
            text(
                "SELECT external_id, type, amount, currency, card_mask, ref_id, job_id "
                "FROM supplier_ledger ORDER BY external_id::int"
            )
        )
        return list(res)


async def _exec(env, sql: str, **params) -> None:
    from sqlalchemy import text

    async with env.owner_session() as db:
        await db.execute(text(sql), params)
        await db.commit()


async def test_webhook_balance_change_is_booked_once_and_card_fee_never(env, up, hook):
    c = env.client(csrf=False)
    bc = {"event": "balance_change", "balance_log_id": 9, "type": "open_card", "amount": -1.5,
          "direction": "debit", "currency": "USD", "balance_after": 10.77,
          "card_number": PAN, "remark": f"card {PAN}",
          "occurred_at": "2026-11-14 10:15:02"}  # fmt: skip
    fee = {"event": "card_fee", "fee_type": "decline_fee", "auth_id": "a1", "amount": 0.3,
           "currency": "USD", "direction": "debit"}  # fmt: skip
    for body in (bc, bc, fee):
        raw, h = _signed(body)
        assert (
            await c.post("/api/v1/webhooks/zovocard", content=raw, headers=h)
        ).status_code == 200
    assert await env.scalar("SELECT count(*) FROM supplier_events") == 2
    mock = await _noop_supplier(up, hook)
    await up.run()
    assert not mock.calls
    (row,) = await _rows(env)
    assert row.external_id == "9" and row.type == "open_card"
    assert row.amount == Decimal("-1.5") and isinstance(row.amount, Decimal)
    assert row.currency == "USD" and row.card_mask == "414700****5678"
    assert await env.scalar("SELECT count(*) FROM supplier_events WHERE processed_at IS NULL") == 0
    dump = await env.scalar("SELECT string_agg(supplier_ledger::text, '|') FROM supplier_ledger")
    assert PAN not in dump
    # the engine consuming the same event again (redelivery) books nothing new
    await _exec(env, "UPDATE supplier_events SET processed_at = NULL")
    await _exec(env, "UPDATE outbox SET processed_at = NULL WHERE topic = 'supplier.event'")
    await up.run()
    assert len(await _rows(env)) == 1


async def _noop_supplier(up: Up, sid: uuid.UUID):
    from app.suppliers.mock import MockSupplier

    m = MockSupplier("zovocard")
    up.mocks[sid] = m
    return m


async def test_backfill_shares_the_unique_path_with_the_webhook(env, up):
    from app.procurement.service import backfill_ledgers

    vendor = MockZovo()
    vendor.balance_logs = [
        {"id": 3, "created_at": "2026-11-14T12:43:37.250019Z", "amount": -17.25, "before": 23.52,
         "after": 6.27, "type": "open_card", "remark": f"card {PAN} open", "ref_id": 903951,
         "card_id": 921097, "card_number": PAN, "wallet": "main"},
        {"id": 2, "created_at": "2026-11-14T04:31:37Z", "amount": 50, "before": 0,
         "after": 50, "type": "recharge", "remark": "top-up", "wallet": "main"},
        {"id": 1, "created_at": "2026-11-13T04:31:37Z", "amount": 0, "type": "admin"},
    ]  # fmt: skip
    sid, _ = await _zovo(up, vendor)
    assert await backfill_ledgers(up.ctx) == 3
    assert await backfill_ledgers(up.ctx) == 0  # idempotent
    rows = await _rows(env)
    assert [r.external_id for r in rows] == ["1", "2", "3"]
    by = {r.external_id: r for r in rows}
    assert by["3"].amount == Decimal("-17.25") and by["3"].card_mask == "414700****5678"
    assert by["3"].ref_id == "903951" and by["3"].job_id is None
    assert by["2"].amount == Decimal("50") and by["2"].card_mask is None
    assert all(r.currency == "USD" for r in rows)
    dump = await env.scalar("SELECT string_agg(supplier_ledger::text, '|') FROM supplier_ledger")
    assert PAN not in dump and "top-up" not in dump
    # the same id arriving as a webhook later is the same row
    from app.procurement import ledger

    async with up.env.owner_session() as db:
        booked = await ledger.handle_balance_event(
            db,
            tenant_id=env.tenant_id,
            supplier_id=sid,
            payload={"balance_log_id": 3, "type": "open_card", "amount": "-17.25"},
            received_at=None,
        )
        await db.commit()
    assert booked is False
    assert len(await _rows(env)) == 3


async def test_backfill_pages_until_nothing_new_and_links_cdk_rows_to_jobs(env, up):
    from app.procurement import ledger
    from app.procurement.service import SupplierRef, backfill_ledgers

    vendor = MockZovo()
    sid, _ = await _zovo(up, vendor)
    await up.paid_order()
    await up.run()  # the job is created and fulfilled through the mock vendor
    # a job of this supplier with an upstream reference the vendor row points at
    await _exec(
        env,
        "UPDATE procurement_jobs SET supplier_id = :s, upstream_ref = '777'",
        s=sid,
    )
    vendor.balance_logs = [
        {"id": i, "created_at": "2026-11-14T00:00:00Z", "amount": -1, "type": "recharge"}
        for i in range(250, 0, -1)
    ]
    vendor.balance_logs.insert(
        0,
        {
            "id": 999,
            "created_at": "2026-11-15T00:00:00Z",
            "amount": -5.5,
            "type": "gpt_direct_cdk_purchase",
            "ref_id": 777,
        },
    )
    assert await backfill_ledgers(up.ctx) == 251
    rows = await _rows(env)
    linked = [r for r in rows if r.job_id is not None]
    assert [r.external_id for r in linked] == ["999"]
    # a row that arrived before its job knew the reference is linked on the next pass
    await _exec(env, "UPDATE supplier_ledger SET job_id = NULL")
    ref = SupplierRef(sid, env.tenant_id, "zovocard", "zovocard", "sandbox", "unknown")
    async with up.env.owner_session() as db:
        assert await ledger.link_rows(db, env.tenant_id, sid) == 1
        await db.commit()
    assert ref.id == sid


async def test_migration_0025_up_and_down(services):
    import asyncio
    import os

    import asyncpg
    from alembic import command
    from alembic.config import Config

    from tests.conftest import ALEMBIC_INI, OWNER_PW, _with_db

    db_name = "g24_mig0025"
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

    async def cols() -> set[str]:
        conn = await asyncpg.connect(owner_url.replace("+asyncpg", ""), timeout=5)
        try:
            rows = await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE column_name IN ('ref_id', 'card_mask', 'breaker_kind')"
            )
            return {r["column_name"] for r in rows}
        finally:
            await conn.close()

    try:
        await alembic("0025_procurement_ledger_breaker")
        assert await cols() == {"ref_id", "card_mask", "breaker_kind"}
        await alembic("0024_procurement_cdk", down=True)
        assert await cols() == set()
        await alembic("head")
    finally:
        admin = await asyncpg.connect(plain, timeout=5)
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")
        finally:
            await admin.close()
