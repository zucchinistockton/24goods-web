"""review-v12 M1: supplier adapter layer, ZovoCard client and error table (no DB needed)."""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from app.devtools.mock_zovocard import NOALLOW_KEY, OK_KEY, MockZovo, create_mock_zovocard
from app.suppliers.base import (
    IDEMPOTENCY_KEY_RE,
    JobSpec,
    OfferSpec,
    Outcome,
    SupplierError,
    dec,
    job_idempotency_key,
)
from app.suppliers.manual import NEEDS_MANUAL, ManualSupplier
from app.suppliers.mock import MockSupplier
from app.suppliers.registry import SupplierConfigError, build_supplier, load_secrets
from app.suppliers.zovocard import (
    PlanCatalog,
    ProductionWriteBlocked,
    RegistryEntry,
    TokenBucket,
    ZovoCardSupplier,
    ZovoClient,
    ZovoConfig,
    is_purchasable,
    parse_balance,
    parse_plans,
)
from app.suppliers.zovocard.errors import RECHARGE_TERMINAL_CODES, TABLE, classify

BASE = "http://mockzovo/openapi/v1"

# ---------------------------------------------------------------- error table, row by row

# Every row of errors.TABLE with the outcome the spec (review-v12 §5, appendix A) requires.
EXPECTED: dict[tuple[int | None, str | None], tuple[Outcome, bool]] = {
    (401, None): (Outcome.UNAVAILABLE, True),
    (403, None): (Outcome.UNAVAILABLE, True),
    (429, None): (Outcome.RETRYABLE, False),
    (500, None): (Outcome.RETRYABLE, False),
    (None, "invalid_argument"): (Outcome.FATAL, False),
    (None, "not_found"): (Outcome.FATAL, False),
    (None, "insufficient_balance"): (Outcome.UNAVAILABLE, True),
    (None, "request_failed"): (Outcome.UNAVAILABLE, True),
    (None, "forbidden"): (Outcome.FATAL, False),
    (None, "internal_error"): (Outcome.RETRYABLE, False),
    (None, "channel_unavailable"): (Outcome.UNAVAILABLE, False),
    (None, "RECHARGE_REQUIRED"): (Outcome.UNAVAILABLE, True),
    (None, "FORBIDDEN"): (Outcome.UNAVAILABLE, True),
    (None, "product_exclusive_access_required"): (Outcome.FATAL, False),
    (None, "product_approval_required"): (Outcome.FATAL, False),
    (None, "product_policy_outdated"): (Outcome.FATAL, False),
    (None, "product_unavailable"): (Outcome.UNAVAILABLE, False),
    (None, "amount_below_min"): (Outcome.FATAL, False),
    (None, "DELETE_DISABLED"): (Outcome.FATAL, False),
    (None, "ACCOUNT_UNAVAILABLE"): (Outcome.UNAVAILABLE, True),
    (None, "GPT_DIRECT_ACCESS_DENIED"): (Outcome.UNAVAILABLE, True),
    (None, "GPT_DIRECT_UNAVAILABLE"): (Outcome.UNAVAILABLE, False),
    (None, "GPT_DIRECT_PAUSED"): (Outcome.UNAVAILABLE, False),
    (None, "DIRECT_PRODUCT_DISABLED"): (Outcome.UNAVAILABLE, False),
    (None, "INVALID_REQUEST"): (Outcome.FATAL, False),
    (None, "EXTERNAL_CARD_DISABLED"): (Outcome.FATAL, False),
    (None, "INSUFFICIENT_BALANCE"): (Outcome.UNAVAILABLE, True),
    (None, "PRECHECK_REJECTED"): (Outcome.FATAL, False),
    (None, "GPT_PLAN_ALREADY_ACTIVE"): (Outcome.FATAL, False),
    (None, "GPT_PRICE_UNCONFIRMED"): (Outcome.RETRYABLE, False),
    (None, "GPT_SESSION_INVALID"): (Outcome.FATAL, False),
    (None, "CLAUDE_SESSION_INVALID"): (Outcome.FATAL, False),
    (None, "GPT_CREDIT_REQUIRES_SUBSCRIPTION"): (Outcome.FATAL, False),
    (None, "GPT_CREDIT_DISABLED"): (Outcome.UNAVAILABLE, False),
    (None, "GPT_RENEW_TARGET_NOT_PRO"): (Outcome.FATAL, False),
    (None, "GPT_RENEW_SUBSCRIPTION_EXPIRED"): (Outcome.FATAL, False),
    (None, "GPT_PREFLIGHT_RATE_LIMITED"): (Outcome.RETRYABLE, False),
    (None, "CDK_DISABLE_REJECTED"): (Outcome.FATAL, False),
    (None, "API_SCOPE_UNAVAILABLE"): (Outcome.UNAVAILABLE, True),
    (None, "ORDER_NOT_FOUND"): (Outcome.FATAL, False),
    (None, "cdk_brand_prefix_invalid"): (Outcome.FATAL, False),
}

