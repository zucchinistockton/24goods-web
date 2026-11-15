"""review-v12 M1: supplier admin API, procurement probe/health, grants, migration 0023."""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from decimal import Decimal

import asyncpg
import httpx
import pytest
from sqlalchemy import text

from app.devtools.mock_zovocard import OK_KEY, MockZovo, create_mock_zovocard
from tests.conftest import ALEMBIC_INI, OWNER_PW, PASSWORD, _with_db

URL = "/api/v1/admin/suppliers"
TOKEN = secrets.token_urlsafe(32)


class Proc:
    """A procurement process wired to the test DB (as app_procurement) and the mock vendor."""

    def __init__(self, env, environ: dict[str, str] | None = None) -> None:
        from app.core.db import make_engine, make_sessionmaker
        from app.procurement.config import ProcurementSettings
        from app.procurement.context import ProcurementContext
        from app.procurement.internal import create_procurement_app

        self.mock = MockZovo()
        self.engine = make_engine(env.s.procurement_url, pool_size=2, max_overflow=1)
        self.vendor = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_mock_zovocard(self.mock))
        )
        self.ctx = ProcurementContext(
            ProcurementSettings(env="test", procurement_token=TOKEN),
            make_sessionmaker(self.engine),
            env.s.redis,
            environ=environ
            if environ is not None
            else {
                "ZOVOCARD_SANDBOX_APP_SECRET": OK_KEY,
                "ZOVOCARD_SANDBOX_BASE_URL": "http://mockzovo/openapi/v1",
            },
            http=self.vendor,
        )
        self.app = create_procurement_app(self.ctx)

    async def close(self) -> None:
        await self.vendor.aclose()
        await self.engine.dispose()


@pytest.fixture
async def proc(env):
    p = Proc(env)
    old_settings, old_client = env.res.settings, env.res.procurement
    env.res.settings = old_settings.model_copy(update={"procurement_token": TOKEN})
    env.res.procurement = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=p.app), base_url="http://procurement"
    )
    yield p
    await env.res.procurement.aclose()
    env.res.settings, env.res.procurement = old_settings, old_client
    await p.close()


