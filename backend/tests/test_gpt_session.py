"""Offline pre-check of a pasted ChatGPT session (no services needed)."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.suppliers.gpt_session import MAX_SESSION_BYTES, GptSessionError, parse_gpt_session

NOW = datetime(2026, 11, 15, tzinfo=timezone.utc)


def _b64(obj: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _jwt(exp: datetime, account: str = "acc-1") -> str:
    claims = {
        "exp": int(exp.timestamp()),
        "https://api.openai.com/auth": {"chatgpt_account_id": account, "chatgpt_plan_type": "plus"},
    }
    return f"{_b64({'alg': 'RS256'})}.{_b64(claims)}.c2ln"


def _session(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "accessToken": _jwt(NOW + timedelta(days=9)),
        "sessionToken": "st-" + "x" * 40,
        "expires": "2027-02-13T02:41:11.117Z",
        "account": {"id": "acc-1", "planType": "plus"},
        "user": {"id": "user-1", "email": "buyer@example.com"},
    }
    base.update(over)
    return base


def _code(raw: str) -> str:
    with pytest.raises(GptSessionError) as err:
        parse_gpt_session(raw, now=NOW)
    return err.value.code


def test_valid_session_with_surrounding_prose() -> None:
    raw = "Paste the whole session:\n\n" + json.dumps(_session(), indent=2) + "\n\n"
    s = parse_gpt_session(raw, now=NOW)
    assert (s.account_id, s.user_id, s.plan_type) == ("acc-1", "user-1", "plus")
    assert s.email_masked == "b***@example.com"
    assert s.access_expires_at == NOW + timedelta(days=9)
    assert s.session_expires_at is not None and s.session_expires_at.year == 2027
    assert json.loads(s.canonical)["sessionToken"].startswith("st-")


def test_secret_never_appears_in_repr_or_error() -> None:
    s = parse_gpt_session(json.dumps(_session()), now=NOW)
    assert "st-xxx" not in repr(s) and "buyer@" not in repr(s)
    bad = json.dumps(_session(accessToken="SECRETTOKEN"))
    with pytest.raises(GptSessionError) as err:
        parse_gpt_session(bad, now=NOW)
    assert "SECRETTOKEN" not in str(err.value)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (_jwt(NOW + timedelta(days=1)), "access_token_only"),
        ("hello", "not_json"),
        ('{"accessToken": "a.b.c", "sessionTok', "not_json"),
        ("[1, 2]", "not_json"),
        (json.dumps(_session(sessionToken="")), "missing_session_token"),
        (json.dumps(_session(accessToken=None)), "missing_access_token"),
        (json.dumps(_session(accessToken="a.b")), "malformed_access_token"),
        (json.dumps(_session(accessToken="a.!!!.c")), "malformed_access_token"),
        (json.dumps(_session(accessToken=f"a.{_b64({'exp': True})}.c")), "malformed_access_token"),
        (json.dumps(_session(accessToken=f"a.{_b64({'exp': 1e30})}.c")), "malformed_access_token"),
        (json.dumps(_session(expires="2026-01-01T00:00:00Z")), "expired"),
        (json.dumps(_session(account={"planType": "plus"})), "missing_account"),
        (json.dumps(_session(user="x")), "missing_account"),
        (json.dumps(_session(account={"id": "other"})), "account_mismatch"),
        ("x" * (MAX_SESSION_BYTES + 1), "too_large"),
    ],
)
def test_rejections(raw: str, code: str) -> None:
    assert _code(raw) == code


def test_an_expired_access_token_with_a_live_session_is_accepted() -> None:
    """Owner 2026-11-25: the supplier renews the access token from the session token, so only
    the session's own ``expires`` decides; the summary still shows when the access token ran out."""
    s = parse_gpt_session(json.dumps(_session(accessToken=_jwt(NOW - timedelta(days=2)))), now=NOW)
    assert s.access_expires_at < NOW and s.session_expires_at is not None