# rows whose outcome on a WRITE differs from the read outcome above
WRITE_OUTCOME: dict[tuple[int | None, str | None], Outcome] = {
    (500, None): Outcome.AMBIGUOUS,
    (None, "internal_error"): Outcome.AMBIGUOUS,
    (None, "request_failed"): Outcome.AMBIGUOUS,
}

# a plausible HTTP status for each code-keyed row (the vendor appendix)
STATUS_FOR = {
    "insufficient_balance": 400,
    "INSUFFICIENT_BALANCE": 400,
    "channel_unavailable": 503,
    "product_unavailable": 503,
    "GPT_DIRECT_UNAVAILABLE": 503,
    "GPT_DIRECT_PAUSED": 503,
    "internal_error": 500,
    "RECHARGE_REQUIRED": 403,
    "FORBIDDEN": 403,
    "GPT_DIRECT_ACCESS_DENIED": 403,
    "API_SCOPE_UNAVAILABLE": 403,
    "EXTERNAL_CARD_DISABLED": 403,
    "DELETE_DISABLED": 403,
    "product_exclusive_access_required": 403,
    "product_approval_required": 403,
    "ACCOUNT_UNAVAILABLE": 401,
    "product_policy_outdated": 409,
    "CDK_DISABLE_REJECTED": 409,
    "GPT_PREFLIGHT_RATE_LIMITED": 429,
    "ORDER_NOT_FOUND": 404,
}


def test_table_has_exactly_the_specified_rows():
    assert set(TABLE) == set(EXPECTED)


@pytest.mark.parametrize("key", sorted(EXPECTED, key=lambda k: (str(k[0]), str(k[1]))))
@pytest.mark.parametrize("write", [True, False])
def test_error_table_row(key, write):
    status, code = key
    outcome, stop = EXPECTED[key]
    if write:
        outcome = WRITE_OUTCOME.get(key, outcome)
    status = status if status is not None else STATUS_FOR.get(code or "", 400)
    rule = classify(status, code, write=write)
    assert (rule.outcome, rule.supplier_stop) == (outcome, stop)


def test_server_errors_and_catch_all_are_ambiguous_on_writes():
    # a write may have run before a 500: query first, resubmit only under the same key
    assert classify(500, None, write=True).outcome is Outcome.AMBIGUOUS
    assert classify(500, None, write=False).outcome is Outcome.RETRYABLE
    assert classify(500, "internal_error", write=True).outcome is Outcome.AMBIGUOUS
    assert classify(500, "internal_error", write=False).outcome is Outcome.RETRYABLE
    rf = classify(400, "request_failed", write=True)
    assert rf.outcome is Outcome.AMBIGUOUS and rf.supplier_stop is True
    rf = classify(400, "request_failed", write=False)
    assert rf.outcome is Outcome.UNAVAILABLE and rf.supplier_stop is True
    # definite "no charge" codes stay UNAVAILABLE on writes (vendor §3 / A.2)
    assert classify(503, "channel_unavailable", write=True).outcome is Outcome.UNAVAILABLE
    assert classify(503, "product_unavailable", write=True).outcome is Outcome.UNAVAILABLE


def test_unknown_errors_on_writes_are_ambiguous_never_retried_with_a_new_key():
    for status in (400, 404, 418, 422):
        assert classify(status, None, write=True).outcome is Outcome.AMBIGUOUS
        assert classify(status, "brand_new_code", write=True).outcome is Outcome.AMBIGUOUS
    assert (
        classify(409, None, write=True).outcome is Outcome.AMBIGUOUS
    )  # idempotent request running
    assert classify(502, None, write=True).outcome is Outcome.AMBIGUOUS
    assert classify(503, None, write=True).outcome is Outcome.AMBIGUOUS  # 503 without a code