async def _create(client, **over) -> dict:
    body = {"code": "zovocard", "adapter": "zovocard", "label": "ZovoCard", "env": "sandbox"}
    body.update(over)
    r = await client.post(URL, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _insert_supplier(env, tenant_id: uuid.UUID, *, enabled: bool = True) -> uuid.UUID:
    from app.models import SupplierAccount

    sid = uuid.uuid4()
    async with env.owner_session(tenant_id) as db:
        db.add(
            SupplierAccount(
                id=sid,
                tenant_id=tenant_id,
                code="zovocard",
                adapter="zovocard",
                label="Foreign",
                env="sandbox",
                enabled=enabled,
                settings={},
            )
        )
        await db.commit()
    return sid


# ---------------------------------------------------------------- admin API


async def test_create_list_update_audit(env):
    admin, _ = await env.login("admin")
    row = await _create(admin, min_spendable="5.5", priority=20)
    assert row["enabled"] is False  # default off
    assert row["health"] == "unknown" and row["min_spendable"] == "5.5"
    r = await admin.get(URL)
    body = r.json()
    assert [i["id"] for i in body["items"]] == [row["id"]]
    assert "mock" in body["adapters"] and body["production_locked"] is True
    # same code + env again: conflict; another env is a separate account
    dup = await admin.post(URL, json={"code": "zovocard", "adapter": "zovocard", "label": "x"})
    assert dup.status_code == 409
    await _create(admin, env="production", label="Live", current_password=PASSWORD)
    r = await admin.patch(f"{URL}/{row['id']}", json={"enabled": True, "label": "Sandbox"})
    assert r.status_code == 200 and r.json()["enabled"] is True and r.json()["label"] == "Sandbox"
    actions = await env.scalar(
        "SELECT array_agg(action ORDER BY id) FROM audit_log WHERE action LIKE 'supplier.%'"
    )
    assert actions == ["supplier.created", "supplier.created", "supplier.updated"]


async def test_validation_and_production_lock(env):
    admin, _ = await env.login("admin")
    for bad in (
        {"code": "Bad-Code", "adapter": "zovocard", "label": "x"},
        {"code": "ok_code", "adapter": "evil", "label": "x"},
        {"code": "ok_code", "adapter": "zovocard", "label": "x", "min_spendable": "1e9"},
        {"code": "ok_code", "adapter": "zovocard", "label": "x", "min_spendable": "-1"},
        {"code": "ok_code", "adapter": "zovocard", "label": "x", "env": "staging"},
    ):
        assert (await admin.post(URL, json=bad)).status_code == 422, bad
    # owner rule: production suppliers stay disabled until the security review
    r = await admin.post(
        URL,
        json={
            "code": "zovocard",
            "adapter": "zovocard",
            "label": "x",
            "env": "production",
            "enabled": True,
            "current_password": PASSWORD,
        },
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "production_locked"
    row = await _create(admin, env="production", current_password=PASSWORD)
    r = await admin.patch(f"{URL}/{row['id']}", json={"enabled": True})
    assert r.status_code == 409
    sandbox = await _create(admin, code="zovocard_other")
    await admin.patch(f"{URL}/{sandbox['id']}", json={"enabled": True})
    r = await admin.patch(
        f"{URL}/{sandbox['id']}", json={"env": "production", "current_password": PASSWORD}
    )
    assert r.status_code == 409  # enabled + production through the env switch, too


async def test_production_supplier_needs_current_password(env):
    admin, _ = await env.login("admin")
    base = {"code": "zovocard", "adapter": "zovocard", "label": "Live", "env": "production"}
    for body in (base, {**base, "current_password": "incorrect"}):
        r = await admin.post(URL, json=body)
        assert r.status_code == 403 and "incorrect" not in r.text
    assert (await admin.get(URL)).json()["items"] == []
    row = await _create(admin, env="production", current_password=PASSWORD)
    assert "current_password" not in row
    # sandbox needs no password; switching one to production does
    sb = await _create(admin, code="zovocard_two")
    r = await admin.patch(f"{URL}/{sb['id']}", json={"env": "production"})
    assert r.status_code == 403
    r = await admin.patch(f"{URL}/{sb['id']}", json={"env": "production", "current_password": "x"})
    assert r.status_code == 403
    assert (await admin.get(URL)).json()["items"][0]["env"] in ("sandbox", "production")
    r = await admin.patch(
        f"{URL}/{sb['id']}", json={"env": "production", "current_password": PASSWORD}
    )
    assert r.status_code == 200 and r.json()["env"] == "production"
    # an unrelated change to a production row needs no password; the password is never audited
    assert (await admin.patch(f"{URL}/{sb['id']}", json={"label": "L"})).status_code == 200
    n = await env.scalar(
        "SELECT count(*) FROM audit_log WHERE action LIKE 'supplier.%' "
        "AND (after::text LIKE :p OR detail::text LIKE :p)",
        p=f"%{PASSWORD}%",
    )
    assert n == 0


async def test_roles_and_cross_tenant_access(env, proc):
    admin, _ = await env.login("admin")
    agent, _ = await env.login("agent")
    assert (await agent.get(URL)).status_code == 403
    assert (
        await agent.post(URL, json={"code": "x1", "adapter": "manual", "label": "x"})
    ).status_code == 403
    foreign_admin, _ = await env.login("admin", tenant_id=env.other_tenant_id)
    assert (await foreign_admin.get(URL)).status_code in (401, 403, 404)

    # a supplier row of another tenant is invisible to this tenant's admin, and cannot be
    # changed or probed by id (RLS + explicit tenant filter)
    foreign = await _insert_supplier(env, env.other_tenant_id)
    assert (await admin.get(URL)).json()["items"] == []
    assert (await admin.patch(f"{URL}/{foreign}", json={"label": "pwned"})).status_code == 404
    assert (await admin.post(f"{URL}/{foreign}/probe")).status_code == 404
    label = await env.scalar(
        "SELECT label FROM suppliers WHERE id = :i", i=foreign
    )  # owner session is pinned to the platform tenant: RLS hides the row
    assert label is None
    # the procurement internal API refuses a cross-tenant pair as well
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proc.app), base_url="http://p"
    ) as c:
        r = await c.post(
            "/internal/probe",
            json={"tenant_id": str(env.tenant_id), "supplier_id": str(foreign)},
            headers={"X-Internal-Token": TOKEN},
        )
        assert r.status_code == 404
        # reseller tenants never get the platform's supplier secrets
        r = await c.post(
            "/internal/probe",
            json={"tenant_id": str(env.other_tenant_id), "supplier_id": str(foreign)},
            headers={"X-Internal-Token": TOKEN},
        )
        assert r.status_code == 404


