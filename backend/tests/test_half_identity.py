"""PR #38 round 3: the half-price account binding uses only the access token's claims, and the
production role assertion runs on every entry point (serve.py passes its own resources)."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from app.procurement.halfcheck import identity_of
from tests.test_procurement_direct import _b64


def _doc(claims: dict[str, object] | None, **fields: object) -> str:
    token = f"eyJhbGciOiJub25lIn0.{_b64({'exp': int(time.time()) + 864000, **(claims or {})})}.sig"
    return json.dumps({"accessToken": token, **fields})


AUTH = "https://api.openai.com/auth"
PROFILE = "https://api.openai.com/profile"


def test_identity_comes_from_the_token_claims_not_the_editable_json_fields() -> None:
    forged = _doc(
        {AUTH: {"chatgpt_account_id": "acct-real"}},
        account={"id": "acct-victim"},
        user={"email": "victim@example.com"},
    )
    assert identity_of("gpt", forged) == "acct:acct-real"


def test_identity_falls_back_to_user_id_then_profile_email() -> None:
    assert identity_of("gpt", _doc({AUTH: {"user_id": "user-1"}})) == "user:user-1"
    mail = _doc({PROFILE: {"email": " Me@Example.COM "}})
    assert identity_of("gpt", mail) == "mail:me@example.com"


@pytest.mark.parametrize(
    "credential",
    [
        _doc(None, account={"id": "acct-1"}, user={"email": "a@example.com"}),  # no claims
        json.dumps({"accessToken": "not-a-jwt", "account": {"id": "acct-1"}}),
        json.dumps({"account": {"id": "acct-1"}}),
        "garbage",
    ],
)
def test_no_identity_without_usable_token_claims(credential: str) -> None:
    assert identity_of("gpt", credential) is None


def test_claude_and_grok_have_no_identity() -> None:
    cred = _doc({AUTH: {"chatgpt_account_id": "acct-1"}})
    assert identity_of("claude", cred) is None and identity_of("grok", cred) is None


async def test_prod_role_assertion_runs_when_resources_are_passed_in(monkeypatch) -> None:
    import app.main as main
    from app.core.config import get_settings

    seen: list[object] = []

    async def refuse(r: object) -> None:
        seen.append(r)
        raise RuntimeError("the API must connect as app_api, not owner")

    monkeypatch.setattr(main, "_assert_api_role", refuse)
    settings = get_settings().model_copy(update={"env": "prod"})
    res = SimpleNamespace(settings=settings)  # serve.py's path: resources owned by the caller
    app = main.create_app(res, background=False)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="app_api"):
        async with app.router.lifespan_context(app):
            pass
    assert seen == [res]
