# ruff: noqa: E501, F811
"""Issue #31 (review-v12 §23): the redemption admin. Attempt log written by the public proxy, code
lookup, stats, settings (maintenance / announcement / limits), disable / enable at the vendor.
Real console endpoints -> real procurement internal app -> real ZovoCard adapter ->
``app/devtools/mock_zovocard.py``; the proxy runs on the same database."""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text

from app.models import CardStock
from app.procurement import redeem_log as rl
from app.procurement.redeem_admin import sweep_attempts
from tests.test_procurement_cards import _order
from tests.test_procurement_direct import DUp, dup  # noqa: F401  (fixtures)
from tests.test_procurement_vcc import _sql, _zovo
from tests.test_redeem import SESSION, Rig

API = "/api/v1/admin/procurement/redeem"
AUTO = {"product": "claude", "plan": "pro", "card_mode": "auto_existing"}


@pytest.fixture
async def stack(env, dup):
    """Vendor mock + procurement internal app (``dup``) + the public redemption proxy on the same
    database, with the fingerprint sealer of the engine."""
    from tests.test_procurement_direct import _vendor

    vendor = _vendor()
    await _zovo(dup, vendor, AUTO)
    rig = Rig(env, vendor=dup.vendor_http, redeem_settings_ttl_s=0.0)
    rig.mock = vendor
    rig.ctx.sealer = dup.ctx.sealer
    rig.vendor_calls = []
    for name in ("post", "get"):
        orig = getattr(rig.vendor, name)

        async def counting(*a, __o=orig, **k):
            rig.vendor_calls.append(a[0] if a else "")
            return await __o(*a, **k)

        setattr(rig.vendor, name, counting)
    rig.dup = dup
    yield rig
    await rig.client.aclose()
    await rig.engine.dispose()


def _app_js() -> str:
    return (Path(__file__).resolve().parents[2] / "deploy" / "redeem" / "app.js").read_text()


async def _rows(env, sql="SELECT * FROM redeem_attempts ORDER BY at, id", **p):
    async with env.owner_session() as db:
        return [dict(r._mapping) for r in await db.execute(text(sql), p)]


async def _sell(env, code: str, *, delivered: bool = True) -> str:
    """A code we sold: sealed-stock row (fingerprint of the code) tied to a delivered order."""
    oid = await _order(env, env.tenant_id)
    async with env.owner_session() as db:
        pid = await db.scalar(text("SELECT product_id FROM orders WHERE id = :o"), {"o": oid})
        db.add(
            CardStock(
                id=uuid.uuid4(), tenant_id=env.tenant_id, product_id=pid, blob=b"x" * 64,
                fingerprint=env.res.sealer.fingerprint(str(env.tenant_id), code), kid="k",
                status="sold", order_id=oid,
            )
        )  # fmt: skip
        if delivered:
            await db.execute(
                text("UPDATE orders SET delivered_at = now(), status = 'delivered' WHERE id = :o"),
                {"o": oid},
            )
        await db.commit()
        return await db.scalar(text("SELECT order_no FROM orders WHERE id = :o"), {"o": oid})