def test_unknown_errors_on_reads():
    assert classify(404, None, write=False).outcome is Outcome.FATAL
    assert classify(400, "brand_new_code", write=False).outcome is Outcome.FATAL
    assert classify(409, None, write=False).outcome is Outcome.RETRYABLE
    assert classify(502, None, write=False).outcome is Outcome.RETRYABLE


def test_codes_are_case_sensitive():
    assert classify(400, "forbidden", write=True).outcome is Outcome.FATAL
    assert classify(403, "FORBIDDEN", write=True).supplier_stop is True
    assert classify(400, "Insufficient_Balance", write=True).outcome is Outcome.AMBIGUOUS


def test_recharge_pending_rule():
    # vendor §3: "result pending" is a 400 WITHOUT error_code on /cards/recharge
    assert (
        classify(400, None, write=True, endpoint="cards.recharge").outcome
        is Outcome.ACCEPTED_PENDING
    )
    pending = classify(400, "request_failed", write=True, endpoint="cards.recharge")
    assert pending.outcome is Outcome.ACCEPTED_PENDING
    for code in RECHARGE_TERMINAL_CODES:
        rule = classify(400, code, write=True, endpoint="cards.recharge")
        assert rule.outcome is not Outcome.ACCEPTED_PENDING
    assert classify(503, "channel_unavailable", write=True, endpoint="cards.recharge").outcome is (
        Outcome.UNAVAILABLE
    )


# ---------------------------------------------------------------- idempotency keys


@pytest.mark.parametrize(
    "key,ok",
    [
        ("pj:0123abcd:submit", True),
        ("A" * 80, True),
        ("a.b_c-d:e", True),
        ("", False),
        ("A" * 81, False),
        ("has space", False),
        ("slash/no", False),
        ("ключ", False),
        ("new\nline", False),
    ],
)
def test_idempotency_key_format(key, ok):
    assert bool(IDEMPOTENCY_KEY_RE.fullmatch(key)) is ok


def test_job_key_format_and_jobspec_validation():
    key = job_idempotency_key("0" * 32, "open")
    assert key == "pj:" + "0" * 32 + ":open"
    with pytest.raises(ValueError):
        job_idempotency_key("0" * 32, "bad step")
    with pytest.raises(ValueError):
        JobSpec("j", "s", "not valid!", OfferSpec("cdk"))


# ---------------------------------------------------------------- money


def test_decimal_from_vendor_floats():
    assert dec(1.5) == Decimal("1.5")
    assert dec(0.1) + dec(0.2) == Decimal("0.3")  # str() round trip, never float math
    for bad in (True, None, "nan", "inf", float("nan"), "abc"):
        with pytest.raises(ValueError):
            dec(bad)
    bal = parse_balance({"balance": 6.27, "spendable_balance": 6.27, "currency": "usd"})
    assert bal.spendable == Decimal("6.27") and bal.currency == "USD"


# ---------------------------------------------------------------- availability rule


def _catalog(registry: list[dict], plans: dict) -> PlanCatalog:
    return parse_plans("gpt", {"version": 1, "plans": plans, "registry": registry})


def test_availability_rule_strict():
    plans = {"plus": {"enabled": True}, "off": {"enabled": False}, "truthy": {"enabled": 1}}
    cat = _catalog(
        [
            {"key": "plus", "acc_plan_key": "plus", "purchasable": True},
            {"key": "alias", "acc_plan_key": "plus", "purchasable": True},
            {"key": "off", "acc_plan_key": "off", "purchasable": True},
            {"key": "no_flag", "acc_plan_key": "plus"},
            {"key": "str_flag", "acc_plan_key": "plus", "purchasable": "true"},
            {"key": "int_flag", "acc_plan_key": "plus", "purchasable": 1},
            {"key": "no_acc", "purchasable": True},
            {"key": "dangling", "acc_plan_key": "missing", "purchasable": True},
            {"key": "truthy", "acc_plan_key": "truthy", "purchasable": True},
            {"key": "blocked", "acc_plan_key": "plus", "purchasable": False},
        ],
        plans,
    )
    assert is_purchasable(cat, "plus") and is_purchasable(cat, "alias")
    for key in ("off", "no_flag", "str_flag", "int_flag", "no_acc", "dangling", "truthy"):
        assert not is_purchasable(cat, key), key
    assert not is_purchasable(cat, "blocked")
    assert not is_purchasable(cat, "unknown")
    # a plan that is enabled but has no registry row is not purchasable either
    assert not is_purchasable(_catalog([], plans), "plus")