async def test_reseller_tenant_admin_is_refused(env):
    admin, _ = await env.login("admin")
    async with env.owner_session(None) as db:
        await db.execute(
            text("UPDATE tenants SET kind = 'reseller' WHERE id = :t"), {"t": env.tenant_id}
        )
        await db.commit()
    try:
        assert (await admin.get(URL)).status_code == 403
    finally:
        async with env.owner_session(None) as db:
            await db.execute(
                text("UPDATE tenants SET kind = 'platform' WHERE id = :t"), {"t": env.tenant_id}
            )
            await db.commit()


# ---------------------------------------------------------------- probe through procurement


async def test_probe_round_trip_updates_health_and_audits(env, proc):
    admin, _ = await env.login("admin")
    row = await _create(admin)
    r = await admin.post(f"{URL}/{row['id']}/probe")
    assert r.status_code == 200, r.text
    report = r.json()["report"]
    assert report["ok"] is True and report["auth_ok"] is True and report["ip_allowed"] is True
    assert report["spendable_balance"] == "12.34" and report["purchasable_plans"] == 5
    assert report["permissions"] == {"direct": True, "cards": True}
    sup = r.json()["supplier"]
    assert sup["health"] == "ok" and sup["last_balance"] == "12.34" and sup["last_probe_at"]
    assert OK_KEY not in r.text
    assert await env.scalar("SELECT count(*) FROM audit_log WHERE action = 'supplier.probed'") == 1
    assert (
        await env.scalar("SELECT count(*) FROM audit_log WHERE action = 'supplier.health_changed'")
        == 1
    )
    # only GET requests reached the vendor
    assert {m for m, _p, _h in proc.mock.requests} == {"GET"}


async def test_probe_reports_vendor_failures(env, proc):
    admin, _ = await env.login("admin")
    row = await _create(admin)
    proc.mock.force("/balance", "403")
    r = await admin.post(f"{URL}/{row['id']}/probe")
    report = r.json()["report"]
    assert report["ok"] is False and report["ip_allowed"] is False
    assert report["errors"] == ["ip_not_allowlisted"]
    assert r.json()["supplier"]["health"] == "down"
    # an account without secrets in the procurement env
    prod = await _create(admin, env="production", current_password=PASSWORD)
    r = await admin.post(f"{URL}/{prod['id']}/probe")
    assert r.json()["report"]["errors"] == ["not_configured"]


async def test_probe_needs_procurement(env):
    admin, _ = await env.login("admin")
    row = await _create(admin)
    r = await admin.post(f"{URL}/{row['id']}/probe")
    assert r.status_code == 503 and r.json()["error"]["code"] == "procurement_off"


async def test_internal_api_token_and_rate_limit(env, proc):
    admin, _ = await env.login("admin")
    row = await _create(admin)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proc.app), base_url="http://p"
    ) as c:
        body = {"tenant_id": str(env.tenant_id), "supplier_id": row["id"]}
        assert (await c.post("/internal/probe", json=body)).status_code == 401
        bad = {"X-Internal-Token": "x" * 40}
        assert (await c.post("/internal/probe", json=body, headers=bad)).status_code == 401
        ok = {"X-Internal-Token": TOKEN}
        assert (await c.post("/internal/probe", json={"x": 1}, headers=ok)).status_code == 422
        codes = [
            (await c.post("/internal/probe", json=body, headers=ok)).status_code for _ in range(11)
        ]
        assert codes[:10] == [200] * 10 and codes[10] == 429


# ---------------------------------------------------------------- health loop


