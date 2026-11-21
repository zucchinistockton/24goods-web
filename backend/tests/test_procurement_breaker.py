"""review-v12 M2: balance breaker (auto-recovering) vs the hard breaker (manual reset)."""

from __future__ import annotations

# ruff: noqa: F811  (fixtures are imported from test_procurement_m2)
import uuid
from decimal import Decimal

import httpx
from sqlalchemy import select, text

from app.devtools.mock_zovocard import OK_KEY, MockZovo, create_mock_zovocard
from app.suppliers.base import Money, OfferSpec
from app.suppliers.mock import MockSupplier
from app.suppliers.zovocard import TokenBucket, ZovoCardSupplier, ZovoClient, ZovoConfig
from tests.test_procurement_m2 import Up, up  # noqa: F401

URL = "/api/v1/admin/suppliers"


async def _sql(env, sql: str, **params):
    async with env.owner_session() as db:
        res = await db.execute(text(sql), params)
        await db.commit()
        return res


async def _refresh(up: Up, sid: uuid.UUID) -> None:
    from app.procurement.service import SupplierRef, refresh_balance

    ref = SupplierRef(sid, up.env.tenant_id, "mock_a", "mock", "sandbox", "unknown")
    await refresh_balance(up.ctx, ref)


async def _breaker(env, sid):
    return await env.scalar(
        "SELECT breaker_open || ':' || coalesce(breaker_kind, '-') || ':' || "
        "coalesce(breaker_reason, '-') FROM suppliers WHERE id = :s",
        s=sid,
    )


async def test_low_balance_opens_and_recovers_by_itself(env, up):
    mock = MockSupplier("mock_a", spendable=Decimal("10"), redemption=Money(Decimal("4"), "USD"))
    sid, _ = await up.supplier(mock)
    await _sql(env, "UPDATE suppliers SET min_spendable = 5 WHERE id = :s", s=sid)
    await up.paid_order()
    await up.run()  # 1 CDK issued: balance 9, reserve 4 -> 5 available, not below 5
    assert await _breaker(env, sid) == "false:-:-"
    await up.paid_order()
    await up.run()  # balance 8, reserve 8 -> 0 available < 5
    assert await _breaker(env, sid) == "true:balance:balance_low"
    audit = await env.scalar(
        "SELECT severity || ':' || (detail->>'reserved') || ':' || (detail->>'issued_unredeemed') "
        "FROM audit_log WHERE action = 'supplier.breaker_opened'"
    )
    assert audit == "critical:8:2"
    # stock disappears for new buyers while paused
    detail = (await env.client().get(f"/api/v1/products/{up.product.id}")).json()
    assert detail["stock"] == 0
    # one issued CDK gets redeemed upstream: its cost is in the balance already, not reserved
    await _sql(env, "UPDATE procurement_jobs SET upstream_status = 'consumed' WHERE unit_no = 1")
    mock.spendable = Decimal("9")
    await _refresh(up, sid)  # 9 - 4 = 5 -> not below 5 -> closes by itself
    assert await _breaker(env, sid) == "false:-:-"
    closed = await env.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'supplier.breaker_closed'"
    )
    assert closed == 1
    detail = (await env.client().get(f"/api/v1/products/{up.product.id}")).json()
    assert detail["stock"] > 0


async def test_hard_breaker_is_never_closed_by_the_balance(env, up):
    mock = MockSupplier("mock_a", spendable=Decimal("1000"))
    sid, _ = await up.supplier(mock)
    await _sql(
        env,
        "UPDATE suppliers SET breaker_open = true, breaker_kind = 'hard', "
        "breaker_reason = 'insufficient_balance', breaker_at = now() WHERE id = :s",
        s=sid,
    )
    await _refresh(up, sid)
    assert await _breaker(env, sid) == "true:hard:insufficient_balance"