def test_availability_rule_plan_without_enabled_field_and_duplicates():
    cat = _catalog(
        [
            {"key": "plus", "acc_plan_key": "plus", "purchasable": True},
            {"key": "dup", "acc_plan_key": "plus", "purchasable": True},
            {"key": "dup", "acc_plan_key": "plus", "purchasable": True},
        ],
        {"plus": {"label": "Plus"}},
    )
    assert not is_purchasable(cat, "plus")  # plans[...].enabled missing
    cat2 = PlanCatalog(
        "gpt",
        1,
        plans={"plus": {"enabled": True}},
        registry={"x": RegistryEntry("x", "gpt", "plus", True)},
    )
    assert is_purchasable(cat2, "x")
    dup = _catalog(
        [
            {"key": "dup", "acc_plan_key": "plus", "purchasable": True},
            {"key": "dup", "acc_plan_key": "plus", "purchasable": True},
        ],
        {"plus": {"enabled": True}},
    )
    assert not is_purchasable(dup, "dup")  # ambiguous duplicate rows: refuse


def test_parse_plans_malformed():
    with pytest.raises(SupplierError):
        parse_plans("gpt", ["not", "a", "dict"])
    cat = parse_plans("gpt", {"plans": "x", "registry": [1, None, {"key": 5}]})
    assert cat.registry == {} and cat.plans == {}


# ---------------------------------------------------------------- client against the mock server


def _client(
    mock: MockZovo | None = None,
    *,
    key: str = OK_KEY,
    env: str = "sandbox",
    allow: bool = False,
    app_id: str = "ak_mock",
) -> tuple[ZovoClient, MockZovo]:
    mock = mock or MockZovo()
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_mock_zovocard(mock)))
    cfg = ZovoConfig(
        base_url=BASE, app_secret=key, app_id=app_id, env=env, allow_production_writes=allow
    )
    return ZovoClient(cfg, http=http, bucket=TokenBucket(6000, 1000)), mock


async def test_client_sends_auth_and_decodes_envelope():
    client, mock = _client()
    resp = await client.get("/balance")
    assert resp.status == 200 and resp.data["currency"] == "USD"
    method, path, seen = mock.requests[-1]
    assert (method, path) == ("GET", "/balance")
    assert seen["api_key"] == "1" and seen["app_id"] == "ak_mock"
    assert seen["idempotency_key"] == ""  # reads carry no key


async def test_client_401_403_are_supplier_stops():
    client, _ = _client(key="sk_wrong")
    with pytest.raises(SupplierError) as e:
        await client.get("/balance")
    assert (e.value.status, e.value.error_code, e.value.supplier_stop) == (401, None, True)
    client, _ = _client(key=NOALLOW_KEY)
    with pytest.raises(SupplierError) as e:
        await client.get("/balance")
    assert e.value.status == 403 and e.value.error_code is None
    assert e.value.outcome is Outcome.UNAVAILABLE and e.value.supplier_stop


@pytest.mark.parametrize(
    "quirk,status,code,outcome",
    [
        ("429", 429, None, Outcome.RETRYABLE),
        ("503", 503, "channel_unavailable", Outcome.UNAVAILABLE),
        ("409", 409, None, Outcome.RETRYABLE),
        ("200_code_404", 404, None, Outcome.FATAL),
        ("500", 500, "internal_error", Outcome.RETRYABLE),
        ("403_access_denied", 403, "GPT_DIRECT_ACCESS_DENIED", Outcome.UNAVAILABLE),
    ],
)
async def test_client_quirks_on_reads(quirk, status, code, outcome):
    client, mock = _client()
    mock.force("/balance", quirk)
    with pytest.raises(SupplierError) as e:
        await client.get("/balance")
    assert (e.value.status, e.value.error_code, e.value.outcome) == (status, code, outcome)
    assert (await client.get("/balance")).status == 200  # a forced quirk is one-shot


async def test_client_rate_limit_429_carries_retry_after():
    client, _ = _client(MockZovo(rate_per_min=2))
    await client.get("/balance")
    await client.get("/balance")
    with pytest.raises(SupplierError) as e:
        await client.get("/balance")
    assert e.value.outcome is Outcome.RETRYABLE and e.value.retry_after == timedelta(seconds=30)