async def test_refresh_all_only_enabled_platform_suppliers(env, proc):
    from app.procurement.service import refresh_all

    admin, _ = await env.login("admin")
    on = await _create(admin, label="on")
    await admin.patch(f"{URL}/{on['id']}", json={"enabled": True})
    off = await _create(admin, code="zovocard_idle", label="off")
    await _insert_supplier(env, env.other_tenant_id, enabled=True)  # reseller tenant: skipped
    assert await refresh_all(proc.ctx) == 1
    items = {i["label"]: i for i in (await admin.get(URL)).json()["items"]}
    assert items["on"]["last_balance"] == "12.34" and items["on"]["health"] == "ok"
    assert items["off"]["last_balance"] is None and items["off"]["health"] == "unknown"
    assert off["id"]
    # a vendor outage degrades health instead of raising
    proc.mock.force("/balance", "500")
    await refresh_all(proc.ctx)
    items = {i["label"]: i for i in (await admin.get(URL)).json()["items"]}
    assert items["on"]["health"] == "degraded" and items["on"]["health_detail"] == "internal_error"
    assert Decimal(items["on"]["last_balance"]) == Decimal("12.34")  # last known value kept


# ---------------------------------------------------------------- grants


async def test_app_procurement_grants_are_narrow(env, proc):
    from app.core.db import tenant_session

    async with tenant_session(proc.ctx.maker, env.tenant_id) as db:
        for sql in (
            "UPDATE orders SET status = status",
            "SELECT 1 FROM deliveries",
            "SELECT blob FROM card_stock",
            # M2 (0024) grants INSERT on both, fenced by RESTRICTIVE policies: only already-sold
            # card rows and only the two procurement topics
            "INSERT INTO card_stock (tenant_id) VALUES (gen_random_uuid())",
            "INSERT INTO outbox (tenant_id, topic, key) VALUES (gen_random_uuid(), 'x', 'y')",
            "SELECT action FROM audit_log",
            "SELECT buyer_input FROM orders",
            "SELECT email FROM users",
            "UPDATE card_stock SET status = status",
        ):
            with pytest.raises(Exception, match=r"permission denied|row-level security"):
                await db.execute(text(sql))
            await db.rollback()
        assert (await db.execute(text("SELECT count(*) FROM procurement_jobs"))).scalar() == 0
        assert (await db.execute(text("SELECT count(id) FROM orders"))).scalar() == 0


async def test_job_constraints(env):
    p = await env.product()
    buyer = await env.user("buyer")
    async with env.owner_session() as db:
        oid = uuid.uuid4()
        await db.execute(
            text(
                "INSERT INTO orders (id, tenant_id, order_no, buyer_id, product_id, title, qty, "
                "amount_minor, subtotal_minor, currency, status, delivery_mode) VALUES "
                "(:i, :t, :n, :b, :p, 'x', 1, 100, 100, 'CNY', 'paid', 'auto_card')"
            ),
            {
                "i": oid,
                "t": env.tenant_id,
                "n": "G" + uuid.uuid4().hex[:12],
                "b": buyer.id,
                "p": p.id,
            },
        )
        ins = (
            "INSERT INTO procurement_jobs (tenant_id, order_id, unit_no, kind, idempotency_key, "
            "state) VALUES (:t, :o, 1, 'cdk', :k, :s)"
        )
        await db.execute(
            text(ins), {"t": env.tenant_id, "o": oid, "k": "pj:a:submit", "s": "voided"}
        )
        await db.execute(
            text(ins), {"t": env.tenant_id, "o": oid, "k": "pj:b:submit", "s": "queued"}
        )
        await db.commit()
        for key, state in (("pj:c:submit", "queued"), ("pj:b:submit", "voided"), ("bad key", "x")):
            with pytest.raises(Exception, match="violates"):
                await db.execute(text(ins), {"t": env.tenant_id, "o": oid, "k": key, "s": state})
            await db.rollback()


# ---------------------------------------------------------------- migration up / down

SCRATCH_DB = "g24_mig0023"