async def test_refusal_upgrades_a_balance_breaker_to_hard(env, up):
    from app.core.db import tenant_session
    from app.models import ProcurementJob
    from app.procurement.engine import _open_breaker

    mock = MockSupplier("mock_a", spendable=Decimal("1000"))
    sid, _ = await up.supplier(mock)
    await up.paid_order()
    await up.run()
    await _sql(
        env,
        "UPDATE suppliers SET breaker_open = true, breaker_kind = 'balance', "
        "breaker_reason = 'balance_low', breaker_at = now() WHERE id = :s",
        s=sid,
    )
    async with tenant_session(up.ctx.maker, env.tenant_id) as db:
        job = await db.scalar(select(ProcurementJob))
        await _open_breaker(db, job, "request_failed")
        await db.commit()
    assert await _breaker(env, sid) == "true:hard:request_failed"
    await _refresh(up, sid)  # plenty of balance: still open
    assert await _breaker(env, sid) == "true:hard:request_failed"


async def test_unknown_cost_opens_the_breaker_and_a_configured_cost_closes_it(env, up):
    mock = MockSupplier("mock_a", spendable=Decimal("10"))  # supplier cannot price a redemption
    sid, oid = await up.supplier(mock)
    await _sql(env, "UPDATE supplier_offers SET cost_estimate = NULL WHERE id = :o", o=oid)
    await _sql(env, "UPDATE suppliers SET min_spendable = 5 WHERE id = :s", s=sid)
    await up.paid_order()
    await up.run()  # balance 9, plenty left, but the issued CDK has no known cost: fail closed
    assert await _breaker(env, sid) == "true:balance:unknown_cost"
    await _sql(
        env,
        "UPDATE supplier_offers SET cost_estimate = 3, currency = 'PHP' WHERE id = :o",
        o=oid,
    )
    await _refresh(up, sid)  # configured cost in another currency than the balance: unusable
    assert await _breaker(env, sid) == "true:balance:unknown_cost"
    await _sql(env, "UPDATE supplier_offers SET currency = 'USD' WHERE id = :o", o=oid)
    await _refresh(up, sid)  # 9 - 3 = 6 >= 5: the self-closing breaker recovers
    assert await _breaker(env, sid) == "false:-:-"
    await _sql(env, "UPDATE suppliers SET min_spendable = 9 WHERE id = :s", s=sid)
    await _refresh(up, sid)  # 6 < 9
    assert await _breaker(env, sid) == "true:balance:balance_low"


async def test_zovocard_redemption_cost_from_cached_plans():
    vendor = MockZovo()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_mock_zovocard(vendor))
    ) as http:
        s = ZovoCardSupplier(
            "zovocard",
            ZovoClient(
                ZovoConfig(base_url="http://m/openapi/v1", app_secret=OK_KEY),
                http=http,
                bucket=TokenBucket(6000, 100),
            ),
        )
        # USD plan: 2000 + max(2000 * 5%, 100) buffer + 15 service fee = 2115 minor
        usd = await s.redemption_cost(OfferSpec("cdk", {"product": "claude", "plan": "pro"}))
        assert usd == Money(Decimal("21.15"), "USD")
        # a PHP plan cannot be converted: the per-offer configured cost applies
        assert await s.redemption_cost(OfferSpec("cdk", {"plan": "plus"})) is None
        assert await s.redemption_cost(OfferSpec("cdk", {"plan": "nope"})) is None


async def test_reset_breaker_route_and_view(env):
    admin, _ = await env.login("admin")
    agent, _ = await env.login("agent")
    r = await admin.post(URL, json={"code": "zovocard", "adapter": "zovocard", "label": "Z"})
    sid = r.json()["id"]
    assert r.json()["breaker_open"] is False and r.json()["breaker_kind"] is None
    assert (await admin.post(f"{URL}/{sid}/reset-breaker")).status_code == 409
    await _sql(
        env,
        "UPDATE suppliers SET breaker_open = true, breaker_kind = 'hard', "
        "breaker_reason = 'x', breaker_at = now() WHERE id = :s",
        s=uuid.UUID(sid),
    )
    row = next(i for i in (await admin.get(URL)).json()["items"] if i["id"] == sid)
    assert row["breaker_open"] is True and row["breaker_kind"] == "hard"
    assert (await agent.post(f"{URL}/{sid}/reset-breaker")).status_code == 403
    r = await admin.post(f"{URL}/{sid}/reset-breaker")
    assert r.status_code == 200 and r.json()["breaker_open"] is False
    assert (
        await env.scalar("SELECT count(*) FROM audit_log WHERE action = 'supplier.breaker_reset'")
        == 1
    )
    # another tenant's supplier is not reachable
    foreign = uuid.uuid4()
    async with env.owner_session(env.other_tenant_id) as db:
        await db.execute(
            text(
                "INSERT INTO suppliers (id, tenant_id, code, adapter, label, env, breaker_open, "
                "breaker_kind) VALUES (:i, :t, 'zovocard', 'zovocard', 'F', 'sandbox', true, "
                "'hard')"
            ),
            {"i": foreign, "t": env.other_tenant_id},
        )
        await db.commit()
    assert (await admin.post(f"{URL}/{foreign}/reset-breaker")).status_code == 404


