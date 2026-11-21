"""The vendor's preflight token became a ~9.8 KB compact JWE (production, 2026-11-20).

A 512-character cap turned every direct-order account check into ``malformed_preflight`` ->
503 ``preflight_unavailable`` for the buyer.
"""

from __future__ import annotations

import secrets

import pytest

from app.devtools.mock_zovocard import _jwe_like_token
from app.procurement import redeem
from app.suppliers.zovocard import direct as dr


def _jwe(ct: int = 7300) -> str:
    return ".".join(secrets.token_urlsafe(n) for n in (60, 192, 12, ct, 12))


def test_direct_preflight_reads_a_long_jwe_token():
    tok = _jwe()
    assert 9000 < len(tok) < dr.PREFLIGHT_TOKEN_MAX
    pf = dr.parse_preflight({"preflight_token": tok, "currentPlan": "free"}, lambda _v: None)
    assert pf is not None and pf.token == tok


@pytest.mark.parametrize("bad", ["short", 123, "x" * (dr.PREFLIGHT_TOKEN_MAX + 1)])
def test_direct_preflight_still_rejects_a_bad_token(bad):
    assert dr.parse_preflight({"preflight_token": bad}, lambda _v: None) is None


def test_redeem_passes_a_long_preflight_token_both_ways():
    tok = _jwe()
    assert redeem.shape_preflight({"preflight_token": tok})["preflight_token"] == tok
    with pytest.raises(redeem.RedeemError):
        redeem.shape_preflight({"preflight_token": tok + "<"})
    # the redemption token itself keeps its own, shorter limit
    assert not redeem.TOKEN_RE.fullmatch(tok)


def test_the_mock_issues_tokens_shaped_like_the_vendor():
    tok = _jwe_like_token()
    assert tok.count(".") == 4 and len(tok) > 9000


def test_an_expired_session_is_the_buyers_to_fix_not_an_outage():
    # vendor 2026-11-20: 400 GPT_SESSION_EXPIRED "Session expired ... not charged"
    from app.suppliers.base import Outcome
    from app.suppliers.zovocard.errors import classify

    assert "GPT_SESSION_EXPIRED" in dr.SESSION_REJECTED_CODES
    mapped = redeem.map_upstream_error("preflight", 400, "GPT_SESSION_EXPIRED")
    assert mapped == "credential_invalid"
    for write in (False, True):  # unknown 400 on a write would be "maybe charged"
        assert classify(400, "GPT_SESSION_EXPIRED", write=write).outcome is Outcome.FATAL
