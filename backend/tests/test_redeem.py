"""review-v12 §8: the public CDK redemption proxy (app/procurement/redeem.py) against the mock."""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from dataclasses import replace

import httpx
import pytest

from app.devtools.mock_zovocard import OK_KEY, MockZovo, create_mock_zovocard, plans_fixture
from app.procurement.config import ProcurementSettings
from app.procurement.redeem import (
    ERRORS,
    create_redeem_app,
    map_upstream_error,
    parse_limits,
)

DEVICE = "redeem-device-" + secrets.token_hex(4)
TOKEN = secrets.token_urlsafe(32)
SESSION = "eyJ" + secrets.token_urlsafe(60)  # stands in for the buyer's ChatGPT session
PLAN = next(r["key"] for r in plans_fixture("gpt")["registry"] if r.get("purchasable") is True)
ENVIRON = {
    "ZOVOCARD_SANDBOX_APP_SECRET": OK_KEY,
    "ZOVOCARD_SANDBOX_BASE_URL": "http://mockzovo/openapi/v1",
}


class Rig:
    def __init__(self, env, vendor: httpx.AsyncClient | None = None, **over) -> None:
        from app.core.db import make_engine, make_sessionmaker
        from app.procurement.context import ProcurementContext

        self.env = env
        self.mock = MockZovo()
        self.engine = make_engine(env.s.procurement_url, pool_size=2, max_overflow=1)
        self.vendor = vendor or httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_mock_zovocard(self.mock))
        )
        base = ProcurementSettings(
            env="test",
            procurement_token=TOKEN,
            redeem_enabled=True,
            redeem_device=DEVICE,
            redeem_ip_limits="preview=50,preflight=50,redeem=50,result=50",
            redeem_global_limits="preview=500,preflight=500,redeem=500,result=500",
            redeem_code_per_10min=50,
            redeem_token_per_min=50,
        )
        self.settings = replace(base, **over)
        self.ctx = ProcurementContext(
            self.settings,
            make_sessionmaker(self.engine),
            env.s.redis,
            environ=dict(ENVIRON),
            http=self.vendor,
        )
        self.app = create_redeem_app(self.ctx)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://redeem.test"
        )

    def add_cdk(self, plan: str = PLAN, status: str = "unused") -> str:
        cid = 5000 + len(self.mock.cdks)
        code = f"MOCKPLUS-{secrets.token_hex(5).upper()}-{secrets.token_hex(5).upper()}"
        self.mock.cdks[cid] = {"id": cid, "code": code, "plan": plan, "status": status}
        return code

    async def post(self, step: str, body, **kw) -> httpx.Response:
        return await self.client.post(f"/redeem/{step}", json=body, **kw)

    async def walk(self, code: str | None = None):
        """preview -> preflight -> redeem -> result; returns (code, results)."""
        code = code or self.add_cdk()
        r1 = await self.post("preview", {"code": code})
        assert r1.status_code == 200, r1.text
        tok = r1.json()["data"]["redemption_token"]
        cred = {"mode": "session", "session": SESSION}
        r2 = await self.post("preflight", {"redemption_token": tok, "credential": cred})
        assert r2.status_code == 200, r2.text
        pf = r2.json()["data"]["preflight_token"]
        r3 = await self.post(
            "redeem",
            {
                "redemption_token": tok,
                "preflight_token": pf,
                "client_request_id": "req-" + tok[3:15],
            },
        )
        assert r3.status_code == 200, r3.text
        r4 = await self.client.post("/redeem/result", json={"redemption_token": tok})
        return code, (r1, r2, r3, r4), tok, pf

    async def close(self) -> None:
        await self.client.aclose()
        await self.vendor.aclose()
        await self.engine.dispose()


