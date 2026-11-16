"""Optional two-factor login (TOTP, review-v12 §13).

Enrolment: ``begin_setup`` stores a sealed, unconfirmed secret and shows it once; ``enable``
verifies one code, activates the row and returns ten recovery codes (stored as keyed hashes).
Login: a correct password for an enrolled account yields a short-lived pending token instead of
a session; ``complete_login`` turns token + code into the user. A time step is accepted once
(``last_step`` moves forward in one conditional UPDATE), a recovery code is removed in the same
statement that accepts it, and failures count against the login limiter of the password step.

Nothing here logs or audits a secret, a code or a recovery code.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import suspend, totp
from app.core.audit import audit
from app.core.config import Settings
from app.core.errors import ApiError, RateLimited
from app.core.ratelimit import (
    blocked,
    enforce,
    hit,
    ident_hash,
    release,
    reserve,
)
from app.core.security import verify_password
from app.models import User, UserTotp

PENDING_TTL_S = 300
PENDING_ATTEMPTS = 5  # wrong codes per pending token before it is destroyed
USER_FAILURES = (10, 3600)  # wrong second factors per account, whatever the IP
MINT_LIMIT = (10, 600)  # pending tokens per account
VERIFY_LIMIT = (10, 600)  # code checks per signed-in user (enable / disable / regenerate)
# the password step's limits (auth/service.py); a wrong second factor counts against the same keys
IP_FAILURES = (5, 60)
PAIR_FAILURES = (10, 3600)
EMAIL_FAILURES = (50, 3600)


class TwoFactorInvalid(ApiError):
    """The code is not valid"""

    status, code = 401, "two_factor_invalid"


class TwoFactorExpired(ApiError):
    """Sign in again"""

    status, code = 401, "two_factor_expired"


class TwoFactorEnabled(ApiError):
    """Two-factor authentication is already on"""

    status, code = 409, "two_factor_enabled"


class TwoFactorNotEnabled(ApiError):
    """Two-factor authentication is not on"""

    status, code = 409, "two_factor_not_enabled"


class TwoFactorPassword(ApiError):
    """Password is incorrect"""

    status, code = 403, "invalid_password"


@dataclass(frozen=True)
class LimiterKeys:
    """The login limiter identities of the password step that led to the second step."""

    email_key: str
    pair_key: str


def limiter_keys(tenant_id: uuid.UUID, ident: str, ip: str | None) -> LimiterKeys:
    ident = ident.strip().lower()
    return LimiterKeys(
        email_key=ident_hash(f"{tenant_id}:{ident}"),
        pair_key=ident_hash(f"{tenant_id}:{ident}:{ip or '-'}"),
    )


# ------------------------------------------------------------------ rows
async def _row(db: AsyncSession, user: User, *, lock: bool = False) -> UserTotp | None:
    q = select(UserTotp).where(UserTotp.user_id == user.id, UserTotp.tenant_id == user.tenant_id)
    return await db.scalar(q.with_for_update() if lock else q)


async def enabled(db: AsyncSession, user: User) -> bool:
    return (
        await db.scalar(
            select(UserTotp.user_id).where(
                UserTotp.user_id == user.id,
                UserTotp.tenant_id == user.tenant_id,
                UserTotp.confirmed_at.is_not(None),
            )
        )
    ) is not None


async def status(db: AsyncSession, user: User) -> dict[str, Any]:
    row = await _row(db, user)
    on = row is not None and row.confirmed_at is not None
    return {
        "enabled": on,
        "enabled_at": row.confirmed_at.isoformat() if on and row and row.confirmed_at else None,
        "recovery_codes_left": len(row.recovery_hashes) if on and row else 0,
    }


async def _consume(
    db: AsyncSession, settings: Settings, user: User, row: UserTotp, code: str, *, recovery: bool
) -> str | None:
    """Accept ``code`` once. Returns ``"totp"`` / ``"recovery"``, or None. Caller commits."""
    if totp.clean_code(code) is not None:
        secret = totp.open_secret(
            settings.totp_secret(), user.tenant_id, user.id, row.secret_sealed
        )
        if secret is None:
            return None
        step = totp.verify(secret, code, last_step=row.last_step)
        if step is None:
            return None
        # one conditional UPDATE: two requests with the same code cannot both pass
        done = await db.execute(
            update(UserTotp)
            .where(
                UserTotp.user_id == user.id,
                UserTotp.tenant_id == user.tenant_id,
                UserTotp.last_step.is_(None) | (UserTotp.last_step < step),
            )
            .values(last_step=step)
        )
        return "totp" if done.rowcount == 1 else None
    if not recovery:
        return None
    digest = totp.recovery_hash(settings.totp_secret(), user.id, code)
    if digest is None:
        return None
    done = await db.execute(
        update(UserTotp)
        .where(
            UserTotp.user_id == user.id,
            UserTotp.tenant_id == user.tenant_id,
            UserTotp.confirmed_at.is_not(None),
            UserTotp.recovery_hashes.any(digest),
        )
        .values(recovery_hashes=func.array_remove(UserTotp.recovery_hashes, digest))
    )
    return "recovery" if done.rowcount == 1 else None


# ------------------------------------------------------------------ signed-in user
async def begin_setup(
    db: AsyncSession,
    redis: Redis,
    settings: Settings,
    user: User,
    password: str | None,
    issuer: str,
    ip: str | None,
) -> dict[str, str]:
    """Start (or restart) enrolment. The secret is returned here and never again."""
    await enforce(redis, "2fa:setup", str(user.id), 10, 3600)
    # accounts without a password the owner knows (one-tap, Google) are proven by the session
    if not user.is_instant and user.google_sub is None:
        ok, _ = await verify_password(user.pw_hash, password or "")
        if not ok:
            raise TwoFactorPassword("Password is incorrect")
    row = await _row(db, user, lock=True)
    if row is not None and row.confirmed_at is not None:
        raise TwoFactorEnabled("Two-factor authentication is already on")
    secret = totp.new_secret()
    sealed = totp.seal_secret(settings.totp_secret(), user.tenant_id, user.id, secret)
    await db.execute(
        pg_insert(UserTotp)
        .values(user_id=user.id, tenant_id=user.tenant_id, secret_sealed=sealed)
        .on_conflict_do_update(
            index_elements=[UserTotp.user_id],
            set_={"secret_sealed": sealed, "last_step": None, "recovery_hashes": []},
            where=UserTotp.confirmed_at.is_(None),
        )
    )
    await audit(
        db,
        tenant_id=user.tenant_id,
        action="auth.2fa_setup_started",
        actor_id=user.id,
        actor_role=user.role,
        target=f"user:{user.id}",
        ip=ip,
    )
    await db.commit()
    account = user.handle or user.email
    return {"secret": secret, "otpauth_uri": totp.otpauth_uri(secret, account, issuer)}


def _new_recovery(settings: Settings, user: User) -> tuple[list[str], list[str]]:
    codes = totp.new_recovery_codes()
    key = settings.totp_secret()
    return codes, [h for c in codes if (h := totp.recovery_hash(key, user.id, c)) is not None]


async def _checked(
    db: AsyncSession,
    redis: Redis,
    settings: Settings,
    user: User,
    code: str,
    *,
    confirmed: bool,
    recovery: bool,
    ip: str | None,
) -> UserTotp:
    """The locked row after ``code`` was accepted for a signed-in user, or an error."""
    await enforce(redis, "2fa:verify", str(user.id), *VERIFY_LIMIT)
    row = await _row(db, user, lock=True)
    if confirmed and (row is None or row.confirmed_at is None):
        raise TwoFactorNotEnabled("Two-factor authentication is not on")
    if row is None:
        raise TwoFactorNotEnabled("Start the setup first")
    if not confirmed and row.confirmed_at is not None:
        raise TwoFactorEnabled("Two-factor authentication is already on")
    if await _consume(db, settings, user, row, code, recovery=recovery) is None:
        await audit(
            db,
            tenant_id=user.tenant_id,
            action="auth.2fa_failed",
            actor_id=user.id,
            actor_role=user.role,
            target=f"user:{user.id}",
            ip=ip,
            detail={"stage": "settings"},
            severity="warning",
        )
        await db.commit()
        raise TwoFactorInvalid("The code is not valid")
    return row


async def enable(
    db: AsyncSession, redis: Redis, settings: Settings, user: User, code: str, ip: str | None
) -> list[str]:
    """Confirm enrolment with one code. Returns the recovery codes (shown once)."""
    await _checked(db, redis, settings, user, code, confirmed=False, recovery=False, ip=ip)
    codes, hashes = _new_recovery(settings, user)
    await db.execute(
        update(UserTotp)
        .where(UserTotp.user_id == user.id, UserTotp.tenant_id == user.tenant_id)
        .values(confirmed_at=datetime.now(timezone.utc), recovery_hashes=hashes)
    )
    await audit(
        db,
        tenant_id=user.tenant_id,
        action="auth.2fa_enabled",
        actor_id=user.id,
        actor_role=user.role,
        target=f"user:{user.id}",
        before={"enabled": False},
        after={"enabled": True},
        ip=ip,
        severity="warning",
    )
    await db.commit()
    return codes


async def disable(
    db: AsyncSession, redis: Redis, settings: Settings, user: User, code: str, ip: str | None
) -> None:
    """Turn 2FA off with a current code or a recovery code."""
    await _checked(db, redis, settings, user, code, confirmed=True, recovery=True, ip=ip)
    await db.execute(
        delete(UserTotp).where(UserTotp.user_id == user.id, UserTotp.tenant_id == user.tenant_id)
    )
    await audit(
        db,
        tenant_id=user.tenant_id,
        action="auth.2fa_disabled",
        actor_id=user.id,
        actor_role=user.role,
        target=f"user:{user.id}",
        before={"enabled": True},
        after={"enabled": False},
        ip=ip,
        severity="warning",
    )
    await db.commit()


async def regenerate(
    db: AsyncSession, redis: Redis, settings: Settings, user: User, code: str, ip: str | None
) -> list[str]:
    """Replace all recovery codes. Needs a current authenticator code (not a recovery code)."""
    await _checked(db, redis, settings, user, code, confirmed=True, recovery=False, ip=ip)
    codes, hashes = _new_recovery(settings, user)
    await db.execute(
        update(UserTotp)
        .where(UserTotp.user_id == user.id, UserTotp.tenant_id == user.tenant_id)
        .values(recovery_hashes=hashes)
    )
    await audit(
        db,
        tenant_id=user.tenant_id,
        action="auth.2fa_recovery_regenerated",
        actor_id=user.id,
        actor_role=user.role,
        target=f"user:{user.id}",
        ip=ip,
        severity="warning",
    )
    await db.commit()
    return codes


async def reset_by_staff(db: AsyncSession, staff: User, user: User, ip: str | None) -> bool:
    """An admin removes a user's 2FA (lost device). Returns whether anything was removed."""
    done = await db.execute(
        delete(UserTotp).where(UserTotp.user_id == user.id, UserTotp.tenant_id == staff.tenant_id)
    )
    await audit(
        db,
        tenant_id=staff.tenant_id,
        action="user.2fa_reset",
        actor_id=staff.id,
        actor_role=staff.role,
        target=f"user:{user.id}",
        before={"enabled": done.rowcount == 1},
        after={"enabled": False},
        ip=ip,
        severity="critical",
    )
    await db.commit()
    return done.rowcount == 1