async def test_client_409_on_write_is_ambiguous_and_keeps_key():
    client, mock = _client()
    mock.force("/gpt-direct/cdks", "409")
    with pytest.raises(SupplierError) as e:
        await client.request(
            "POST", "/gpt-direct/cdks", json_body={"plan": "plus"}, idempotency_key="pj:abc:submit"
        )
    assert e.value.outcome is Outcome.AMBIGUOUS
    assert mock.requests[-1][2]["idempotency_key"] == "pj:abc:submit"


async def test_client_write_requires_valid_idempotency_key_before_sending():
    client, mock = _client()
    for bad in (None, "", "has space", "x" * 81):
        with pytest.raises(ValueError):
            await client.request("POST", "/gpt-direct/cdks", json_body={}, idempotency_key=bad)
    with pytest.raises(ValueError):
        await client.request("GET", "/balance", idempotency_key="pj:a:b")
    assert mock.requests == []


async def test_mock_rejects_malformed_idempotency_key_like_the_vendor():
    mock = MockZovo()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_mock_zovocard(mock)), base_url="http://m"
    ) as http:
        r = await http.post(
            "/openapi/v1/cards/open",
            headers={"X-API-Key": OK_KEY, "Idempotency-Key": "bad key"},
            json={},
        )
    assert r.status_code == 400 and "error_code" not in r.json()


async def test_production_write_guard():
    client, mock = _client(env="production")
    with pytest.raises(ProductionWriteBlocked):
        await client.request(
            "POST", "/gpt-direct/cdks", json_body={"plan": "plus"}, idempotency_key="pj:abc:submit"
        )
    with pytest.raises(ProductionWriteBlocked):
        await client.request("DELETE", "/cards/1", idempotency_key="pj:abc:void")
    assert mock.requests == []  # nothing left the process
    assert (await client.get("/balance")).status == 200  # reads stay allowed
    # explicit opt-in (APP_PROCUREMENT_ALLOW_PRODUCTION_WRITES=1): the request is sent
    client, mock = _client(env="production", allow=True)
    with pytest.raises(SupplierError) as e:  # the mock implements no writes: unknown 404
        await client.request("POST", "/gpt-direct/cdks", json_body={}, idempotency_key="pj:a:b")
    assert e.value.outcome is Outcome.AMBIGUOUS
    assert mock.requests[-1][:2] == ("POST", "/gpt-direct/cdks")
    # sandbox writes are not guarded
    client, mock = _client(env="sandbox")
    with pytest.raises(SupplierError):
        await client.request("POST", "/gpt-direct/cdks", json_body={}, idempotency_key="pj:a:b")
    assert mock.requests


async def test_client_transport_errors():
    def handler_for(exc: Exception):
        def handler(request: httpx.Request) -> httpx.Response:
            raise exc

        return handler

    async def call(exc: Exception, method: str) -> SupplierError:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler_for(exc)))
        cfg = ZovoConfig(base_url=BASE, app_secret="sk_x", env="sandbox")
        client = ZovoClient(cfg, http=http, bucket=TokenBucket(6000, 100))
        key = None if method == "GET" else "pj:a:b"
        with pytest.raises(SupplierError) as e:
            await client.request(method, "/balance", idempotency_key=key)
        return e.value

    connect = httpx.ConnectError("refused")
    read = httpx.ReadTimeout("slow")
    assert (await call(connect, "POST")).outcome is Outcome.RETRYABLE  # never sent
    assert (await call(read, "POST")).outcome is Outcome.AMBIGUOUS  # timeout after send
    assert (await call(read, "GET")).outcome is Outcome.RETRYABLE


async def test_client_200_with_error_code_on_write_is_ambiguous():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"code": 500, "error_code": "internal_error", "msg": "server error"}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ZovoClient(
        ZovoConfig(base_url=BASE, app_secret="sk_x"), http=http, bucket=TokenBucket(6000, 100)
    )
    with pytest.raises(SupplierError) as e:
        await client.request("POST", "/gpt-direct/cdks", json_body={}, idempotency_key="pj:a:b")
    assert e.value.outcome is Outcome.AMBIGUOUS and e.value.status == 500
    with pytest.raises(SupplierError) as e:
        await client.get("/balance")
    assert e.value.outcome is Outcome.RETRYABLE