async def _supplier(env, *, env_name: str = "sandbox", enabled: bool = True) -> None:
    from app.models import SupplierAccount

    async with env.owner_session(env.tenant_id) as db:
        db.add(
            SupplierAccount(
                id=uuid.uuid4(),
                tenant_id=env.tenant_id,
                code="zovocard",
                adapter="zovocard",
                label="ZovoCard",
                env=env_name,
                enabled=enabled,
                settings={},
            )
        )
        await db.commit()


@pytest.fixture
async def rig(env):
    await _supplier(env)
    r = Rig(env)
    yield r
    await r.close()


def _err(resp: httpx.Response) -> str:
    return resp.json()["error"]["code"]


# ---------------------------------------------------------------- happy path


async def test_happy_path_four_steps(rig):
    code, (r1, r2, r3, r4), tok, pf = await rig.walk()
    d1, d2, d3, d4 = (r.json()["data"] for r in (r1, r2, r3, r4))
    assert set(d1) == {"redemption_token", "expires_at", "plan", "plan_flow", "funding_cap_minor"}
    assert d1["plan"] == PLAN and d1["redemption_token"] == tok
    assert d2["payment_country"] == "PH" and d2["payment_currency"] == "PHP"
    assert d2["preflight_token"] == pf and d2["subscription_recovery_required"] is False
    assert d3["status"] == "succeeded" and d3["order_id"].startswith("SBXORD-")
    assert d3["terminal"] is True
    assert d4["order_id"] == d3["order_id"] and d4["events"][0]["type"] == "order.succeeded"
    # the vendor saw the same constant device on all four steps
    assert [c[0] for c in rig.mock.redeem_calls] == ["preview", "preflight", "redeem", "result"]
    assert {c[1] for c in rig.mock.redeem_calls} == {DEVICE}
    # nothing of the credential or the code comes back
    blob = "".join(r.text for r in (r1, r2, r3, r4))
    assert SESSION not in blob and code not in blob


async def test_result_post_and_grok_credential_flow(rig):
    code = rig.add_cdk(plan="grok_premium_monthly")
    r1 = await rig.post("preview", {"code": code})
    tok = r1.json()["data"]["redemption_token"]
    cred = {"mode": "session", "session": SESSION}
    r2 = await rig.post("preflight", {"redemption_token": tok, "credential": cred})
    assert r2.json()["data"]["preflight_token"] is None  # vendor: no token for Grok/Claude
    r3 = await rig.post("redeem", {"redemption_token": tok, "client_request_id": "g-1"})
    assert _err(r3) == "invalid_request"  # neither preflight token nor credential
    r3 = await rig.post(
        "redeem", {"redemption_token": tok, "client_request_id": "g-1", "credential": cred}
    )
    assert r3.status_code == 200
    r4 = await rig.post("result", {"redemption_token": tok})
    assert r4.status_code == 200 and r4.json()["data"]["status"] == "succeeded"


async def test_redeem_replay_and_conflict(rig):
    _, _, tok, pf = await rig.walk()
    body = {"redemption_token": tok, "preflight_token": pf, "client_request_id": "req-" + tok[3:15]}
    again = await rig.post("redeem", body)
    assert again.status_code == 200  # same request id and parameters: the original order
    other = await rig.post("redeem", {**body, "preflight_token": "pf_" + "0" * 32})
    assert _err(other) == "conflict"


async def test_result_404_is_not_found(env):
    await _supplier(env)
    r = Rig(env)
    r.mock.result_404 = True
    try:
        _, (_, _, _, r4), _, _ = await r.walk()
        assert r4.status_code == 404 and _err(r4) == "not_found"
        unknown = await r.client.post("/redeem/result", json={"redemption_token": "rt_" + "a" * 32})
        assert _err(unknown) == "not_found"
    finally:
        await r.close()


async def test_unknown_disabled_used_codes(rig):
    for code in (
        "MOCKPLUS-DOESNOTEXIST",
        rig.add_cdk(status="disabled"),
        rig.add_cdk(status="consumed"),
    ):
        r = await rig.post("preview", {"code": code})
        assert r.status_code == 422 and _err(r) == "code_unavailable"
        assert code not in r.text