# ------------------------------------------------------------------ login, second step
def _pending_key(token: str) -> str:
    return "2fa:pending:" + hashlib.sha256(token.encode()).hexdigest()


async def start_pending(redis: Redis, user: User, keys: LimiterKeys) -> str:
    """A single-use token that stands for "the password was right" for five minutes."""
    # a known password must not buy an unlimited supply of tokens (each carries five guesses)
    await enforce(redis, "2fa:mint", str(user.id), *MINT_LIMIT)
    token = secrets.token_urlsafe(32)
    data = {
        "uid": str(user.id),
        "tid": str(user.tenant_id),
        "email_key": keys.email_key,
        "pair_key": keys.pair_key,
    }
    await redis.set(_pending_key(token), json.dumps(data), ex=PENDING_TTL_S)
    return token


async def complete_login(
    db: AsyncSession,
    redis: Redis,
    settings: Settings,
    tenant_id: uuid.UUID,
    token: str,
    code: str,
    ip: str | None,
) -> tuple[User, str]:
    """Pending token + code -> (user, ``"totp"`` | ``"recovery"``). Raises 401 / 429 otherwise."""
    ip_key = ip or "-"
    await blocked(redis, "loginfail:ip", ip_key, *IP_FAILURES)
    key = _pending_key(token)
    raw = await redis.get(key)
    if not raw:
        raise TwoFactorExpired("Sign in again")
    data = json.loads(raw)
    if data.get("tid") != str(tenant_id):
        raise TwoFactorExpired("Sign in again")
    uid = uuid.UUID(data["uid"])
    await blocked(redis, "loginfail:pair", data["pair_key"], *PAIR_FAILURES)
    await blocked(redis, "loginfail:email", data["email_key"], *EMAIL_FAILURES)
    tries = await redis.incr(key + ":n")
    await redis.expire(key + ":n", PENDING_TTL_S)
    if int(tries) > PENDING_ATTEMPTS:
        await redis.delete(key, key + ":n")
        raise TwoFactorExpired("Sign in again")
    user = await db.scalar(select(User).where(User.id == uid, User.tenant_id == tenant_id))
    if user is None or user.status != "active":
        await redis.delete(key, key + ":n")
        raise TwoFactorExpired("Sign in again")
    await suspend.enforce(db, user)
    # Count the guess BEFORE checking it (given back on success): parallel requests cannot all
    # pass a read-only check and overshoot the per-account budget.
    fits, reset = await reserve(redis, "2fa:fail:user", str(uid), 1, *USER_FAILURES)
    if not fits:
        raise RateLimited(retry_after=reset)
    row = await _row(db, user, lock=True)
    kind = None
    if row is not None and row.confirmed_at is not None:
        kind = await _consume(db, settings, user, row, code, recovery=True)
    if kind is None:
        for name, ident, (limit, window) in (
            ("loginfail:ip", ip_key, IP_FAILURES),
            ("loginfail:pair", data["pair_key"], PAIR_FAILURES),
            ("loginfail:email", data["email_key"], EMAIL_FAILURES),
        ):
            await hit(redis, name, ident, limit, window)
        await audit(
            db,
            tenant_id=tenant_id,
            action="auth.2fa_failed",
            actor_id=uid,
            target=f"user:{uid}",
            ip=ip,
            detail={"stage": "login"},
            severity="warning",
        )
        await db.commit()
        raise TwoFactorInvalid("The code is not valid")
    # single use: the token dies before the session exists
    await redis.delete(key, key + ":n")
    await release(redis, "2fa:fail:user", str(uid), 1, USER_FAILURES[1])
    left = None
    if kind == "recovery":
        left = await db.scalar(
            select(func.cardinality(UserTotp.recovery_hashes)).where(
                UserTotp.user_id == uid, UserTotp.tenant_id == tenant_id
            )
        )
    user.last_login_at = datetime.now(timezone.utc)
    await audit(
        db,
        tenant_id=tenant_id,
        action="auth.login",
        actor_id=user.id,
        actor_role=user.role,
        target=f"user:{user.id}",
        ip=ip,
        detail={"second_factor": kind}
        | ({"recovery_codes_left": int(left or 0)} if kind == "recovery" else {}),
        severity="warning" if kind == "recovery" else "info",
    )
    await db.commit()
    return user, kind
