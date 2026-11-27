"""ADR-057: SMS verification codes (``upstream_sms``) end to end against a fake HeroSMS."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from app.suppliers.herosms import HeroSmsClient, HeroSmsConfig, HeroSmsSupplier
from app.suppliers.herosms import params as P
from tests.conftest import FP_KEY, VAULT_KID, VAULT_PUB_JSON
from tests.helpers import buy, pay_order

CODE = "482913"


class FakeHero:
    """Stateful HeroSMS: balance, offers, buy (NO_NUMBERS per country), open list, codes, cancel
    (refused while ``early``), finish."""

    def __init__(self) -> None:
        self.balance = Decimal("10")
        self.no_numbers: set[int] = set()
        self.early = False
        self.open: dict[str, dict] = {}
        self.codes: dict[str, list[str]] = {}
        self.calls: list[str] = []
        self.n = 0
        self.prices = {182: Decimal("0.455"), 52: Decimal("0.13"), 187: Decimal("0.715")}

    def give_code(self, ext: str, code: str = CODE) -> None:
        self.codes.setdefault(ext, []).append(code)

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append(f"{req.method} {path}")
        if path == "/stubs/handler_api.php":
            return httpx.Response(200, text=f"ACCESS_BALANCE:{self.balance}")
        if path == "/api/v1/activations/offers/sms":
            data = {
                str(c): {"prices": {"min": float(p), "retail": float(p)}, "counts": {"total": 50}}
                for c, p in self.prices.items()
            }
            return httpx.Response(200, json={"data": {"dr": data}})
        if path == "/api/v1/activations" and req.method == "POST":
            body = json.loads(req.content)
            c = body["country"]
            if c in self.no_numbers:
                return httpx.Response(400, json={"title": "NO_NUMBERS", "details": "none"})
            price = self.prices[c]
            if Decimal(str(body["maxPrice"])) < price:
                return httpx.Response(400, json={"title": "NO_NUMBERS", "details": "price"})
            self.n += 1
            ext = str(1000 + self.n)
            item = {"id": int(ext), "phone": f"81701234{self.n:04d}", "country": c,
                    "operator": "rakuten_mobile", "price": float(price), "status": 4,
                    "expiredAt": "2030-01-01T00:00:00Z"}  # fmt: skip
            self.open[ext] = item
            self.balance -= price
            return httpx.Response(200, json={"data": [item]})
        if path == "/api/v1/activations" and req.method == "GET":
            items = [
                {
                    **it,
                    "otpList": [
                        {"id": i, "smsCode": c} for i, c in enumerate(self.codes.get(k, []))
                    ],
                }
                for k, it in self.open.items()
            ]
            return httpx.Response(200, json={"data": items, "meta": {"hasMore": False}})
        m = re.fullmatch(r"/api/v1/activations/(\d+)(/otp|/finish)?", path)
        if m:
            ext, tail = m.group(1), m.group(2)
            if tail == "/otp":
                return httpx.Response(
                    200, json={"data": [{"smsCode": c} for c in self.codes.get(ext, [])]}
                )
            if tail == "/finish":
                self.open.pop(ext, None)
                return httpx.Response(204)
            if req.method == "DELETE":
                if self.early:
                    return httpx.Response(409, json={"title": "EARLY_CANCEL_DENIED", "details": ""})
                it = self.open.pop(ext, None)
                if it is not None and not self.codes.get(ext):
                    self.balance += Decimal(str(it["price"]))  # refunded
                return httpx.Response(204)
        return httpx.Response(404, json={"title": "NOT_FOUND"})


class Sms:
    def __init__(self, env) -> None:
        self.env = env
        self.fake = FakeHero()
        self.http = httpx.AsyncClient(
            transport=httpx.MockTransport(self.fake.handler), base_url="http://hero"
        )

    async def start(self, **params) -> None:
        from app.core.db import make_engine, make_sessionmaker
        from app.models import SupplierAccount
        from app.procurement.config import ProcurementSettings
        from app.procurement.context import ProcurementContext
        from app.procurement.internal import create_procurement_app
        from app.procurement.keys import build_sealer

        self.product = await self.env.product(
            mode="upstream_sms", title="OpenAI SMS code", price=800, floor=500
        )
        self.engine = make_engine(self.env.s.procurement_url, pool_size=3, max_overflow=2)
        cfg = HeroSmsConfig(base_url="http://hero", api_key="k" * 20, env="sandbox")
        self.ctx = ProcurementContext(
            ProcurementSettings(env="test", procurement_token="t" * 40),
            make_sessionmaker(self.engine),
            self.env.s.redis,
            environ={},
            sealer=build_sealer(
                public_keys_json=VAULT_PUB_JSON, active_kid=VAULT_KID, fp_key=FP_KEY
            ),
            supplier_factory=lambda ref: HeroSmsSupplier(
                ref.code, HeroSmsClient(cfg, http=self.http)
            ),
        )
        self.sid = uuid.uuid4()
        async with self.env.owner_session() as db:
            db.add(
                SupplierAccount(
                    id=self.sid, tenant_id=self.env.tenant_id, code="herosms", adapter="herosms",
                    label="HeroSMS", env="sandbox", enabled=True, priority=10, settings={},
                    last_balance=Decimal("10"), last_balance_at=datetime.now(timezone.utc),
                )
            )  # fmt: skip
            await db.commit()
        self._old = (self.env.res.settings, self.env.res.procurement)
        self.env.res.settings = self._old[0].model_copy(update={"procurement_token": "t" * 40})
        self.env.res.procurement = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_procurement_app(self.ctx)),
            base_url="http://procurement",
        )
        self.admin, _ = await self.env.login("admin")
        await self.configure(**params)
        # clear cooling flags from earlier tests
        for k in await self.env.s.redis.keys("rl:proc:sms:*"):
            await self.env.s.redis.delete(k)

    async def configure(self, enabled: bool = True, **params) -> httpx.Response:
        body = {**P.defaults(), **params}
        r = await self.admin.put(
            f"/api/v1/admin/sms/products/{self.product.id}",
            json={"supplier_id": str(self.sid), "enabled": enabled, "params": body},
        )
        assert r.status_code == 200, r.text
        await self.refresh()
        return r

    async def refresh(self):
        from app.procurement import sms

        return await sms.refresh_one(self.ctx, self.env.tenant_id, self.product.id)

    async def close(self) -> None:
        await self.env.res.procurement.aclose()
        self.env.res.settings, self.env.res.procurement = self._old
        await self.http.aclose()
        await self.engine.dispose()

    async def paid(self):
        from app.procurement.engine import drain

        buyer, _ = await self.env.login("buyer")
        order = await buy(buyer, self.product, 1)
        await pay_order(self.env, buyer, order)
        assert await self.env.drain() >= 1  # order.paid -> procure.requested
        await drain(self.ctx)  # procure.requested -> sms session
        return buyer, order

    async def age_numbers(self, seconds: int) -> None:
        async with self.env.owner_session() as db:
            await db.execute(
                text(
                    "UPDATE sms_attempts SET bought_at = bought_at - make_interval(secs => :s) "
                    "WHERE state = 'waiting'"
                ),
                {"s": seconds},
            )
            await db.commit()


@pytest.fixture
async def sms_env(env):
    s = Sms(env)
    await s.start()
    yield s
    await s.close()


async def _status(buyer, order) -> str:
    return (await buyer.get(f"/api/v1/orders/{order['id']}")).json()["status"]


async def test_sms_order_end_to_end(env, sms_env):
    from app.procurement import sms

    s = sms_env
    detail = (await env.client().get(f"/api/v1/products/{s.product.id}")).json()
    assert detail["delivery_mode"] == "upstream_sms" and detail["stock"] > 0
    buyer, order = await s.paid()
    view = (await buyer.get(f"/api/v1/orders/{order['id']}/sms")).json()
    assert view["state"] == "ready" and view["current"] is None and view["max_attempts"] == 5
    r = await buyer.post(f"/api/v1/orders/{order['id']}/sms/number")
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["state"] == "active" and view["current"]["phone"].startswith("8170")
    assert view["attempts_used"] == 1 and view["can_swap_in_s"] > 0
    ext = next(iter(s.fake.open))
    assert await sms.poll_once(s.ctx) == 1  # no code yet
    s.fake.give_code(ext)
    assert await sms.poll_once(s.ctx) == 0
    assert await env.drain() >= 1  # procure.fulfilled -> delivered
    assert await _status(buyer, order) == "delivered"
    cards = (await buyer.post(f"/api/v1/orders/{order['id']}/reveal")).json()["cards"]
    assert cards[0]["code"] == f"{CODE}----+{view['current']['phone']}"
    assert f"POST /api/v1/activations/{ext}/finish" in s.fake.calls
    # the code is nowhere in plaintext: SMS tables, audit, outbox
    for sql in (
        "SELECT count(*) FROM sms_attempts WHERE row_to_json(sms_attempts)::text LIKE :c",
        "SELECT count(*) FROM sms_sessions WHERE row_to_json(sms_sessions)::text LIKE :c",
        "SELECT count(*) FROM audit_log WHERE row_to_json(audit_log)::text LIKE :c",
        "SELECT count(*) FROM outbox WHERE payload::text LIKE :c",
    ):
        assert await env.scalar(sql, c=f"%{CODE}%") == 0
    # nor the phone number in audit rows
    assert (
        await env.scalar(
            "SELECT count(*) FROM audit_log WHERE detail::text LIKE :p",
            p=f"%{view['current']['phone']}%",
        )
        == 0
    )
    stats = (await s.admin.get("/api/v1/admin/sms/stats")).json()
    assert stats["routes"][0]["country"] == 182 and stats["routes"][0]["codes"] == 1
    assert stats["sessions"]["delivered"] == 1


async def test_route_fallback_cooling_and_swap(env, sms_env):
    s = sms_env
    s.fake.no_numbers = {182}
    buyer, order = await s.paid()
    view = (await buyer.post(f"/api/v1/orders/{order['id']}/sms/number")).json()
    assert view["attempts_used"] == 1
    assert await env.scalar("SELECT country FROM sms_attempts") == 52  # Thailand next
    assert await env.s.redis.get(f"rl:proc:sms:cool:{s.product.id}:182")
    # too early to swap
    r = await buyer.post(f"/api/v1/orders/{order['id']}/sms/number")
    assert r.status_code == 409 and r.json()["error"]["code"] == "sms_too_early"
    await s.age_numbers(130)
    r = await buyer.post(f"/api/v1/orders/{order['id']}/sms/number")
    assert r.status_code == 200, r.text
    states = await env.scalar("SELECT array_agg(state ORDER BY seq) FROM sms_attempts")
    assert states == ["no_code", "waiting"]
    assert s.fake.balance == Decimal("10") - Decimal("0.13")  # the first number was refunded
    # the provider refusing an early cancel is "too early", nothing changes
    await s.age_numbers(130)
    s.fake.early = True
    r = await buyer.post(f"/api/v1/orders/{order['id']}/sms/number")
    assert r.status_code == 409 and r.json()["error"]["code"] == "sms_too_early"
    assert await env.scalar("SELECT count(*) FROM sms_attempts") == 2


async def test_exhausted_session_goes_to_manual_review(env, sms_env):
    from app.procurement import sms

    s = sms_env
    await s.configure(max_attempts=1, number_wait_s=120)
    buyer, order = await s.paid()
    assert (await buyer.post(f"/api/v1/orders/{order['id']}/sms/number")).status_code == 200
    await s.age_numbers(125)
    assert await sms.poll_once(s.ctx) == 0  # cancelled for no code
    assert await env.scalar("SELECT state FROM sms_attempts") == "no_code"
    r = await buyer.post(f"/api/v1/orders/{order['id']}/sms/number")
    assert r.status_code == 409 and r.json()["error"]["code"] == "sms_attempts_exhausted"
    assert await sms.expire_sessions(s.ctx) == 1
    assert await env.scalar("SELECT fail_reason FROM sms_sessions") == "attempts_exhausted"
    assert await env.drain() >= 1  # procure.failed -> manual_review
    assert await _status(buyer, order) == "manual_review"
    staff = (await s.admin.get(f"/api/v1/admin/sms/orders/{order['id']}")).json()
    assert staff["session"]["state"] == "failed" and "*" in staff["attempts"][0]["phone"]


async def test_not_started_in_time_fails(env, sms_env):
    from app.procurement import sms

    s = sms_env
    buyer, order = await s.paid()
    async with env.owner_session() as db:
        await db.execute(text("UPDATE sms_sessions SET start_deadline = now() - interval '1 s'"))
        await db.commit()
    assert await sms.expire_sessions(s.ctx) == 1
    assert await env.drain() >= 1
    assert await _status(buyer, order) == "manual_review"


async def test_checkout_guards_and_stock(env, sms_env):
    s = sms_env
    await s.configure(per_buyer_daily=1)
    buyer, _ = await env.login("buyer")
    r = await buyer.post("/api/v1/orders", json={"product_id": str(s.product.id), "qty": 2})
    assert r.status_code == 422 and r.json()["error"]["code"] == "sms_qty_one"
    assert (
        await buyer.post("/api/v1/orders", json={"product_id": str(s.product.id), "qty": 1})
    ).status_code == 200
    r = await buyer.post("/api/v1/orders", json={"product_id": str(s.product.id), "qty": 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "sms_daily_limit"
    # balance too low for one more session -> sold out
    s.fake.balance = Decimal("0.10")
    ok, reason = await s.refresh()
    assert not ok and reason == "low_balance"
    other, _ = await env.login("buyer")
    r = await other.post("/api/v1/orders", json={"product_id": str(s.product.id), "qty": 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "out_of_stock"
    # every route cooling -> sold out
    s.fake.balance = Decimal("10")
    for c in (182, 52, 187):
        await env.s.redis.set(f"rl:proc:sms:cool:{s.product.id}:{c}", "1", ex=60)
    assert await s.refresh() == (False, "no_route")
    # disabled -> sold out
    await env.s.redis.delete(*[f"rl:proc:sms:cool:{s.product.id}:{c}" for c in (182, 52, 187)])
    await s.configure(enabled=False)
    detail = (await env.client().get(f"/api/v1/products/{s.product.id}")).json()
    assert detail["stock"] == 0


async def test_admin_config_validation_and_cross_tenant(env, sms_env):
    s = sms_env
    bad = {**P.defaults(), "max_attempts": 99}
    r = await s.admin.put(
        f"/api/v1/admin/sms/products/{s.product.id}",
        json={"supplier_id": str(s.sid), "enabled": True, "params": bad},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "sms_bad_params"
    bad = {**P.defaults(), "routes": [{"country": 182, "max_price": "9"}]}
    r = await s.admin.put(
        f"/api/v1/admin/sms/products/{s.product.id}",
        json={"supplier_id": str(s.sid), "enabled": True, "params": bad},
    )
    assert r.status_code == 422
    manual = await env.product(mode="manual")
    r = await s.admin.put(
        f"/api/v1/admin/sms/products/{manual.id}",
        json={"supplier_id": str(s.sid), "enabled": True, "params": P.defaults()},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "sms_wrong_mode"
    # another tenant's admin sees neither the product nor the order
    foreign, _ = await env.login("admin", tenant_id=env.other_tenant_id)
    denied = (401, 403, 404)
    assert (await foreign.get(f"/api/v1/admin/sms/products/{s.product.id}")).status_code in denied
    _buyer, order = await s.paid()
    assert (await foreign.get(f"/api/v1/admin/sms/orders/{order['id']}")).status_code in denied
    # ... and this tenant's admin cannot reach another tenant's product
    theirs = await env.product(mode="upstream_sms", tenant_id=env.other_tenant_id)
    assert (await s.admin.get(f"/api/v1/admin/sms/products/{theirs.id}")).status_code == 404
    r = await s.admin.put(
        f"/api/v1/admin/sms/products/{theirs.id}",
        json={"supplier_id": str(s.sid), "enabled": True, "params": P.defaults()},
    )
    assert r.status_code == 404
    # another buyer cannot read or drive the order
    stranger, _ = await env.login("buyer")
    assert (await stranger.get(f"/api/v1/orders/{order['id']}/sms")).status_code == 404
    assert (await stranger.post(f"/api/v1/orders/{order['id']}/sms/number")).status_code == 404
    assert await env.scalar("SELECT count(*) FROM sms_attempts") == 0


async def test_params_normalizer():
    routes = [{"country": 182, "max_price": "0.6"}, {"country": 182, "max_price": "1"}]
    p = P.normalize({"routes": routes, "max_attempts": 0, "service": "BAD!"})
    assert p["routes"] == [{"country": 182, "max_price": "0.6000"}]
    assert p["max_attempts"] == 1 and p["service"] == "dr"
    assert P.normalize(None)["routes"][0]["country"] == 182
    assert P.max_cap(P.defaults()) == Decimal("0.85")