async def test_open_breaker_raises_one_staff_alert(env, up, monkeypatch):
    from app.core import notify_tg
    from app.core.db import tenant_session
    from app.modules.payments.reconcile import alert_supplier_breakers

    sid, _ = await up.supplier(MockSupplier("mock_a"))
    sent: list[dict] = []

    def fake(res, tid, kind, lines, **kw):
        sent.append(
            {
                "tid": tid,
                "lines": [notify_tg.render("en", x) for x in lines],
                "once": kw.get("once"),
            }
        )

    monkeypatch.setattr(notify_tg, "notify_bg", fake)
    async with tenant_session(env.res.maker, env.tenant_id) as db:
        assert await alert_supplier_breakers(env.res, db, env.tenant_id) == 0
    await _sql(
        env,
        "UPDATE suppliers SET breaker_open = true, breaker_kind = 'balance', "
        "breaker_reason = 'balance_low', breaker_at = now() WHERE id = :s",
        s=sid,
    )
    async with tenant_session(env.res.maker, env.tenant_id) as db:
        assert await alert_supplier_breakers(env.res, db, env.tenant_id) == 1
    assert len(sent) == 1 and "balance_low" in sent[0]["lines"][1]
    assert sent[0]["once"][0].startswith(f"supplier_breaker:{sid}:")


async def test_a_low_zovocard_wallet_alerts_staff_every_six_hours(env, up, monkeypatch):
    """ADR-049 (owner 2026-11-21): ZovoCard wallet below USD 60 (default) -> Telegram alert,
    one per 6-hour window while it stays low; 0 switches it off; a stale balance never alerts."""
    from datetime import datetime, timezone

    from app.core import notify_tg
    from app.core.db import tenant_session
    from app.modules.payments.reconcile import alert_wallet_low

    sid, _ = await up.supplier(MockSupplier("mock_a"))
    sent: list[dict] = []

    def fake(res, tid, kind, lines, **kw):
        sent.append({"lines": [notify_tg.render("en", x) for x in lines], "once": kw.get("once")})

    monkeypatch.setattr(notify_tg, "notify_bg", fake)
    now = datetime.now(timezone.utc)

    async def run() -> int:
        async with tenant_session(env.res.maker, env.tenant_id) as db:
            return await alert_wallet_low(env.res, db, env.tenant_id, now)

    await _sql(
        env,
        "UPDATE suppliers SET code = 'zovocard_low', adapter = 'zovocard', env = 'production', "
        "enabled = true, "
        "last_balance = 75, last_balance_at = now() WHERE id = :s",
        s=sid,
    )
    assert await run() == 0  # above the line
    await _sql(env, "UPDATE suppliers SET last_balance = 5.59 WHERE id = :s", s=sid)
    assert await run() == 1
    assert "5.59 USD" in sent[0]["lines"][0] and "60 USD" in sent[0]["lines"][0]
    assert sent[0]["once"][0].startswith(f"wallet_low:{sid}:") and sent[0]["once"][1] == 6 * 3600
    await _sql(
        env,
        "UPDATE tenants SET funding_settings = CAST(:j AS jsonb) WHERE id = :t",
        j='{"wallet_alert_usd": "0"}',
        t=env.tenant_id,
    )
    assert await run() == 0  # switched off
    await _sql(env, "UPDATE tenants SET funding_settings = NULL WHERE id = :t", t=env.tenant_id)
    await _sql(
        env,
        "UPDATE suppliers SET last_balance_at = now() - interval '3 hours' WHERE id = :s",
        s=sid,
    )
    assert await run() == 0  # a stale reading says nothing about now