async def test_wrong_credential_and_expired_session(rig):
    code = rig.add_cdk()
    tok = (await rig.post("preview", {"code": code})).json()["data"]["redemption_token"]
    bad = {"mode": "session", "session": "this-session-is-invalid-xyz"}
    r = await rig.post("preflight", {"redemption_token": tok, "credential": bad})
    assert _err(r) == "credential_invalid" and "invalid-xyz" not in r.text
    r = await rig.post(
        "preflight",
        {
            "redemption_token": "rt_" + "f" * 32,
            "credential": {"mode": "session", "session": SESSION},
        },
    )
    assert r.status_code == 409 and _err(r) == "session_expired"
    r = await rig.post(
        "redeem",
        {"redemption_token": tok, "client_request_id": "x-1", "preflight_token": "pf_" + "1" * 32},
    )
    assert _err(r) == "session_expired"  # no preflight happened for this token


# ---------------------------------------------------------------- input validation


@pytest.mark.parametrize(
    "step,body",
    [
        ("preview", {}),
        ("preview", {"code": ""}),
        ("preview", {"code": "short"}),
        ("preview", {"code": "A" * 129}),
        ("preview", {"code": "MOCKPLUS-ABC DEF-1234"}),  # space
        ("preview", {"code": "MOCKPLUS-ABCéDEF"}),  # non-ASCII
        ("preview", {"code": 12345678}),
        ("preview", {"code": ["MOCKPLUS-ABCDEFGH"]}),
        ("preview", {"code": "MOCKPLUS-ABCDEFGH", "extra": 1}),  # unknown key
        ("preview", [1, 2]),
        ("preflight", {"redemption_token": "rt_" + "a" * 32}),  # no credential
        ("preflight", {"redemption_token": "bad token!", "credential": {}}),
        (
            "preflight",
            {
                "redemption_token": "rt_" + "a" * 32,
                "credential": {"mode": "email", "session": SESSION},
            },
        ),
        (
            "preflight",
            {
                "redemption_token": "rt_" + "a" * 32,
                "credential": {"mode": "session", "session": SESSION, "x": 1},
            },
        ),
        (
            "preflight",
            {
                "redemption_token": "rt_" + "a" * 32,
                "credential": {"mode": "session", "session": "x" * 65537},
            },
        ),
        (
            "preflight",
            {
                "redemption_token": "rt_" + "a" * 32,
                "credential": {"mode": "session", "session": "ab\x00cdefghij"},
            },
        ),
        ("redeem", {"redemption_token": "rt_" + "a" * 32, "preflight_token": "pf_" + "b" * 32}),
        ("redeem", {"redemption_token": "rt_" + "a" * 32, "client_request_id": "id-1"}),
        (
            "redeem",
            {
                "redemption_token": "rt_" + "a" * 32,
                "preflight_token": "pf_" + "b" * 32,
                "client_request_id": "x" * 81,
            },
        ),
        (
            "redeem",
            {
                "redemption_token": "rt_" + "a" * 32,
                "preflight_token": "pf_" + "b" * 32,
                "client_request_id": "has space",
            },
        ),
        (
            "redeem",
            {
                "redemption_token": "rt_" + "a" * 32,
                "preflight_token": "pf_" + "b" * 32,
                "client_request_id": "id-1",
                "card_id": 7,
            },
        ),
        ("result", {"redemption_token": "x"}),
        ("result", {}),
    ],
)
async def test_invalid_input_never_reaches_the_vendor(rig, step, body):
    r = await rig.post(step, body)
    assert r.status_code == 400 and _err(r) == "invalid_request"
    assert rig.mock.redeem_calls == []