async def _alembic(owner_url: str, target: str, *, down: bool = False) -> None:
    from alembic import command
    from alembic.config import Config

    prev = os.environ.get("APP_MIGRATE_DATABASE_URL")
    os.environ["APP_MIGRATE_DATABASE_URL"] = owner_url
    try:
        cfg = Config(str(ALEMBIC_INI))
        fn = command.downgrade if down else command.upgrade
        await asyncio.to_thread(fn, cfg, target)
    finally:
        if prev is None:
            os.environ.pop("APP_MIGRATE_DATABASE_URL", None)
        else:
            os.environ["APP_MIGRATE_DATABASE_URL"] = prev


async def test_migration_0023_up_and_down(services):
    dsn = os.environ["TEST_DATABASE_URL"]
    plain = dsn.replace("postgresql+asyncpg://", "postgresql://")
    admin = await asyncpg.connect(plain, timeout=5)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {SCRATCH_DB} OWNER app_owner")
    finally:
        await admin.close()
    owner_url = _with_db(dsn, "app_owner", OWNER_PW, SCRATCH_DB)
    tables = [
        "suppliers",
        "supplier_offers",
        "procurement_jobs",
        "supplier_events",
        "supplier_ledger",
    ]
    try:
        await _alembic(owner_url, "0023_procurement")
        conn = await asyncpg.connect(owner_url.replace("+asyncpg", ""), timeout=5)
        try:
            rows = await conn.fetch(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = ANY($1)",
                tables,
            )
            assert {r["relname"] for r in rows} == set(tables)
            assert all(r["relrowsecurity"] and r["relforcerowsecurity"] for r in rows)
            role = await conn.fetchrow(
                "SELECT rolcanlogin, rolbypassrls, rolsuper FROM pg_roles "
                "WHERE rolname = 'app_procurement'"
            )
            assert role["rolcanlogin"] and not role["rolbypassrls"] and not role["rolsuper"]
        finally:
            await conn.close()
        await _alembic(owner_url, "0022_v11_content_ops", down=True)
        conn = await asyncpg.connect(owner_url.replace("+asyncpg", ""), timeout=5)
        try:
            left = await conn.fetchval(
                "SELECT count(*) FROM pg_class WHERE relname = ANY($1)", tables
            )
            assert left == 0
            grants = await conn.fetchval(
                "SELECT count(*) FROM information_schema.role_table_grants "
                "WHERE grantee = 'app_procurement'"
            )
            assert grants == 0
        finally:
            await conn.close()
        await _alembic(owner_url, "head")
    finally:
        admin = await asyncpg.connect(plain, timeout=5)
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
        finally:
            await admin.close()


async def test_supplier_code_rules_in_api_and_db(env):
    admin, _ = await env.login("admin")
    for code, adapter in (("acme", "zovocard"), ("zovocard_sandbox", "zovocard")):
        r = await admin.post(URL, json={"code": code, "adapter": adapter, "label": "x"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "supplier_code", r.text
    await _create(admin, code="zovocard_backup")
    await _create(admin, code="desk", adapter="manual")
    # the same rules hold in the database
    for code, adapter in (("acme", "zovocard"), ("x_sandbox", "manual")):
        async with env.owner_session() as db:
            with pytest.raises(Exception, match="violates check constraint"):
                await db.execute(
                    text(
                        "INSERT INTO suppliers (tenant_id, code, adapter, label) "
                        "VALUES (:t, :c, :a, 'x')"
                    ),
                    {"t": env.tenant_id, "c": code, "a": adapter},
                )


async def test_app_api_updates_only_editable_supplier_columns(env):
    sid = await _insert_supplier(env, env.tenant_id)
    async with env.api_session() as db:
        await db.execute(
            text("UPDATE suppliers SET label = 'ok', enabled = false WHERE id = :i"), {"i": sid}
        )
        await db.commit()
        for sql in (
            "UPDATE suppliers SET code = 'zovocard_x' WHERE id = :i",
            "UPDATE suppliers SET adapter = 'manual' WHERE id = :i",
            "UPDATE suppliers SET settings = '{}' WHERE id = :i",
            "UPDATE suppliers SET last_balance = 1 WHERE id = :i",
            "UPDATE suppliers SET tenant_id = tenant_id WHERE id = :i",
        ):
            with pytest.raises(Exception, match="permission denied"):
                await db.execute(text(sql), {"i": sid})
            await db.rollback()