async def test_client_500_on_write_is_ambiguous_via_mock():
    client, mock = _client()
    mock.force("/gpt-direct/cdks", "500")
    with pytest.raises(SupplierError) as e:
        await client.request("POST", "/gpt-direct/cdks", json_body={}, idempotency_key="pj:a:b")
    assert e.value.outcome is Outcome.AMBIGUOUS


async def test_client_non_json_and_bodyless_2xx():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="<html>proxy</html>")
        return httpx.Response(200, json={"msg": "ok"})  # no code at all

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ZovoClient(
        ZovoConfig(base_url=BASE, app_secret="sk_x"), http=http, bucket=TokenBucket(6000, 100)
    )
    with pytest.raises(SupplierError) as e:
        await client.get("/balance")
    assert e.value.outcome is Outcome.RETRYABLE
    with pytest.raises(SupplierError) as e:
        await client.request("POST", "/x", json_body={}, idempotency_key="pj:a:b")
    assert e.value.outcome is Outcome.AMBIGUOUS


async def test_client_never_logs_secret_or_body(caplog):
    client, _ = _client(key="sk_wrong")
    caplog.set_level("DEBUG", logger="app.suppliers")
    with pytest.raises(SupplierError):
        await client.get("/balance")
    text = caplog.text
    assert "sk_wrong" not in text and "invalid api key" not in text
    assert "status=401" in text


async def test_client_rejects_odd_paths():
    client, mock = _client()
    for path in ("balance", "/../admin", "/x?y=1", "https://evil/x", "/a b"):
        with pytest.raises(ValueError):
            await client.get(path)
    assert mock.requests == []


async def test_token_bucket_paces_requests():
    now = [0.0]
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    bucket = TokenBucket(60, 2, clock=lambda: now[0], sleep=fake_sleep)
    for _ in range(5):
        await bucket.acquire()
    # burst of 2, then one token per second
    assert slept == pytest.approx([1.0, 1.0, 1.0])


# ---------------------------------------------------------------- supplier adapter


def _supplier(mock: MockZovo | None = None, key: str = OK_KEY) -> ZovoCardSupplier:
    client, _ = _client(mock, key=key)
    return ZovoCardSupplier("zovocard", client)


async def test_probe_green():
    report = await _supplier().probe()
    assert report.ok and report.auth_ok and report.ip_allowed
    assert report.permissions == {"direct": True, "cards": True}
    assert report.balance is not None and report.balance.spendable == Decimal("12.34")
    # gpt: go + plus; claude: pro + max_5x; grok: premium_monthly (super disabled)
    assert report.purchasable_plans == 5
    assert report.card_products == 2  # MOCK03 is disabled
    d = report.as_dict()
    assert d["spendable_balance"] == "12.34" and d["currency"] == "USD"
    assert OK_KEY not in repr(d)


async def test_probe_allowlist_and_auth_failures():
    r = await _supplier(key=NOALLOW_KEY).probe()
    assert not r.ok and r.auth_ok is True and r.ip_allowed is False
    assert r.errors == ("ip_not_allowlisted",)
    r = await _supplier(key="sk_wrong").probe()
    assert not r.ok and r.auth_ok is False and r.errors == ("auth_failed",)


async def test_probe_degraded_when_direct_permission_missing():
    mock = MockZovo()
    mock.force("/gpt-direct/plans", "403_access_denied", times=3)
    r = await _supplier(mock).probe()
    assert not r.ok and r.balance is not None
    assert r.permissions["direct"] is False and r.permissions["cards"] is True
    assert r.purchasable_plans is None
    assert "plans_gpt:GPT_DIRECT_ACCESS_DENIED" in r.errors


async def test_availability_via_adapter():
    s = _supplier()
    assert (await s.availability(OfferSpec("cdk", {"product": "gpt", "plan": "plus"}))).available
    for plan in ("pro_20x", "pro_5x_renew", "credit250", "nope"):
        a = await s.availability(OfferSpec("cdk", {"product": "gpt", "plan": plan}))
        assert not a.available and a.reason == "not_purchasable", plan
    direct = await s.availability(OfferSpec("direct", {"product": "claude", "plan": "pro"}))
    assert direct.available
    # the acc_plan_key is not a registry key
    bad = await s.availability(OfferSpec("direct", {"product": "claude", "plan": "claude_pro"}))
    assert not bad.available
    assert not (await s.availability(OfferSpec("cdk", {"product": "grok", "plan": "x"}))).available
    assert not (await s.availability(OfferSpec("vcc", {}))).available
    assert not (await s.availability(OfferSpec("cdk", {"product": "x", "plan": "a"}))).available