async def test_body_cap_content_type_and_bad_json(rig):
    big = json.dumps({"code": "MOCKPLUS-ABCDEFGH", "pad": "x" * 100000})
    r = await rig.client.post(
        "/redeem/preview", content=big, headers={"content-type": "application/json"}
    )
    assert r.status_code == 413 and _err(r) == "payload_too_large"

    async def chunks():  # no Content-Length: the streaming cap must hold too
        for _ in range(120):
            yield b"x" * 1024

    r = await rig.client.post(
        "/redeem/preview", content=chunks(), headers={"content-type": "application/json"}
    )
    assert r.status_code == 413
    r = await rig.client.post(
        "/redeem/preview", content="{not json", headers={"content-type": "application/json"}
    )
    assert _err(r) == "invalid_request"
    r = await rig.client.post(
        "/redeem/preview",
        content='{"code":"MOCKPLUS-ABCDEFGH"}',
        headers={"content-type": "text/plain"},
    )
    assert _err(r) == "invalid_request"
    r = await rig.client.post(
        "/redeem/result", json={"redemption_token": "rt_" + "a" * 32, "x": "1"}
    )
    assert _err(r) == "invalid_request"
    assert rig.mock.redeem_calls == []


# ---------------------------------------------------------------- rate limits


async def test_per_ip_rate_limit_and_xff_spoofing(env):
    await _supplier(env)
    r = Rig(env, redeem_ip_limits="preview=2,preflight=2,redeem=2,result=2")
    try:
        code = r.add_cdk()
        for _ in range(2):
            assert (await r.post("preview", {"code": code})).status_code == 200
        blocked = await r.post("preview", {"code": code})
        assert blocked.status_code == 429 and _err(blocked) == "rate_limited"
        assert int(blocked.headers["retry-after"]) >= 1
        # X-Forwarded-For is ignored when no trusted proxy is configured: no evasion by spoofing
        spoof = await r.post("preview", {"code": code}, headers={"x-forwarded-for": "198.51.100.9"})
        assert spoof.status_code == 429
        # other steps have their own per-IP budget
        assert (
            await r.post("preflight", {"redemption_token": "bad", "credential": {}})
        ).status_code == 400
    finally:
        await r.close()


async def test_trusted_proxy_xff_separates_buyers(env):
    await _supplier(env)
    r = Rig(
        env,
        redeem_ip_limits="preview=1,preflight=5,redeem=5,result=5",
        redeem_trusted_proxies="127.0.0.1/32",
    )
    try:
        code = r.add_cdk()
        a = {"x-forwarded-for": "198.51.100.1"}
        b = {"x-forwarded-for": "198.51.100.2"}
        assert (await r.post("preview", {"code": code}, headers=a)).status_code == 200
        assert (await r.post("preview", {"code": code}, headers=a)).status_code == 429
        assert (await r.post("preview", {"code": code}, headers=b)).status_code == 200
    finally:
        await r.close()


async def test_per_code_and_per_token_limits(env):
    await _supplier(env)
    r = Rig(env, redeem_code_per_10min=2, redeem_token_per_min=2)
    try:
        code = r.add_cdk()
        assert (await r.post("preview", {"code": code})).status_code == 200
        assert (await r.post("preview", {"code": code})).status_code == 200
        assert _err(await r.post("preview", {"code": code})) == "rate_limited"
        other = r.add_cdk()  # another code is unaffected
        tok = (await r.post("preview", {"code": other})).json()["data"]["redemption_token"]
        for _ in range(2):
            assert (
                await r.client.post("/redeem/result", json={"redemption_token": tok})
            ).status_code == 404
        assert (
            _err(await r.client.post("/redeem/result", json={"redemption_token": tok}))
            == "rate_limited"
        )
    finally:
        await r.close()


async def test_global_budget_is_shared_by_all_buyers(env):
    await _supplier(env)
    r = Rig(
        env,
        redeem_global_limits="preview=2,preflight=5,redeem=5,result=5",
        redeem_trusted_proxies="127.0.0.1/32",
    )
    try:
        for i in range(2):
            c = r.add_cdk()
            ok = await r.post(
                "preview", {"code": c}, headers={"x-forwarded-for": f"198.51.100.{i}"}
            )
            assert ok.status_code == 200
        r3 = await r.post(
            "preview", {"code": r.add_cdk()}, headers={"x-forwarded-for": "198.51.100.77"}
        )
        assert _err(r3) == "rate_limited"
    finally:
        await r.close()