# ============================================================================== pure helpers
def test_mask_code_never_shows_more_than_a_third():
    assert rl.mask_code("UUUPLUS-ABCDEF1234-0123456789") == ("UUUP", "6789")
    head, tail = rl.mask_code("ABCDEFGH")  # 8 chars -> 1 + 1
    assert (head, tail) == ("A", "H")
    for n in range(8, 80):
        code = "".join(chr(65 + i % 26) for i in range(n))
        h, t = rl.mask_code(code)
        assert len(h) + len(t) <= max(2, n // 3) and len(h) <= 4 and len(t) <= 4
    assert rl.mask_code("") == ("", "")
    assert rl.display_mask("UUUP", "6789") == "UUUP…6789" and rl.display_mask(None, None) == "…"


def test_ip_class_is_a_keyed_hash_of_the_network_never_the_address():
    key = b"k" * 32
    a = rl.ip_class_hash(key, "203.0.113.7")
    assert a == rl.ip_class_hash(key, "203.0.113.200")  # the same /24
    assert a != rl.ip_class_hash(key, "203.0.114.7")
    assert a != rl.ip_class_hash(b"z" * 32, "203.0.113.7")  # keyed
    assert "203" not in a and len(a) == 32
    v6 = rl.ip_class_hash(key, "2001:db8:1:2::1")
    assert v6 == rl.ip_class_hash(key, "2001:db8:1:ffff::9")  # the same /48
    assert v6 != rl.ip_class_hash(key, "2001:db8:2::1")
    assert rl.ip_class_hash(key, "::ffff:203.0.113.9") == a  # mapped v4 = v4
    assert rl.ip_class_hash(key, "not an ip") is None and rl.ip_class_hash(key, None) is None


def test_settings_are_normalised_clamped_and_plain_text():
    s = rl.normalize_settings(
        {
            "maintenance": "yes",  # not a real bool
            "announcement_en": "  Hello\x00\u202e  <b>world</b>\n\n" + "x" * 500,
            "announcement_zh": 7,
            "ip_limits": {"preview": 999, "redeem": 0, "result": True, "preflight": 5, "evil": 1},
        }
    )
    assert s["maintenance"] is False
    assert len(s["announcement_en"]) == rl.ANNOUNCE_MAX
    assert s["announcement_en"].startswith(
        "Hello <b>world</b> x"
    )  # text stays text (no markup rules)
    assert "\x00" not in s["announcement_en"] and "\u202e" not in s["announcement_en"]
    assert s["announcement_zh"] == ""
    assert s["ip_limits"] == {"preview": 30, "preflight": 5, "redeem": 1}
    assert rl.normalize_settings("garbage") == rl.default_settings()
    assert rl.normalize_settings(None)["ip_limits"] == {}
    cfg = rl.public_config(rl.default_settings())
    assert cfg["maintenance"] is False and cfg["maintenance_message"]["en"]


def test_stats_maths():
    assert rl.success_rate(0, 0) is None
    assert rl.success_rate(1, 2) == "0.3333"
    assert rl.success_rate(2, 1) == "0.6667"
    assert rl.success_rate(5, 0) == "1.0000"
    today = date(2026, 11, 17)
    out = rl.build_stats(
        [
            (date(2026, 11, 17), 4, 2, 1),
            (date(2026, 11, 15), 1, 0, 1),
            (date(2026, 10, 1), 9, 9, 9),
        ],
        [("declined", 2), ("code_unavailable", 5), ("declined", 1), ("timeout", 3)],
        3,
        today,
    )
    assert [d["day"] for d in out["days"]] == ["2026-11-15", "2026-11-16", "2026-11-17"]
    assert out["days"][1] == {
        "day": "2026-11-16",
        "attempts": 0,
        "completed": 0,
        "failed": 0,
        "success_rate": None,
    }
    assert out["totals"] == {
        "attempts": 5,
        "completed": 2,
        "failed": 2,
        "success_rate": "0.5000",
    }  # Oct 1 is outside
    assert out["top_reasons"][:3] == [
        {"reason": "code_unavailable", "count": 5},
        {"reason": "declined", "count": 3},
        {"reason": "timeout", "count": 3},
    ]


# ============================================================================== the proxy writes the log
async def test_a_walk_is_logged_masked_with_no_secret_in_any_column(env, stack, caplog):
    caplog.set_level(logging.DEBUG)
    rig = stack
    code, _results, tok, pf = await rig.walk()
    rows = await _rows(env)
    assert [r["step"] for r in rows][:3] == ["preview", "preflight", "redeem"]
    assert all(r["tenant_id"] == env.tenant_id for r in rows)
    head, tail = rl.mask_code(code)
    assert {(r["code_head"], r["code_tail"]) for r in rows} == {
        (head, tail)
    }  # later steps remember it
    assert all(r["code_fp"] == env.res.sealer.fingerprint(str(env.tenant_id), code) for r in rows)
    assert rows[0]["outcome"] == "ok" and rows[0]["plan"] and rows[1]["plan"] == rows[0]["plan"]
    blob = json.dumps(rows, default=str) + caplog.text
    for secret in (code, tok, pf, SESSION, "127.0.0.1", code.lower()):
        assert secret not in blob, "a secret reached the attempt log or the logs"
    assert all(r["ip_hash"] and re.fullmatch(r"[0-9a-f]{32}", r["ip_hash"]) for r in rows)
    assert rows[0]["ip_hash"] == rl.ip_class_hash(
        __import__("hmac")
        .new(rig.settings.procurement_token.encode(), b"redeem-ip-class", "sha256")
        .digest(),
        "127.0.0.1",
    )
    # the terminal result is logged once, not once per poll
    for _ in range(3):
        await rig.client.post("/redeem/result", json={"redemption_token": tok})
    terminal = [r for r in await _rows(env) if r["step"] == "result"]
    assert len(terminal) <= 1


async def test_failures_are_logged_with_a_coarse_reason(env, stack):
    rig = stack
    assert (await rig.post("preview", {"code": "MOCKPLUS-NOSUCHCODE1"})).status_code == 422
    assert (await rig.post("preview", {"code": "x"})).status_code == 400
    (bad, invalid) = await _rows(env)
    assert (bad["step"], bad["outcome"], bad["reason"]) == ("preview", "error", "code_unavailable")
    assert (invalid["outcome"], invalid["reason"]) == ("invalid", "invalid_request")
    assert invalid["code_head"] is None  # a malformed input is not recorded, not even masked


async def test_the_log_budget_bounds_failure_rows_per_client(env, stack):
    rig = stack
    for _ in range(40):
        await rig.post("preview", {"code": "x"})
    assert len(await _rows(env)) == 30  # 30 non-ok rows a minute per client class


async def test_the_log_budget_is_per_network_and_the_tenant_has_a_ceiling(env, stack, monkeypatch):
    import httpx

    from app.procurement import redeem as rd
    from app.procurement.redeem import create_redeem_app

    rig = stack
    rig.ctx.settings = replace(rig.ctx.settings, redeem_trusted_proxies="127.0.0.1/32")
    app = create_redeem_app(rig.ctx)
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://redeem.test")
    try:
        # 40 addresses inside one /48 (and so one /64) share one budget of 30 rows
        for i in range(40):
            h = {"x-forwarded-for": f"2001:db8:1:2::{i + 1:x}"}
            await c.post("/redeem/preview", json={"code": "x"}, headers=h)
        assert len(await _rows(env)) == 30
        # the tenant ceiling: 5 more rows across other networks, then nothing is written
        await _sql(env, "DELETE FROM redeem_attempts")
        await rig.ctx.redis.flushdb()
        monkeypatch.setattr(rd, "LOG_TENANT_PER_MIN", 5)
        for i in range(12):
            h = {"x-forwarded-for": f"203.0.{i}.9"}
            await c.post("/redeem/preview", json={"code": "x"}, headers=h)
        assert len(await _rows(env)) == 5
    finally:
        await c.aclose()


async def test_without_a_sealer_the_log_still_works_with_no_fingerprint(env, stack):
    stack.ctx.sealer = None
    await stack.walk()
    rows = await _rows(env)
    assert rows and all(r["code_fp"] is None and r["code_head"] for r in rows)


async def test_a_log_write_failure_never_changes_the_buyers_answer(env, stack):
    await _sql(env, "REVOKE INSERT ON redeem_attempts FROM app_procurement")
    try:
        code = stack.add_cdk()
        r = await stack.post("preview", {"code": code})
        assert r.status_code == 200
    finally:
        await _sql(env, "GRANT INSERT ON redeem_attempts TO app_procurement")


# ============================================================================== maintenance, announcement, limits
async def _set_settings(env, doc: dict | None):
    await _sql(
        env, "UPDATE tenants SET redeem_settings = CAST(:d AS jsonb) WHERE id = :t",
        d=json.dumps(doc) if doc is not None else None, t=env.tenant_id,
    )  # fmt: skip


async def test_maintenance_blocks_every_vendor_call_and_is_logged(env, stack):
    rig = stack
    code = rig.add_cdk()
    await _set_settings(env, {"maintenance": True})
    for step, body in (
        ("preview", {"code": code}),
        (
            "preflight",
            {"redemption_token": "t" * 20, "credential": {"mode": "session", "session": SESSION}},
        ),
        (
            "redeem",
            {"redemption_token": "t" * 20, "preflight_token": "p" * 20, "client_request_id": "r1"},
        ),
        ("result", {"redemption_token": "t" * 20}),
    ):
        r = await rig.post(step, body)
        assert r.status_code == 503 and r.json()["error"]["code"] == "maintenance", step
    assert rig.vendor_calls == []  # nothing reached the vendor
    assert {r["outcome"] for r in await _rows(env)} == {"maintenance"}
    await _set_settings(env, {"maintenance": False})
    assert (await rig.post("preview", {"code": code})).status_code == 200
    assert rig.vendor_calls


async def test_result_of_an_active_token_still_works_in_maintenance(env, stack):
    rig = stack
    code = rig.add_cdk()
    tok = (await rig.post("preview", {"code": code})).json()["data"]["redemption_token"]
    await _set_settings(env, {"maintenance": True})
    rig.vendor_calls.clear()
    r = await rig.post("result", {"redemption_token": tok})
    assert r.status_code != 503, r.text  # a read of a session we issued
    assert (await rig.post("result", {"redemption_token": "t" * 20})).status_code == 503
    assert (await rig.post("preview", {"code": code})).status_code == 503
    cfg = await rig.client.get("/redeem/config")
    assert cfg.headers["cache-control"] == "public, max-age=30"
    assert (await rig.post("preview", {"code": "x"})).headers["cache-control"] == "no-store"


def test_text_filter_strips_invisible_characters_and_links_are_refused():
    dirty = "a\u061cb\u180ec\u2060d\u2064e\ufff9f\U000e0041g\u202eh"
    out = rl.clean_text(dirty, 100)
    assert re.fullmatch(r"[a-h ]+", out), repr(out)
    assert rl.has_link("see https://x.example", "") and rl.has_link("WWW.evil.test")
    assert not rl.has_link("Back at 10:00 UTC", None)
    # re-check of #49: invisible characters or spaces inside the scheme separator do not hide a link
    for sneaky in ("https:\u200b//evil.example", "https: //evil.example", "w\u2060ww.evil.test",
                   "https:\u202e//x", "ht tps : / / x", "w w w .evil.test"):  # fmt: skip
        assert rl.has_link(sneaky), repr(sneaky)
    assert not rl.has_link(rl.clean_text("Back at 10:00 UTC \u200b today", 100))


async def test_links_in_announcement_text_are_refused(env, stack):
    admin, _ = await env.login("admin")
    for k in ("announcement_en", "maintenance_zh"):
        r = await admin.put(f"{API}/settings", json={k: "go to www.evil.test now"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "announcement_no_links"
    r = await admin.put(f"{API}/settings", json={"announcement_en": "see http://x.test"})
    assert r.status_code == 422
    r = await admin.put(
        f"{API}/settings", json={"announcement_en": "see https:\u200b//evil.example"}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "announcement_no_links"


async def test_public_config_serves_text_only_and_the_page_renders_it_as_text(env, stack):
    rig = stack
    r = await rig.client.get("/redeem/config")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["maintenance"] is False and d["announcement"] == {"en": "", "zh": ""}
    await _set_settings(
        env,
        {
            "maintenance": True,
            "announcement_en": "<img src=x onerror=alert(1)>" + "y" * 400,
            "announcement_zh": "公告\x07",
        },
    )
    d = (await rig.client.get("/redeem/config")).json()["data"]
    assert d["maintenance"] is True and len(d["announcement"]["en"]) == rl.ANNOUNCE_MAX
    assert d["announcement"]["zh"] == "公告"  # control characters are gone
    assert d["maintenance_message"]["en"] and d["maintenance_message"]["zh"]
    assert rig.client.headers.get("origin") is None
    assert (await rig.client.post("/redeem/config")).status_code == 405
    # the page: text nodes only, never innerHTML / insertAdjacentHTML / document.write
    js = _app_js()
    assert "/redeem/config" in js and "announce" in js
    assert not re.search(r"innerHTML|insertAdjacentHTML|document\.write|outerHTML", js)
    assert re.search(
        r"el\.textContent = text \? prefix \+ Array\.from\(String\(text\)\)\.slice\(0, 400\)", js
    )
    assert "setAttribute('role', 'alert')" in js and "t('maint')" in js


async def test_per_ip_limits_come_from_the_settings_within_bounds(env, stack):
    rig = stack
    await _set_settings(env, {"ip_limits": {"preview": 2, "redeem": 9999}})
    code = rig.add_cdk()
    codes = [200, 200, 429]
    got = [(await rig.post("preview", {"code": code})).status_code for _ in range(3)]
    assert got == codes
    assert (
        rl.normalize_settings({"ip_limits": {"redeem": 9999}})["ip_limits"]["redeem"] == 20
    )  # the vendor's own limit


# ============================================================================== retention
async def _aged(env, days: int, step: str = "preview", outcome: str = "ok", reason=None) -> None:
    """A log row of a given age (the table has no UPDATE policy, so age is set on insert)."""
    await _sql(
        env,
        "INSERT INTO redeem_attempts (id, tenant_id, at, step, outcome, reason) VALUES "
        "(gen_random_uuid(), :t, now() - make_interval(days => :d), :s, :o, :r)",
        t=env.tenant_id, d=days, s=step, o=outcome, r=reason,
    )  # fmt: skip


async def test_retention_deletes_only_rows_older_than_90_days(env, stack):
    await stack.walk()
    for days in (91, 120, 365):
        await _aged(env, days)
    await _aged(env, 89, "preflight")
    before = await _rows(env)
    assert await sweep_attempts(stack.dup.ctx) == 3
    left = await _rows(env)
    assert len(left) == len(before) - 3 and len(left) == 5
    # the engine's own sweeper runs it too
    await _aged(env, 100)
    from app.procurement.engine import sweep_credentials

    await sweep_credentials(stack.dup.ctx, force=True)
    assert len(await _rows(env)) == 5


async def test_the_api_role_can_neither_edit_nor_delete_the_log(env, stack):
    await stack.walk()
    async with env.api_session() as db:
        for sql in (
            "DELETE FROM redeem_attempts",
            "UPDATE redeem_attempts SET step = 'redeem'",
            "INSERT INTO redeem_attempts (id, tenant_id, step, outcome) VALUES (gen_random_uuid(), :t, 'preview', 'ok')",
        ):
            with pytest.raises(Exception, match="permission denied"):
                await db.execute(text(sql), {"t": env.tenant_id})
            await db.rollback()


# ============================================================================== authorization + tenants
async def test_roles_buyer_agent_admin_and_foreign_tenant(env, stack):
    rig = stack
    await rig.walk()
    buyer, _ = await env.login("buyer")
    agent, _ = await env.login("agent")
    admin, _ = await env.login("admin")
    foreign, _ = await env.login("admin", tenant_id=env.other_tenant_id)
    reads = [("GET", f"{API}/attempts"), ("GET", f"{API}/stats")]
    for c, ok in ((buyer, False), (agent, True), (admin, True), (foreign, False)):
        for m, url in reads:
            got = (await c.request(m, url)).status_code
            assert got == 200 if ok else got in (401, 403), (m, url, ok)
        r = await c.post(f"{API}/lookup", json={"order_ref": "NOPE"})
        assert r.status_code == 404 if ok else r.status_code in (401, 403)
    code = rig.add_cdk()
    admin_only = [
        ("GET", f"{API}/settings", None),
        ("PUT", f"{API}/settings", {"maintenance": True}),
        ("POST", f"{API}/cdk/disable", {"code": code, "confirm": True}),
        ("POST", f"{API}/cdk/enable", {"code": code, "confirm": True}),
    ]
    for c in (buyer, agent, foreign):  # agents read, but never configure or write at the vendor
        for m, url, body in admin_only:
            assert (await c.request(m, url, json=body)).status_code in (401, 403), (m, url)
    assert rig.mock.cdks[next(iter(rig.mock.cdks))]["status"] == "unused"
    assert (await admin.get(f"{API}/settings")).status_code == 200
    assert (
        await env.scalar("SELECT count(*) FROM audit_log WHERE action LIKE 'procurement.redeem_%'")
        == 0
    )


async def test_the_log_and_lookup_are_tenant_scoped(env, stack):
    rig = stack
    code, *_ = await rig.walk()
    foreign_tid = env.other_tenant_id
    # a row of ANOTHER tenant with the same code fingerprint must never show up
    async with env.owner_session(foreign_tid) as db:
        await db.execute(
            text(
                "INSERT INTO redeem_attempts (id, tenant_id, step, outcome, code_head, code_tail, code_fp) "
                "VALUES (gen_random_uuid(), :t, 'preview', 'ok', 'EVIL', 'EVIL', :fp)"
            ),
            {"t": foreign_tid, "fp": env.res.sealer.fingerprint(str(env.tenant_id), code)},
        )
        await db.commit()
    admin, _ = await env.login("admin")
    items = (await admin.get(f"{API}/attempts")).json()["items"]
    assert items and all(i["code"] != "EVIL…EVIL" for i in items)
    out = (await admin.post(f"{API}/lookup", json={"code": code})).json()
    assert all("EVIL" not in json.dumps(a) for a in out["attempts"])
    # and another tenant's admin sees none of ours (platform-only: refused outright)
    fadmin, _ = await env.login("admin", tenant_id=foreign_tid)
    assert (await fadmin.get(f"{API}/attempts")).status_code in (401, 403)
    # row-level security, below the API: the API role pinned to the other tenant sees only its row
    async with env.api_session(foreign_tid) as db:
        seen = list(await db.scalars(text("SELECT tenant_id FROM redeem_attempts")))
    assert seen == [foreign_tid]
    async with env.api_session() as db:
        assert env.other_tenant_id not in set(
            await db.scalars(text("SELECT tenant_id FROM redeem_attempts"))
        )
    # an order of the other tenant cannot be looked up by reference
    foreign_order = await _order(env, foreign_tid)
    async with env.owner_session(foreign_tid) as db:
        ref = await db.scalar(
            text("SELECT order_no FROM orders WHERE id = :o"), {"o": foreign_order}
        )
    assert ref
    assert (await admin.post(f"{API}/lookup", json={"order_ref": ref})).status_code == 404
    assert (await admin.get(f"{API}/attempts", params={"order_ref": ref})).json()["total"] == 0


# ============================================================================== the console log
async def test_attempts_search_filters_and_pagination(env, stack):
    rig = stack
    code, *_ = await rig.walk()
    order_no = await _sell(env, code)
    await rig.post("preview", {"code": "MOCKPLUS-NOSUCHCODE1"})
    head, tail = rl.mask_code(code)
    admin, _ = await env.login("admin")
    allrows = (await admin.get(f"{API}/attempts")).json()
    assert allrows["total"] >= 5 and allrows["retention_days"] == 90
    mine = (await admin.get(f"{API}/attempts", params={"q": f"{head}…{tail}"})).json()
    assert mine["total"] >= 3 and all(i["code"] == f"{head}…{tail}" for i in mine["items"])
    assert all(i["order_no"] == order_no for i in mine["items"])  # resolved through the fingerprint
    assert (await admin.get(f"{API}/attempts", params={"q": tail})).json()["total"] >= 3
    by_order = (await admin.get(f"{API}/attempts", params={"order_ref": order_no})).json()
    assert by_order["total"] == mine["total"]
    assert (await admin.get(f"{API}/attempts", params={"order_ref": "G000NOSUCH"})).json()[
        "total"
    ] == 0
    err = (await admin.get(f"{API}/attempts", params={"outcome": "error"})).json()
    assert err["total"] == 1 and err["items"][0]["reason"] == "code_unavailable"
    assert (await admin.get(f"{API}/attempts", params={"step": "redeem"})).json()["total"] == 1
    today = date.today().isoformat()
    assert (
        await admin.get(f"{API}/attempts", params={"date_from": today, "date_to": today})
    ).json()["total"] == allrows["total"]
    assert (await admin.get(f"{API}/attempts", params={"date_to": "2001-01-01"})).json()[
        "total"
    ] == 0
    p1 = (await admin.get(f"{API}/attempts", params={"page_size": 2, "page": 1})).json()
    p2 = (await admin.get(f"{API}/attempts", params={"page_size": 2, "page": 2})).json()
    assert len(p1["items"]) == 2 and {i["id"] for i in p1["items"]}.isdisjoint(
        {i["id"] for i in p2["items"]}
    )
    assert (await admin.get(f"{API}/attempts", params={"outcome": "nonsense"})).status_code == 422
    blob = json.dumps(allrows)
    for secret in (code, SESSION, "127.0.0.1"):
        assert secret not in blob


async def test_stats_endpoint_matches_the_rows(env, stack):
    rig = stack
    await rig.walk()
    await rig.post("preview", {"code": "MOCKPLUS-NOSUCHCODE1"})
    await _aged(env, 0, "result", "failed", "declined")
    await _aged(env, 0, "result", "completed")
    await _aged(env, 40, "redeem", "error", "rejected")
    admin, _ = await env.login("agent")
    s = (await admin.get(f"{API}/stats")).json()
    assert len(s["days"]) == 30 and s["days"][-1]["day"] == date.today().isoformat()
    done = sum(d["completed"] for d in s["days"])
    failed = sum(d["failed"] for d in s["days"])
    ok_terminal = len([r for r in await _rows(env) if r["outcome"] == "completed"])
    assert done == ok_terminal and failed == 1  # the 40-day-old redeem error is outside the window
    assert s["totals"]["attempts"] == 2 and s["totals"]["success_rate"] == rl.success_rate(
        done, failed
    )
    assert {"reason": "declined", "count": 1} in s["top_reasons"]
    assert (await admin.get(f"{API}/stats", params={"days": 91})).status_code == 422
    assert len((await admin.get(f"{API}/stats", params={"days": 7})).json()["days"]) == 7


# ============================================================================== lookup
async def test_lookup_by_code_shows_our_side_and_the_vendor_status(env, stack, caplog):
    caplog.set_level(logging.DEBUG)
    rig = stack
    code = rig.add_cdk()
    order_no = await _sell(env, code)
    r = await rig.post("preview", {"code": code})
    assert r.status_code == 200
    admin, _ = await env.login("agent")
    r = await admin.post(f"{API}/lookup", json={"code": code})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["sold_by_us"] and out["ours"]["order_no"] == order_no
    assert (
        out["ours"]["delivered_at"]
        and out["ours"]["buyer"].endswith("@example.com")
        and "***" in out["ours"]["buyer"]
    )
    assert (
        out["vendor"]["available"]
        and out["vendor"]["found"]
        and out["vendor"]["status"] == "unused"
    )
    assert [a["step"] for a in out["attempts"]] == ["preview"]
    assert code not in r.text and code not in caplog.text
    # a code that is not ours still gets the vendor view, and an unknown one is "not found"
    other = rig.add_cdk()
    out = (await admin.post(f"{API}/lookup", json={"code": other})).json()
    assert not out["sold_by_us"] and out["vendor"]["found"]
    out = (await admin.post(f"{API}/lookup", json={"code": "MOCKPLUS-NOSUCHCODE9"})).json()
    assert out["vendor"] == {
        "available": True,
        "found": False,
        "status": None,
        "plan": None,
        "env": "sandbox",
    }
    # by order: our side only (the order holds a sealed code we cannot read), no vendor call
    n = len(rig.mock.requests)
    out = (await admin.post(f"{API}/lookup", json={"order_ref": order_no})).json()
    assert (
        out["ours"]["order_no"] == order_no and out["vendor"] is None and len(out["attempts"]) == 1
    )
    assert len(rig.mock.requests) == n
    for bad in (
        {},
        {"code": code, "order_ref": order_no},
        {"code": "short"},
        {"code": "bad code!!!!"},
        {"x": 1},
    ):
        assert (await admin.post(f"{API}/lookup", json=bad)).status_code in (422,), bad


async def test_lookup_when_procurement_is_unreachable_is_not_an_error(env, stack):
    code = stack.add_cdk()
    admin, _ = await env.login("admin")
    saved = env.res.settings
    env.res.settings = saved.model_copy(update={"procurement_token": ""})
    try:
        out = (await admin.post(f"{API}/lookup", json={"code": code})).json()
    finally:
        env.res.settings = saved
    assert out["vendor"]["available"] is False


# ============================================================================== disable / enable
async def test_disable_and_enable_are_confirmed_audited_and_never_store_the_code(
    env, stack, caplog
):
    caplog.set_level(logging.DEBUG)
    rig = stack
    code = rig.add_cdk()
    order_no = await _sell(env, code)
    admin, user = await env.login("admin")
    for bad in (
        {"code": code},
        {"code": code, "confirm": False},
        {"code": code, "confirm": True, "id": 1},
    ):
        assert (await admin.post(f"{API}/cdk/disable", json=bad)).status_code == 422
    assert not [x for x in rig.mock.requests if x[0] != "GET"]
    r = await admin.post(f"{API}/cdk/disable", json={"code": code, "confirm": True})
    assert r.status_code == 200 and r.json()["after"] == {"status": "disabled"}, r.text
    cdk = next(iter(rig.mock.cdks.values()))
    assert cdk["status"] == "disabled"
    # a redemption of a disabled code is refused by the vendor
    assert (await rig.post("preview", {"code": code})).status_code == 422
    # disabling twice: the vendor refuses (not unused)
    r = await admin.post(f"{API}/cdk/disable", json={"code": code, "confirm": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "cdk_rejected"
    r = await admin.post(f"{API}/cdk/enable", json={"code": code, "confirm": True})
    assert r.status_code == 200 and cdk["status"] == "unused"
    assert (
        await admin.post(
            f"{API}/cdk/disable",
            json={"code": "MOCKPLUS-NOSUCHCODE9", "confirm": True, "force_unsold": True},
        )
    ).status_code == 404  # sold-check passed by force; the vendor has no such code
    rows = await _rows(
        env,
        "SELECT action, actor_id, before, after, detail, severity FROM audit_log WHERE action LIKE 'procurement.redeem_cdk_%' ORDER BY id",
    )
    assert [r["action"] for r in rows] == [
        "procurement.redeem_cdk_disabled",
        "procurement.redeem_cdk_enabled",
    ]
    assert rows[0]["before"] == {"status": "unused"} and rows[0]["after"] == {"status": "disabled"}
    assert rows[1]["before"] == {"status": "disabled"} and rows[1]["after"] == {"status": "unused"}
    assert rows[0]["actor_id"] == user.id and rows[0]["detail"]["order_no"] == order_no
    head, tail = rl.mask_code(code)
    assert rows[0]["detail"]["code"] == f"{head}…{tail}"
    everything = (
        json.dumps(rows, default=str) + caplog.text + json.dumps(await _rows(env), default=str)
    )
    assert code not in everything and code.lower() not in everything
    assert (
        await _rows(
            env,
            "SELECT 1 FROM audit_log WHERE detail::text LIKE :c OR after::text LIKE :c",
            c=f"%{code}%",
        )
        == []
    )


async def test_disable_requires_that_we_sold_the_code_unless_forced_and_audited(env, stack):
    rig = stack
    code = rig.add_cdk()  # at the vendor, never sold by us
    admin, _ = await env.login("admin")
    for action in ("disable", "enable"):
        r = await admin.post(f"{API}/cdk/{action}", json={"code": code, "confirm": True})
        assert r.status_code == 409 and r.json()["error"]["code"] == "code_not_ours", action
    assert not [x for x in rig.mock.requests if x[0] != "GET"]
    assert (
        await env.scalar(
            "SELECT count(*) FROM audit_log WHERE action LIKE 'procurement.redeem_cdk_%'"
        )
        == 0
    )
    r = await admin.post(
        f"{API}/cdk/disable", json={"code": code, "confirm": True, "force_unsold": True}
    )
    assert r.status_code == 200, r.text
    (row,) = await _rows(
        env,
        "SELECT detail, severity FROM audit_log WHERE action = 'procurement.redeem_cdk_disabled'",
    )
    assert row["severity"] == "warning" and row["detail"]["force_unsold"] is True
    assert code not in json.dumps(row, default=str)
    # a code we sold carries no force marker
    sold = rig.add_cdk()
    await _sell(env, sold)
    assert (
        await admin.post(f"{API}/cdk/disable", json={"code": sold, "confirm": True})
    ).status_code == 200
    rows = await _rows(
        env,
        "SELECT detail FROM audit_log WHERE action = 'procurement.redeem_cdk_disabled' ORDER BY id",
    )
    assert "force_unsold" not in rows[1]["detail"]


async def test_a_production_supplier_is_never_written_to(env, stack):
    rig = stack
    code = rig.add_cdk()
    await _sell(env, code)
    await _sql(env, "UPDATE suppliers SET env = 'production'")
    assert rig.dup.ctx.settings.procurement_allow_production_writes is False
    admin, _ = await env.login("admin")
    for action in ("disable", "enable"):
        r = await admin.post(f"{API}/cdk/{action}", json={"code": code, "confirm": True})
        assert r.status_code == 409 and r.json()["error"]["code"] == "production_writes_disabled", (
            action
        )
    assert not [x for x in rig.mock.requests if x[0] != "GET"]  # not one write reached the vendor
    assert (
        await env.scalar(
            "SELECT count(*) FROM audit_log WHERE action LIKE 'procurement.redeem_cdk_%'"
        )
        == 0
    )
    assert (
        await admin.post(f"{API}/lookup", json={"code": code})
    ).status_code == 200  # reads stay open
    rig.dup.ctx.settings = replace(rig.dup.ctx.settings, procurement_allow_production_writes=True)
    assert (
        await admin.post(f"{API}/cdk/disable", json={"code": code, "confirm": True})
    ).status_code == 200


async def test_internal_redeem_routes_need_the_token(env, stack):
    for path in ("cdk-status", "cdk-set"):
        r = await env.res.procurement.post(
            f"/internal/redeem/{path}", json={"tenant_id": str(env.tenant_id), "code": "MOCKPLUS-ABCDEF123", "action": "disable"},
            headers={"X-Internal-Token": "x" * 40},
        )  # fmt: skip
        assert r.status_code == 401
    assert not [x for x in stack.mock.requests if x[0] != "GET"]


# ============================================================================== settings
async def test_settings_are_admin_only_validated_clamped_audited_and_reach_the_proxy(env, stack):
    rig = stack
    admin, _ = await env.login("admin")
    s = (await admin.get(f"{API}/settings")).json()
    assert s["maintenance"] is False and s["bounds"]["result"] == {"min": 1, "max": 60}
    assert s["caps"]["announcement"] == 300 and s["defaults"]["preview"] == 10
    r = await admin.put(
        f"{API}/settings",
        json={
            "announcement_en": "Back at 10:00 <b>UTC</b>\x07",
            "announcement_zh": "10:00 恢复",
            "ip_limits": {"preview": 4},
        },
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["announcement_en"] == "Back at 10:00 <b>UTC</b>" and out["ip_limits"] == {
        "preview": 4
    }
    # the proxy sees it without any push
    cfg = (await rig.client.get("/redeem/config")).json()["data"]
    assert cfg["announcement"]["zh"] == "10:00 恢复"
    for bad in (
        {"announcement_en": "x" * 301},
        {"maintenance_zh": "x" * 201},
        {"ip_limits": {"preview": 31}},
        {"ip_limits": {"redeem": 0}},
        {"ip_limits": {"bogus": 3}},
        {"maintenance": "yes"},
        {"unknown": 1},
        {},
    ):
        assert (await admin.put(f"{API}/settings", json=bad)).status_code == 422, bad
    # merge semantics: a null limit returns to the default, other fields stay
    out = (
        await admin.put(
            f"{API}/settings", json={"ip_limits": {"preview": None}, "maintenance": True}
        )
    ).json()
    assert (
        out["ip_limits"] == {}
        and out["maintenance"] is True
        and out["announcement_zh"] == "10:00 恢复"
    )
    rows = await _rows(
        env,
        "SELECT action, before, after, severity FROM audit_log WHERE action = 'procurement.redeem_settings_updated' ORDER BY id",
    )
    assert len(rows) == 2 and rows[0]["before"]["announcement_en"] == ""
    assert rows[1]["before"]["maintenance"] is False and rows[1]["after"]["maintenance"] is True
    assert rows[1]["severity"] == "warning"
    assert (
        await rig.post("preview", {"code": rig.add_cdk()})
    ).status_code == 503  # maintenance is live


async def test_the_proxy_role_reads_only_the_redeem_settings_column_of_tenants(env, stack):
    from app.core.db import tenant_session

    async with tenant_session(stack.dup.ctx.maker, None) as db:
        with pytest.raises(Exception, match="permission denied"):
            await db.execute(text("SELECT settings FROM tenants"))
        await db.rollback()
        await db.execute(
            text("SELECT id, kind, redeem_settings FROM tenants")
        )  # the one it may read
        with pytest.raises(Exception, match="permission denied"):
            await db.execute(text("UPDATE tenants SET redeem_settings = NULL"))
