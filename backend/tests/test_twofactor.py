"""Optional two-factor login: enrolment, the second login step, recovery codes, limits, reset."""

from __future__ import annotations

from app.core import totp
from tests.conftest import PASSWORD

LOGIN = "/api/v1/auth/login"
SECOND = "/api/v1/auth/login/2fa"


def code(secret: str, ahead: int = 0) -> str:
    return totp.code_at(secret, totp.step_at() + ahead)


async def enrol(env, role: str = "buyer", email: str = "tf@example.com"):
    """A signed-in user with 2FA on: (client, user, secret, recovery codes)."""
    c, u = await env.login(role, email=email)
    r = await c.post("/api/v1/me/2fa/setup", json={"password": PASSWORD})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    secret = r.json()["secret"]
    assert (
        r.json()["otpauth_uri"].startswith("otpauth://totp/") and secret in r.json()["otpauth_uri"]
    )
    r = await c.post("/api/v1/me/2fa/enable", json={"code": code(secret)})
    assert r.status_code == 200, r.text
    return c, u, secret, r.json()["recovery_codes"]


async def password_step(env, email: str = "tf@example.com"):
    c = env.client()
    r = await c.post(LOGIN, json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return c, r


async def test_setup_needs_the_password_and_a_valid_code(env):
    c, u = await env.login("buyer", email="tf@example.com")
    assert (await c.get("/api/v1/me/2fa")).json() == {
        "enabled": False,
        "enabled_at": None,
        "recovery_codes_left": 0,
    }
    r = await c.post("/api/v1/me/2fa/setup", json={"password": "wrong"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "invalid_password"
    r = await c.post("/api/v1/me/2fa/enable", json={"code": "123456"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "two_factor_not_enabled"
    secret = (await c.post("/api/v1/me/2fa/setup", json={"password": PASSWORD})).json()["secret"]
    # a started enrolment changes nothing at login
    _, r = await password_step(env)
    assert r.json()["id"] == str(u.id)
    wrong = "000000" if code(secret) != "000000" else "111111"
    r = await c.post("/api/v1/me/2fa/enable", json={"code": wrong})
    assert r.status_code == 401 and r.json()["error"]["code"] == "two_factor_invalid"
    # restarting replaces the pending secret; the old one no longer enables
    second = (await c.post("/api/v1/me/2fa/setup", json={"password": PASSWORD})).json()["secret"]
    assert second != secret
    r = await c.post("/api/v1/me/2fa/enable", json={"code": code(secret)})
    assert r.status_code == 401
    r = await c.post("/api/v1/me/2fa/enable", json={"code": code(second)})
    assert r.status_code == 200 and len(r.json()["recovery_codes"]) == 10
    st = (await c.get("/api/v1/me/2fa")).json()
    assert st["enabled"] is True and st["recovery_codes_left"] == 10 and st["enabled_at"]
    r = await c.post("/api/v1/me/2fa/setup", json={"password": PASSWORD})
    assert r.status_code == 409 and r.json()["error"]["code"] == "two_factor_enabled"


async def test_nothing_usable_is_stored_or_audited(env):
    _, _u, secret, recovery = await enrol(env)
    sealed = await env.scalar("SELECT encode(secret_sealed, 'escape') FROM user_totp")
    assert secret not in sealed and secret.lower() not in sealed.lower()
    hashes = await env.scalar("SELECT array_to_string(recovery_hashes, ',') FROM user_totp")
    assert len(hashes.split(",")) == 10
    dump = await env.scalar(
        "SELECT string_agg(audit_log::text, '|') FROM audit_log WHERE action LIKE 'auth.2fa%'"
    )
    assert "auth.2fa_enabled" in dump
    for secret_text in [secret, *recovery, recovery[0].replace("-", "")]:
        assert secret_text not in dump and secret_text not in hashes


async def test_login_needs_the_second_step_and_a_code_works_once(env):
    _, u, secret, _ = await enrol(env)
    c, r = await password_step(env)
    body = r.json()
    assert body["two_factor_required"] is True and body["expires_in"] == 300
    assert "id" not in body and "set-cookie" not in r.headers
    assert r.headers["cache-control"] == "no-store"
    assert (await c.get("/api/v1/auth/me")).status_code == 401
    assert await env.scalar("SELECT last_login_at FROM users WHERE id = :u", u=u.id) is None
    token = body["pending_token"]
    wrong = "000000" if "000000" not in (code(secret, 1), code(secret)) else "111111"
    r = await c.post(SECOND, json={"pending_token": token, "code": wrong})
    assert r.status_code == 401 and r.json()["error"]["code"] == "two_factor_invalid"
    # the code that enabled 2FA belongs to a used time step
    r = await c.post(SECOND, json={"pending_token": token, "code": code(secret)})
    assert r.status_code == 401
    good = code(secret, 1)
    r = await c.post(SECOND, json={"pending_token": token, "code": good})
    assert r.status_code == 200 and r.json()["id"] == str(u.id), r.text
    assert (await c.get("/api/v1/auth/me")).status_code == 200
    assert await env.scalar("SELECT last_login_at FROM users WHERE id = :u", u=u.id) is not None
    # the pending token is single use, and the accepted code cannot be replayed on a new one
    r = await env.client().post(SECOND, json={"pending_token": token, "code": good})
    assert r.status_code == 401 and r.json()["error"]["code"] == "two_factor_expired"
    c2, r = await password_step(env)
    r = await c2.post(SECOND, json={"pending_token": r.json()["pending_token"], "code": good})
    assert r.status_code == 401 and r.json()["error"]["code"] == "two_factor_invalid"
    assert (await c2.get("/api/v1/auth/me")).status_code == 401


async def test_recovery_code_logs_in_once(env):
    _, u, _secret, recovery = await enrol(env)
    c, r = await password_step(env)
    typed = recovery[3].upper().replace("-", " ")
    r = await c.post(SECOND, json={"pending_token": r.json()["pending_token"], "code": typed})
    assert r.status_code == 200 and r.json()["id"] == str(u.id), r.text
    assert (await c.get("/api/v1/me/2fa")).json()["recovery_codes_left"] == 9
    detail = await env.scalar(
        "SELECT detail::text FROM audit_log WHERE action = 'auth.login' "
        "AND detail->>'second_factor' = 'recovery'"
    )
    assert '"recovery_codes_left": 9' in detail and recovery[3] not in detail
    c2, r = await password_step(env)
    r = await c2.post(
        SECOND, json={"pending_token": r.json()["pending_token"], "code": recovery[3]}
    )
    assert r.status_code == 401 and r.json()["error"]["code"] == "two_factor_invalid"


async def test_pending_token_limits(env):
    _, _u, secret, _ = await enrol(env)
    r = await env.client().post(SECOND, json={"pending_token": "x" * 43, "code": "123456"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "two_factor_expired"
    from app.modules.auth import twofactor

    # a token that already took its five wrong codes is destroyed on the next try
    c, r = await password_step(env)
    token = r.json()["pending_token"]
    key = twofactor._pending_key(token)
    await env.res.redis.set(key + ":n", twofactor.PENDING_ATTEMPTS, ex=60)
    r = await c.post(SECOND, json={"pending_token": token, "code": code(secret, 1)})
    assert r.status_code == 401 and r.json()["error"]["code"] == "two_factor_expired"
    assert await env.res.redis.get(key) is None
    # a token of this tenant means nothing in another tenant
    c, r = await password_step(env)
    token = r.json()["pending_token"]
    async with env.api_session(env.other_tenant_id) as db:
        try:
            await twofactor.complete_login(
                db,
                env.res.redis,
                env.res.settings,
                env.other_tenant_id,
                token,
                code(secret, 1),
                None,
            )
            raise AssertionError("accepted in another tenant")
        except twofactor.TwoFactorExpired:
            pass
    # wrong codes count against the login limiter of the address (5 per minute)
    wrong = (
        "000000" if "000000" not in (code(secret, 1), code(secret), code(secret, -1)) else "111111"
    )
    for _ in range(5):
        r = await c.post(SECOND, json={"pending_token": token, "code": wrong})
        assert r.status_code == 401, r.text
    r = await c.post(SECOND, json={"pending_token": token, "code": code(secret, 1)})
    assert r.status_code == 429
    r = await env.client().post(LOGIN, json={"email": "tf@example.com", "password": PASSWORD})
    assert r.status_code == 429


async def test_disable_and_regenerate(env):
    c, u, secret, recovery = await enrol(env)
    # new recovery codes need the authenticator, not a recovery code
    r = await c.post("/api/v1/me/2fa/recovery-codes", json={"code": recovery[0]})
    assert r.status_code == 401
    r = await c.post("/api/v1/me/2fa/recovery-codes", json={"code": code(secret, 1)})
    assert r.status_code == 200, r.text
    fresh = r.json()["recovery_codes"]
    assert len(fresh) == 10 and not set(fresh) & set(recovery)
    r = await c.post("/api/v1/me/2fa/disable", json={"code": recovery[1]})  # replaced: dead
    assert r.status_code == 401
    r = await c.post("/api/v1/me/2fa/disable", json={"code": fresh[0]})
    assert r.status_code == 204
    assert await env.scalar("SELECT count(*) FROM user_totp") == 0
    _, r = await password_step(env)
    assert r.json()["id"] == str(u.id)
    r = await c.post("/api/v1/me/2fa/disable", json={"code": fresh[1]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "two_factor_not_enabled"


async def test_enabling_ends_other_sessions(env):
    c, u = await env.login("buyer", email="tf@example.com")
    other = env.client()
    other.cookies.set("sid", await env.res.sessions.create(u.id, u.tenant_id, u.role))
    assert (await other.get("/api/v1/auth/me")).status_code == 200
    secret = (await c.post("/api/v1/me/2fa/setup", json={"password": PASSWORD})).json()["secret"]
    assert (await c.post("/api/v1/me/2fa/enable", json={"code": code(secret)})).status_code == 200
    assert (await c.get("/api/v1/auth/me")).status_code == 200
    assert (await other.get("/api/v1/auth/me")).status_code == 401


async def test_admin_reset_and_tenant_isolation(env):
    c, u, _secret, _ = await enrol(env)
    agent, _ = await env.login("agent")
    r = await agent.post(f"/api/v1/admin/users/{u.id}/2fa/reset")
    assert r.status_code == 403
    # another tenant sees neither the row nor the user
    async with env.api_session(env.other_tenant_id) as db:
        from sqlalchemy import text

        assert (await db.execute(text("SELECT count(*) FROM user_totp"))).scalar() == 0
    foreign, _ = await env.login("admin", tenant_id=env.other_tenant_id)
    foreign_host = await foreign.post(f"/api/v1/admin/users/{u.id}/2fa/reset")
    assert foreign_host.status_code in (401, 403, 404)
    assert await env.scalar("SELECT count(*) FROM user_totp") == 1
    admin, a = await env.login("admin")
    r = await admin.post(f"/api/v1/admin/users/{a.id}/2fa/reset")
    assert r.status_code == 403
    peer = await env.user("admin")
    r = await admin.post(f"/api/v1/admin/users/{peer.id}/2fa/reset")
    assert r.status_code == 403  # an admin's second factor is not resettable from a session
    r = await admin.post(f"/api/v1/admin/users/{u.id}/2fa/reset")
    assert r.status_code == 200 and r.json() == {
        "id": str(u.id),
        "two_factor": False,
        "removed": True,
    }
    assert (await c.get("/api/v1/auth/me")).status_code == 401  # the user's sessions ended
    _, r = await password_step(env)
    assert r.json()["id"] == str(u.id)
    sev = await env.scalar("SELECT severity FROM audit_log WHERE action = 'user.2fa_reset'")
    assert sev == "critical"


async def test_staff_login_with_two_factor(env):
    _, _u, secret, _ = await enrol(env, role="admin", email="boss@example.com")
    c, r = await password_step(env, "boss@example.com")
    r = await c.post(
        SECOND, json={"pending_token": r.json()["pending_token"], "code": code(secret, 1)}
    )
    assert r.status_code == 200 and r.json()["role"] == "admin"
    assert "Max-Age=43200" in r.headers["set-cookie"]


async def test_google_sign_in_still_owes_the_second_factor(env, monkeypatch):
    from tests.test_v5_core import _claims, _google_callback, setup_google

    admin, _ = await env.login("admin")
    await setup_google(env, admin)
    # first Google sign-in creates the account; it then turns 2FA on (no password to confirm)
    c, r = await _google_callback(env, _claims(sub="g-2fa", email="g2fa@example.com"), monkeypatch)
    assert r.status_code == 302
    r = await c.post("/api/v1/me/2fa/setup", json={})
    assert r.status_code == 200, r.text
    secret = r.json()["secret"]
    assert (await c.post("/api/v1/me/2fa/enable", json={"code": code(secret)})).status_code == 200
    fresh, r = await _google_callback(
        env,
        _claims(sub="g-2fa", email="g2fa@example.com"),
        monkeypatch,
        start_params={"next": "/orders"},
    )
    assert r.status_code == 302
    where, _, fragment = r.headers["location"].partition("#")
    assert (
        where == "/login" and fragment.startswith("2fa=") and fragment.endswith("&next=%2Forders")
    )
    assert (await fresh.get("/api/v1/auth/me")).status_code == 401  # no session from Google alone
    token = fragment[4:].split("&")[0]
    r = await fresh.post(SECOND, json={"pending_token": token, "code": code(secret, 1)})
    assert r.status_code == 200 and r.json()["email"] == "g2fa@example.com"


async def test_google_takeover_of_an_unverified_account_drops_its_second_factor(env, monkeypatch):
    from tests.test_v5_core import _claims, _google_callback, setup_google

    admin, _ = await env.login("admin")
    await setup_google(env, admin)
    await enrol(env, email="victim@example.com")  # a squatter on an address they never proved
    c, r = await _google_callback(env, _claims(), monkeypatch)
    assert r.status_code == 302 and "#" not in r.headers["location"]
    assert (await c.get("/api/v1/auth/me")).status_code == 200
    assert await env.scalar("SELECT count(*) FROM user_totp") == 0


async def test_guess_budget_is_counted_before_the_check_and_tokens_are_capped(env):
    from app.core.errors import RateLimited
    from app.core.ratelimit import window_key
    from app.modules.auth import twofactor

    _, u, secret, _ = await enrol(env)
    keys = twofactor.limiter_keys(env.tenant_id, "tf@example.com", None)
    # ten failures used up for this account: even the right code is refused, and not consumed
    await env.res.redis.set(
        window_key("2fa:fail:user", str(u.id), twofactor.USER_FAILURES[1])[0],
        twofactor.USER_FAILURES[0],
        ex=60,
    )
    token = await twofactor.start_pending(env.res.redis, u, keys)
    async with env.api_session() as db:
        try:
            await twofactor.complete_login(
                db, env.res.redis, env.res.settings, env.tenant_id, token, code(secret, 1), None
            )
            raise AssertionError("accepted over the budget")
        except RateLimited:
            pass
    # a correct code gives its reserved unit back
    await env.res.redis.flushdb()
    token = await twofactor.start_pending(env.res.redis, u, keys)
    async with env.api_session() as db:
        await twofactor.complete_login(
            db, env.res.redis, env.res.settings, env.tenant_id, token, code(secret, 1), None
        )
    key = window_key("2fa:fail:user", str(u.id), twofactor.USER_FAILURES[1])[0]
    assert int(await env.res.redis.get(key) or 0) == 0
    # pending tokens per account are capped
    await env.res.redis.flushdb()
    for _ in range(twofactor.MINT_LIMIT[0]):
        await twofactor.start_pending(env.res.redis, u, keys)
    try:
        await twofactor.start_pending(env.res.redis, u, keys)
        raise AssertionError("minted over the cap")
    except RateLimited:
        pass


async def test_password_reset_of_an_unverified_account_drops_its_second_factor(env):
    from app.modules.auth import codes

    await enrol(env, email="victim@example.com")  # a squatter on an address they never proved
    assert (
        await env.scalar("SELECT email_verified FROM users WHERE email='victim@example.com'")
        is False
    )

    async def yes(*a, **k):
        return True

    orig, codes.verify = codes.verify, yes
    try:
        async with env.api_session() as db:
            await codes.reset_password(
                db, env.res, env.tenant_id, "victim@example.com", "000000", "a-new-password-1", None
            )
    finally:
        codes.verify = orig
    assert await env.scalar("SELECT count(*) FROM user_totp") == 0