async def test_redis_keys_hold_no_code_token_or_ip(rig):
    code, _, tok, pf = await rig.walk()
    keys = [k.decode() if isinstance(k, bytes) else k for k in await rig.env.s.redis.keys("*")]
    assert keys, "rate limits must have written keys"
    for k in keys:
        assert k.startswith("rl:proc:redeem-"), k  # inside the procurement ACL (~rl:proc:*)
        for secret in (code, tok, pf, SESSION, "127.0.0.1", code.lower(), tok.lower()):
            assert secret not in k


async def test_rate_limit_store_failure_fails_closed(rig):
    class Broken:
        def pipeline(self, *a, **k):
            raise ConnectionError("redis down: " + SESSION)

    rig.ctx.redis = Broken()
    r = await rig.post("preview", {"code": rig.add_cdk()})
    assert r.status_code == 503 and _err(r) == "unavailable"
    assert SESSION not in r.text and rig.mock.redeem_calls == []


# ---------------------------------------------------------------- upstream mapping


def _vendor(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _one(env, handler, step="preview", body=None, **over):
    if not getattr(env, "_redeem_supplier", False):
        await _supplier(env)
        env._redeem_supplier = True
    r = Rig(env, vendor=_vendor(handler), **over)
    try:
        body = body or {"code": "MOCKPLUS-ABCDEFGH"}
        return await r.post(step, body)
    finally:
        await r.close()


@pytest.mark.parametrize(
    "status,payload,expected",
    [
        (400, {"code": 400, "error_code": "WEIRD_NEW_CODE", "msg": "VENDOR-TEXT-LEAK"}, "rejected"),
        (500, {"code": 500, "msg": "VENDOR-TEXT-LEAK", "trace": "VENDOR-TEXT-LEAK"}, "unavailable"),
        (503, {"code": 503, "error_code": "channel_unavailable"}, "unavailable"),
        (401, {"code": 401, "msg": "VENDOR-TEXT-LEAK"}, "unavailable"),
        (403, {"code": 403, "msg": "VENDOR-TEXT-LEAK"}, "unavailable"),
        (404, {"code": 404, "msg": "VENDOR-TEXT-LEAK"}, "code_unavailable"),
        (429, {"code": 429, "msg": "VENDOR-TEXT-LEAK"}, "rate_limited"),
        (
            200,
            {"code": 400, "error_code": "CDK_UNAVAILABLE", "msg": "VENDOR-TEXT-LEAK"},
            "code_unavailable",
        ),
        (200, {"code": 500, "msg": "VENDOR-TEXT-LEAK"}, "unavailable"),  # HTTP 200, code != 0
        (200, {"msg": "no envelope"}, "unavailable"),
        (200, {"code": 0, "msg": "ok", "data": {"redemption_token": "bad token"}}, "unavailable"),
        (502, "<html>VENDOR-TEXT-LEAK</html>", "unavailable"),
    ],
)
async def test_upstream_errors_map_to_fixed_codes(env, status, payload, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload, headers={"Retry-After": "7"})

    r = await _one(env, handler)
    assert _err(r) == expected and r.status_code == ERRORS[expected][0]
    assert "VENDOR-TEXT-LEAK" not in r.text
    assert r.json()["error"]["message"] == ERRORS[expected][1]
    if expected == "rate_limited":
        assert r.headers["retry-after"] == "7"


async def test_upstream_timeout_and_connect_error(env):
    def slow(request):
        raise httpx.ReadTimeout("timed out talking to https://x/?token=SECRET-TOKEN")

    def down(request):
        raise httpx.ConnectError("boom SECRET-TOKEN")

    r = await _one(env, slow)
    assert r.status_code == 504 and _err(r) == "timeout" and "SECRET" not in r.text
    r = await _one(env, down)
    assert r.status_code == 503 and _err(r) == "unavailable" and "SECRET" not in r.text


async def test_vendor_response_is_whitelisted(env):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "redemption_token": "rt_" + "a" * 32,
                    "expires_at": "2030-01-01T00:00:00Z",
                    "plan": "plus",
                    "plan_flow": "checkout",
                    "funding_cap_minor": 0,
                    "owner_email": "owner@example.com",
                    "api_key": "vendor-field-that-must-be-dropped",
                },
            },
        )

    r = await _one(env, handler)
    assert r.status_code == 200
    assert "owner@example.com" not in r.text and "SHOULDNOTLEAK" not in r.text
    assert set(r.json()["data"]) == {
        "redemption_token",
        "expires_at",
        "plan",
        "plan_flow",
        "funding_cap_minor",
    }