async def test_writes_not_implemented_in_m1():
    s = _supplier()
    job = JobSpec("j", "submit", "pj:j:submit", OfferSpec("cdk", {"plan": "plus"}))
    for op in (s.submit, s.query, s.void):
        with pytest.raises(NotImplementedError):
            await op(job)
    with pytest.raises(NotImplementedError):
        s.verify_webhook(b"{}", {})


# ---------------------------------------------------------------- mock / manual suppliers


async def test_mock_supplier_is_deterministic_and_idempotent():
    m = MockSupplier(outcomes=[Outcome.AMBIGUOUS])
    job = JobSpec("j", "submit", "pj:j:submit", OfferSpec("cdk", {"plan": "plus"}))
    first = await m.submit(job)
    assert first.outcome is Outcome.AMBIGUOUS and m.spendable == Decimal("100.00")
    ok = await m.submit(job)
    again = await m.submit(job)
    assert ok.outcome is Outcome.OK and again == ok and m.spendable == Decimal("99.00")
    assert (await m.query(job)).secrets == ok.secrets
    assert (await m.availability(OfferSpec("cdk", {"plan": "plus"}))).available
    assert not (await m.availability(OfferSpec("cdk", {"plan": "pro"}))).available


async def test_manual_supplier_always_needs_manual():
    s = ManualSupplier()
    job = JobSpec("j", "submit", "pj:j:submit", OfferSpec("cdk"))
    for op in (s.submit, s.query, s.void):
        r = await op(job)
        assert r.outcome is Outcome.FATAL and r.error_code == NEEDS_MANUAL
    assert (await s.probe()).ok


# ---------------------------------------------------------------- registry / secrets


def test_secrets_by_code_and_env():
    env = {
        "ZOVOCARD_APP_SECRET": "sk_prod_value",
        "ZOVOCARD_APP_ID": "ak_prod",
        "ZOVOCARD_SANDBOX_APP_SECRET": "sk_sandbox_value",
    }
    prod = load_secrets("zovocard", "production", env, default_base="https://p")
    sand = load_secrets("zovocard", "sandbox", env, default_base="https://s")
    assert (prod.app_secret, prod.app_id, prod.base_url) == (
        "sk_prod_value",
        "ak_prod",
        "https://p",
    )
    assert (sand.app_secret, sand.app_id) == ("sk_sandbox_value", "")
    assert "sk_prod_value" not in repr(prod)
    with pytest.raises(SupplierConfigError) as e:
        load_secrets("other", "sandbox", env)
    assert "OTHER_SANDBOX_APP_SECRET" in str(e.value)
    with pytest.raises(SupplierConfigError):
        load_secrets("Bad-Code", "sandbox", env)


def test_build_supplier_guards():
    env = {"ZOVOCARD_SANDBOX_APP_SECRET": "sk_x", "ZOVOCARD_SANDBOX_BASE_URL": "http://mock"}
    s = build_supplier(
        code="zovocard", adapter="zovocard", env="sandbox", environ=env, app_env="dev"
    )
    assert isinstance(s, ZovoCardSupplier) and s.client.config.base_url == "http://mock"
    with pytest.raises(SupplierConfigError):  # plain http outside dev/test
        build_supplier(
            code="zovocard", adapter="zovocard", env="sandbox", environ=env, app_env="prod"
        )
    with pytest.raises(SupplierConfigError):
        build_supplier(code="mock", adapter="mock", env="sandbox", environ={}, app_env="prod")
    assert build_supplier(code="mock", adapter="mock", env="sandbox", environ={}, app_env="test")
    prod = build_supplier(
        code="zovocard",
        adapter="zovocard",
        env="production",
        environ={"ZOVOCARD_APP_SECRET": "sk_p"},
        app_env="prod",
    )
    assert prod.client.config.base_url == "https://zovocard.com/openapi/v1"
    assert prod.client.config.allow_production_writes is False


# ---------------------------------------------------------------- process boundaries


