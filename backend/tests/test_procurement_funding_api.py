"""ADR-048 console: automatic top-up settings behind a fresh 2FA code; the top-up log."""

from __future__ import annotations

import uuid

from tests.test_procurement_vcc import _rows
from tests.test_twofactor import code, enrol

URL = "/api/v1/admin/procurement/funding"


async def test_read_defaults_off_with_the_owners_card_ranges(env):
    staff, _ = await env.login("agent")
    r = await staff.get(URL)
    assert r.status_code == 200, r.text
    st = r.json()["settings"]
    assert st["auto_recharge"] is False
    assert st["bin_priority"][:2] == ["532604", "537882"]
    assert r.json()["today"]["spent_usd"] == "0.00" and r.json()["topups"] == []


async def test_a_change_needs_an_admin_session_with_a_fresh_code(env):
    admin, _u, secret, recovery = await enrol(env, role="admin", email="owner@example.com")
    body = {"auto_recharge": True, "daily_cap_usd": "40", "bin_priority": ["537872", "474613"]}
    # no code / a wrong code / a recovery code: refused, nothing changes
    assert (await admin.put(URL, json=body)).status_code == 422
    r = await admin.put(URL, json={**body, "totp_code": "000000"})
    # 422, never 401: a 401 signs the console out (the 2026-11-19 login loop)
    assert r.status_code == 422 and r.json()["error"]["code"] == "two_factor_invalid"
    assert (await admin.get(URL)).status_code == 200  # still signed in
    assert (await admin.put(URL, json={**body, "totp_code": recovery[0]})).status_code == 422
    assert (await admin.get(URL)).json()["settings"]["auto_recharge"] is False
    # a bad value is refused before the code is spent
    bad = await admin.put(URL, json={"bin_priority": ["12"], "totp_code": code(secret, 1)})
    assert bad.status_code == 422
    r = await admin.put(URL, json={**body, "totp_code": code(secret, 1)})
    assert r.status_code == 200, r.text
    st = r.json()["settings"]
    assert st["auto_recharge"] is True and st["daily_cap_usd"] == "40.00"
    assert st["bin_priority"] == ["537872", "474613"]
    # the same code never works twice, and the answer says so (wait for the next code)
    again = await admin.put(URL, json={"auto_recharge": False, "totp_code": code(secret, 1)})
    assert again.status_code == 422 and again.json()["error"]["code"] == "two_factor_used"
    (row,) = await _rows(
        env,
        "SELECT before, after, detail, severity FROM audit_log "
        "WHERE action = 'procurement.funding_settings_updated'",
    )
    assert row[0]["auto_recharge"] is False and row[1]["auto_recharge"] is True
    assert row[2]["two_factor"] is True and row[3] == "warning"
    assert secret not in str(row) and "totp" not in str(row[1])


async def test_without_two_factor_or_as_staff_nothing_changes(env):
    admin, _ = await env.login("admin", email="no2fa@example.com")
    r = await admin.put(URL, json={"auto_recharge": True, "totp_code": "123456"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "two_factor_not_enabled"
    staff, _ = await env.login("agent", email="clerk@example.com")
    r = await staff.put(URL, json={"auto_recharge": True, "totp_code": "123456"})
    assert r.status_code == 403
    buyer, _ = await env.login("buyer")
    assert (await buyer.get(URL)).status_code in (401, 403)


async def test_the_log_shows_only_this_tenants_top_ups(env):
    from sqlalchemy import text

    for tid, key in ((env.tenant_id, "k-mine"), (env.other_tenant_id, "k-theirs")):
        async with env.owner_session(tid) as db:
            sid = await db.scalar(
                text(
                    "INSERT INTO suppliers (id, tenant_id, code, adapter, label, settings) "
                    "VALUES (gen_random_uuid(), :t, 'zovocard', 'zovocard', 'Z', '{}') "
                    "RETURNING id"
                ),
                {"t": tid},
            )
            await db.execute(
                text(
                    "INSERT INTO procurement_fundings (id, tenant_id, supplier_id, card_id, "
                    "amount, state, idem_key) VALUES (:i, :t, :s, :c, 10, 'done', :k)"
                ),
                {"i": uuid.uuid4(), "t": tid, "s": sid, "k": key,
                 "c": "921097" if tid == env.tenant_id else "555"},
            )  # fmt: skip
            await db.commit()
    staff, _ = await env.login("agent")
    data = (await staff.get(URL)).json()
    assert [t["card_id"] for t in data["topups"]] == ["921097"]
    assert data["today"]["spent_usd"] == "10.00"


async def test_the_code_used_to_sign_in_is_reported_as_used_not_wrong(env):
    """The owner's loop (2026-11-19): signed in with code X, then typed X again to save."""
    from tests.test_twofactor import password_step

    _, _u, secret, _ = await enrol(env, role="admin", email="loop@example.com")
    c, r = await password_step(env, "loop@example.com")
    login_code = code(secret, 1)
    pending = r.json()["pending_token"]
    r = await c.post("/api/v1/auth/login/2fa", json={"pending_token": pending, "code": login_code})
    assert r.status_code == 200, r.text
    r = await c.put(URL, json={"auto_recharge": True, "totp_code": login_code})
    assert r.status_code == 422 and r.json()["error"]["code"] == "two_factor_used"
    assert (await c.get(URL)).status_code == 200  # the session survived