def test_error_mapping_table_and_limit_parser():
    assert map_upstream_error("preview", 404, None) == "code_unavailable"
    assert map_upstream_error("result", 404, "not_found") == "not_found"
    assert map_upstream_error("redeem", 400, "CARD_NOT_PAYABLE") == "rejected"
    assert map_upstream_error("redeem", 409, "IDEMPOTENCY_CONFLICT") == "conflict"
    assert map_upstream_error("preflight", 400, "GPT_PLAN_ALREADY_ACTIVE") == "account_ineligible"
    assert map_upstream_error("redeem", 400, "REDEMPTION_SESSION_INVALID") == "session_expired"
    assert set(parse_limits("preview=1,preflight=2,redeem=3,result=4")) == {
        "preview",
        "preflight",
        "redeem",
        "result",
    }
    for bad in ("preview=1", "preview=0,preflight=1,redeem=1,result=1", "x=1,preview=1", ""):
        with pytest.raises(ValueError):
            parse_limits(bad)


# ---------------------------------------------------------------- supplier selection


async def test_no_supplier_or_ambiguous_suppliers_is_unavailable(env):
    r = Rig(env)  # no supplier row at all
    try:
        resp = await r.post("preview", {"code": r.add_cdk()})
        assert resp.status_code == 503 and _err(resp) == "unavailable"
    finally:
        await r.close()
    await _supplier(env, env_name="sandbox")
    await _supplier(env, env_name="production")
    r = Rig(env)
    try:
        resp = await r.post("preview", {"code": r.add_cdk()})
        assert _err(resp) == "unavailable"  # two environments enabled, none pinned
    finally:
        await r.close()
    r = Rig(env, redeem_supplier_env="sandbox")
    try:
        resp = await r.post("preview", {"code": r.add_cdk()})
        assert resp.status_code == 200  # pinned
    finally:
        await r.close()


async def test_disabled_supplier_and_other_tenant_are_ignored(env):
    await _supplier(env, enabled=False)
    from app.models import SupplierAccount

    async with env.owner_session(env.other_tenant_id) as db:
        db.add(
            SupplierAccount(
                id=uuid.uuid4(),
                tenant_id=env.other_tenant_id,
                code="zovocard",
                adapter="zovocard",
                label="Foreign",
                env="sandbox",
                enabled=True,
                settings={},
            )
        )
        await db.commit()
    r = Rig(env)
    try:
        resp = await r.post("preview", {"code": r.add_cdk()})
        assert _err(resp) == "unavailable"  # a reseller's supplier never serves the platform host
    finally:
        await r.close()


def test_settings_are_validated_at_startup():
    from app.procurement.context import ProcurementContext

    def make(**over):
        s = replace(ProcurementSettings(env="test", redeem_device=DEVICE), **over)
        return create_redeem_app(ProcurementContext(s, None, None))  # type: ignore[arg-type]

    for bad in ({"redeem_device": ""}, {"redeem_device": "short"}, {"redeem_device": "a b" * 5}):
        with pytest.raises(ValueError):
            make(**bad)
    with pytest.raises(ValueError):
        make(redeem_supplier_env="staging")
    with pytest.raises(ValueError):
        make(redeem_ip_limits="preview=1")
    assert make() is not None