def test_suppliers_and_procurement_do_not_load_the_api_stack():
    code = (
        "import sys, app.procurement.__main__, app.procurement.service, "
        "app.procurement.internal, app.suppliers.registry; "
        "bad = {'fastapi', 'pydantic', 'PIL', 'argon2'} & {k.split('.')[0] for k in sys.modules}; "
        "bad |= {k for k in sys.modules if k.startswith('app.modules') "
        "or k in ('app.core.resources', 'app.core.config', 'app.delivery.keys')}; "
        "sys.exit(1 if bad else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0  # noqa: S603


def test_procurement_settings_bool_and_private_key_refusal():
    from app.procurement.config import ProcurementSettings

    s = ProcurementSettings.from_env({"APP_PROCUREMENT_ALLOW_PRODUCTION_WRITES": "0"})
    assert s.procurement_allow_production_writes is False
    s = ProcurementSettings.from_env({"APP_PROCUREMENT_ALLOW_PRODUCTION_WRITES": "1"})
    assert s.procurement_allow_production_writes is True
    with pytest.raises(ValueError):
        ProcurementSettings.from_env({"APP_PROCUREMENT_ALLOW_PRODUCTION_WRITES": "maybe"})
    with pytest.raises(ValueError):
        ProcurementSettings.from_env({"APP_VAULT_PRIVATE_KEYS_FILE": "/run/secrets/x"})
    assert ProcurementSettings.from_env({}).procurement_allow_production_writes is False


async def test_probe_cli_reports_missing_config_without_secrets():
    from app.procurement.__main__ import probe_cli

    r = await probe_cli("zovocard", "sandbox", "zovocard", "dev", environ={})
    assert not r.ok and "ZOVOCARD_SANDBOX_APP_SECRET" in r.errors[0]


async def test_vendor_error_codes_are_sanitized():
    from app.suppliers.zovocard.client import MALFORMED_CODE, sanitize_error_code

    assert sanitize_error_code(None) is None and sanitize_error_code("") is None
    assert sanitize_error_code("GPT_SESSION_INVALID") == "GPT_SESSION_INVALID"
    for bad in ("<script>", "a b", "x" * 65, 42, "代码", "a\nb"):
        assert sanitize_error_code(bad) == MALFORMED_CODE

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 400, "error_code": "<img src=x>"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ZovoClient(
        ZovoConfig(base_url=BASE, app_secret="sk_x"), http=http, bucket=TokenBucket(6000, 100)
    )
    with pytest.raises(SupplierError) as e:
        await client.get("/balance")
    assert e.value.error_code == MALFORMED_CODE and e.value.reason == MALFORMED_CODE


def test_procurement_env_defaults_to_prod():
    from app.procurement.config import ProcurementSettings

    assert ProcurementSettings.from_env({}).env == "prod"
    assert ProcurementSettings.from_env({"APP_ENV": "dev"}).env == "dev"


@pytest.mark.parametrize(
    "code,adapter,problem",
    [
        ("zovocard", "zovocard", None),
        ("zovocard_backup", "zovocard", None),
        ("zovocard_sandbox", "zovocard", "reserved_suffix"),
        ("x_sandbox", "manual", "reserved_suffix"),
        ("acme", "zovocard", "code_adapter_mismatch"),
        ("zovocardx", "zovocard", "code_adapter_mismatch"),
        ("anything", "manual", None),
        ("anything", "mock", None),
        ("Bad", "manual", "invalid_code"),
    ],
)
def test_supplier_code_rules(code, adapter, problem):
    from app.suppliers.base import supplier_code_problem

    assert supplier_code_problem(code, adapter) == problem


def test_registry_refuses_mismatched_codes():
    env = {"ACME_SANDBOX_APP_SECRET": "sk_x", "ZOVOCARD_SANDBOX_SANDBOX_APP_SECRET": "sk_y"}
    for code in ("acme", "zovocard_sandbox"):
        with pytest.raises(SupplierConfigError):
            build_supplier(code=code, adapter="zovocard", env="sandbox", environ=env, app_env="dev")
    with pytest.raises(SupplierConfigError):
        load_secrets("zovocard_sandbox", "production", env)


def test_clean_detail():
    from app.suppliers.base import clean_detail

    assert (
        clean_detail("plans_gpt:GPT_DIRECT_ACCESS_DENIED") == "plans_gpt:GPT_DIRECT_ACCESS_DENIED"
    )
    assert clean_detail("<b>x y</b>") == "_b_x_y__b_"
    assert len(clean_detail("a" * 200)) == 64
