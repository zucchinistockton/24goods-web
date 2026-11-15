"""Payment-audit fixes on v12 M2 (2026-11-15): H1, H2, M1-M5 and the low findings."""

from __future__ import annotations

import asyncio
import os

import asyncpg
from sqlalchemy import text

from tests.conftest import ALEMBIC_INI, OWNER_PW, _with_db
from tests.test_procurement_m2 import Up, up  # noqa: F401


async def _sql(env, sql: str, **params):
    async with env.owner_session() as db:
        res = await db.execute(text(sql), params)
        await db.commit()
        return res


# ------------------------------------------------------------------ migration 0027


async def test_migration_0027_up_and_down(services):
    from alembic import command
    from alembic.config import Config

    db_name = "g24_mig0027"
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

    cols = ("maybe_sent", "recover", "lease_until", "first_sent_at")

    async def state() -> tuple[set[str], bool]:
        conn = await asyncpg.connect(owner_url.replace("+asyncpg", ""), timeout=5)
        try:
            have = {
                r["column_name"]
                for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'procurement_jobs'"
                )
            }
            force = await conn.fetchval(
                "SELECT relforcerowsecurity FROM pg_class WHERE relname = 'procurement_jobs'"
            )
            return have & set(cols), bool(force)
        finally:
            await conn.close()

    async def priv(role: str, col: str, kind: str) -> bool:
        conn = await asyncpg.connect(owner_url.replace("+asyncpg", ""), timeout=5)
        try:
            return bool(
                await conn.fetchval(
                    "SELECT has_column_privilege($1, 'procurement_jobs', $2, $3)", role, col, kind
                )
            )
        finally:
            await conn.close()

    try:
        await alembic("head")
        have, force = await state()
        assert have == set(cols) and force
        assert await priv("app_api", "recover", "UPDATE")
        assert not await priv("app_api", "maybe_sent", "UPDATE")  # engine-owned
        assert not await priv("app_api", "lease_until", "UPDATE")
        assert await priv("app_delivery", "state", "SELECT")
        assert not await priv("app_delivery", "upstream_ref", "SELECT")
        assert not await priv("app_delivery", "state", "UPDATE")
        await alembic("0026_procurement_admin_actions", down=True)
        have, force = await state()
        assert have == set() and force
        assert not await priv("app_delivery", "state", "SELECT")
        await alembic("head")
        assert (await state())[0] == set(cols)
    finally:
        admin = await asyncpg.connect(plain, timeout=5)
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")
        finally:
            await admin.close()