def test_from_env_reads_redeem_settings():
    s = ProcurementSettings.from_env(
        {"APP_REDEEM_ENABLED": "1", "APP_REDEEM_PORT": "8123", "APP_REDEEM_DEVICE": DEVICE}
    )
    assert s.redeem_enabled is True and s.redeem_port == 8123 and s.redeem_device == DEVICE
    assert ProcurementSettings.from_env({}).redeem_enabled is False  # off by default


# ---------------------------------------------------------------- HTTP hygiene


async def test_headers_no_store_no_cors_no_cookies(rig):
    ok = await rig.post(
        "preview", {"code": rig.add_cdk()}, headers={"origin": "http://redeem.test"}
    )
    bad = await rig.post("preview", {"code": "x"})
    missing = await rig.client.get("/nope")
    wrong_method = await rig.client.get("/redeem/preview")
    for r in (ok, bad, missing, wrong_method):
        assert r.headers["cache-control"] == "no-store"
        assert not [h for h in r.headers if h.lower().startswith("access-control-")]
        assert "set-cookie" not in r.headers
        assert "server" not in r.headers or "uvicorn" not in r.headers["server"].lower()
    assert missing.status_code == 404 and wrong_method.status_code == 405
    pre = await rig.client.options(
        "/redeem/preview",
        headers={"origin": "https://evil.example", "access-control-request-method": "POST"},
    )
    assert pre.status_code == 405 and "access-control-allow-origin" not in pre.headers


async def test_cross_origin_post_is_refused(rig):
    code = rig.add_cdk()
    r = await rig.post("preview", {"code": code}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and _err(r) == "forbidden"
    assert rig.mock.redeem_calls == []
    r = await rig.post("preview", {"code": code}, headers={"origin": "http://redeem.test"})
    assert r.status_code == 200
    # cookies a browser might attach are ignored and never echoed
    r = await rig.post("preview", {"code": code}, headers={"cookie": "session=abc"})
    assert "set-cookie" not in r.headers


async def test_listeners_do_not_cross(env, rig):
    from app.procurement.internal import create_procurement_app

    public = rig.client
    for method, path in (
        ("POST", "/internal/probe"),
        ("POST", "/internal/check-offer"),
        ("GET", "/healthz"),
        ("GET", "/admin/anything"),
    ):
        r = await public.request(method, path, json={})
        assert r.status_code in (404, 405), (method, path, r.status_code)
    internal = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_procurement_app(rig.ctx)),
        base_url="http://procurement",
    )
    try:
        for step in ("preview", "preflight", "redeem", "result"):
            r = await internal.post(f"/redeem/{step}", json={"code": "MOCKPLUS-ABCDEFGH"})
            assert r.status_code == 404
    finally:
        await internal.aclose()


# ---------------------------------------------------------------- nothing sensitive is recorded


async def test_no_secret_in_logs_or_audit(env, rig, caplog):
    caplog.set_level(logging.DEBUG)
    before = await env.scalar("SELECT count(*) FROM audit_log")
    code, _, tok, pf = await rig.walk()
    # failure paths: bad credential, unknown code, rate limit, vendor outage, internal error
    await rig.post("preview", {"code": "MOCKPLUS-NOSUCHCODE1"})
    await rig.post(
        "preflight",
        {
            "redemption_token": tok,
            "credential": {"mode": "session", "session": "invalid-" + SESSION},
        },
    )

    async def boom(*a, **k):
        raise RuntimeError(f"explode {code} {SESSION} {tok}")

    rig.vendor.post = boom
    r = await rig.post("preview", {"code": code})
    assert r.status_code == 503 and code not in r.text
    logged = caplog.text
    assert "redeem" in logged  # the proxy does log, just not secrets
    for secret in (code, tok, pf, SESSION, "invalid-" + SESSION):
        assert secret not in logged, "a secret reached the logs"
    assert await env.scalar("SELECT count(*) FROM audit_log") == before


