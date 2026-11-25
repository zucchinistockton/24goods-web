"""Local pre-check of a buyer-pasted ChatGPT session (``chatgpt.com/api/auth/session`` JSON).

Pure and offline: no network call, no logging. It rejects input that cannot work (a bare access
token, truncated JSON, an expired session) before anything is sealed or sent to a supplier, and
returns a non-secret summary for the UI. An expired access token is accepted (the supplier
renews it from the session token). The access-token payload is decoded **without** verifying
its signature, so every field here is a hint: the supplier's preflight stays the truth
(review-v12 §5).

The raw session is a credential. ``GptSession.canonical`` is the only field that carries it; it is
excluded from ``repr`` and must only ever be sealed or forwarded, never stored or logged in
plaintext.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

MAX_SESSION_BYTES = 64 * 1024
_AUTH_CLAIM = "https://api.openai.com/auth"


class GptSessionError(ValueError):
    """``code`` is a stable machine code (maps to an i18n key); it never contains input text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GptSession:
    account_id: str
    user_id: str
    email_masked: str
    plan_type: str
    access_expires_at: datetime
    session_expires_at: datetime | None
    canonical: str = field(repr=False)


def _mask_email(email: str) -> str:
    local, sep, domain = email.partition("@")
    if not sep:
        return "***"
    return f"{local[:1]}***@{domain}"


def _jwt_payload(token: str) -> dict[str, object]:
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise GptSessionError("malformed_access_token")
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        payload = json.loads(raw)
    except (binascii.Error, ValueError) as exc:
        raise GptSessionError("malformed_access_token") from exc
    if not isinstance(payload, dict):
        raise GptSessionError("malformed_access_token")
    return payload


def jwt_claims(token: str) -> dict[str, object]:
    """The UNVERIFIED claims of an access token (raises ``GptSessionError`` if malformed). Only
    for values the vendor's own use of the token vouches for; never logged."""
    return _jwt_payload(token)


def _str(obj: object, key: str) -> str:
    value = obj.get(key) if isinstance(obj, dict) else None
    return value if isinstance(value, str) else ""


def parse_gpt_session(raw: str, *, now: datetime | None = None) -> GptSession:
    """Parse pasted text (surrounding prose is tolerated) into a checked ``GptSession``."""
    now = now or datetime.now(timezone.utc)
    if len(raw.encode("utf-8", "replace")) > MAX_SESSION_BYTES:
        raise GptSessionError("too_large")
    text = raw.strip()
    start = text.find("{")
    if start < 0:
        # the most common mistake: only the "eyJ..." access token was pasted
        raise GptSessionError("access_token_only" if text.startswith("eyJ") else "not_json")
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError as exc:
        raise GptSessionError("not_json") from exc
    if not isinstance(data, dict):
        raise GptSessionError("not_json")

    if not _str(data, "sessionToken"):
        raise GptSessionError("missing_session_token")
    access = _str(data, "accessToken")
    if not access:
        raise GptSessionError("missing_access_token")
    claims = _jwt_payload(access)

    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int | float):
        raise GptSessionError("malformed_access_token")
    try:
        access_expires = datetime.fromtimestamp(exp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise GptSessionError("malformed_access_token") from exc
    # An expired accessToken alone is no reason to refuse (owner 2026-11-25): the supplier's
    # preflight and order renew it from the sessionToken (ZovoCard docs 6.18b). Only a session
    # whose own ``expires`` has passed is dead.

    session_expires: datetime | None = None
    expires_raw = _str(data, "expires")
    if expires_raw:
        try:
            session_expires = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GptSessionError("not_json") from exc
        if session_expires.tzinfo is None:
            session_expires = session_expires.replace(tzinfo=timezone.utc)
        if session_expires <= now:
            raise GptSessionError("expired")

    account, user = data.get("account"), data.get("user")
    account_id, user_id = _str(account, "id"), _str(user, "id")
    if not account_id or not user_id:
        raise GptSessionError("missing_account")
    auth = claims.get(_AUTH_CLAIM)
    claimed = _str(auth, "chatgpt_account_id")
    if claimed and claimed != account_id:
        raise GptSessionError("account_mismatch")

    return GptSession(
        account_id=account_id,
        user_id=user_id,
        email_masked=_mask_email(_str(user, "email")),
        plan_type=_str(account, "planType") or _str(auth, "chatgpt_plan_type") or "unknown",
        access_expires_at=access_expires,
        session_expires_at=session_expires,
        canonical=json.dumps(data, ensure_ascii=False, separators=(",", ":")),
    )