async def test_result_is_post_only(rig):
    """A token must not travel in a URL on our side: the GET form is gone."""
    r = await rig.client.get("/redeem/result", params={"token": "rt_" + "a" * 32})
    assert r.status_code == 405 and _err(r) == "method_not_allowed"


async def test_a_real_sized_session_is_accepted(rig):
    """A real ChatGPT session text is about 37 KB of JSON (quotes double when embedded)."""
    session = json.dumps(
        {"accessToken": "a" * 1800, "sessionToken": "s" * 3900, "pad": ['"x"'] * 4500}
    )
    assert 36_000 < len(session) < 65_536
    r = await rig.client.post("/redeem/preview", json={"code": rig.add_cdk()})
    tok = r.json()["data"]["redemption_token"]
    r = await rig.client.post(
        "/redeem/preflight",
        json={"redemption_token": tok, "credential": {"mode": "session", "session": session}},
    )
    assert r.status_code == 200, _err(r)


def test_redeem_answer_in_the_vendors_real_shape():
    """Shape seen in the vendor sandbox on 2026-11-16: the order id is data.order.id."""
    from app.procurement.redeem import shape_redeem

    real = {
        "events": [{"at": "2026-11-15T12:10:00Z", "type": "created"}],
        "order": {
            "client_request_id": "x",
            "id": "SBXORD-b5020d12a1a8a9a0",
            "product": "gpt",
            "sandbox_mock": True,
            "status": "succeeded",
        },
    }
    shaped = shape_redeem(real)
    assert shaped.pop("progress")["outcome"] == "completed"  # issue #30: the same step model
    assert shaped == {
        "order_id": "SBXORD-b5020d12a1a8a9a0",
        "status": "succeeded",
        "stage": None,
        "terminal": True,
    }


def test_production_unknown_code_answer_maps_to_code_unavailable():
    """Seen on the production vendor 2026-11-17: HTTP 400 with error_code ``not_found``."""
    from app.procurement.redeem import map_upstream_error

    assert map_upstream_error("preview", 400, "not_found") == "code_unavailable"
    assert map_upstream_error("result", 400, "not_found") == "not_found"


async def test_showcase_simulator_redemption_moves_through_its_stages(rig, monkeypatch):
    """Demo showcase (``catalog_full``): a redemption is "processing" for ~20 s with advancing
    stages and progress, then succeeds, so the redemption page shows its progress screen."""
    import time as _time
    from types import SimpleNamespace

    from app.devtools import mock_zovocard as mz

    rig.mock.catalog_full = True
    t0 = _time.monotonic()
    clock = {"now": t0}
    # only the simulator's clock moves (asyncio and the proxy keep the real one)
    monkeypatch.setattr(
        mz, "time", SimpleNamespace(monotonic=lambda: clock["now"], time=_time.time)
    )
    _code, (_r1, _r2, r3, r4), tok, _pf = await rig.walk()
    d3, d4 = r3.json()["data"], r4.json()["data"]
    assert d3["status"] == "processing" and d3["terminal"] is False
    assert d4["status"] == "processing" and d4["progress"]["outcome"] == "active"
    first = d4["progress"]["percent"]
    clock["now"] = t0 + 13
    d5 = (await rig.client.post("/redeem/result", json={"redemption_token": tok})).json()["data"]
    assert d5["status"] == "processing" and d5["progress"]["percent"] > first
    clock["now"] = t0 + 25
    d6 = (await rig.client.post("/redeem/result", json={"redemption_token": tok})).json()["data"]
    assert d6["status"] == "succeeded" and d6["progress"]["outcome"] == "completed"
