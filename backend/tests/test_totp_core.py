"""RFC 6238 / RFC 4226 conformance and the storage helpers of ``app.core.totp`` (no services)."""

from __future__ import annotations

import base64
import uuid

from app.core import totp

# RFC 6238 appendix B uses the ASCII secret "12345678901234567890" (SHA-1) with 8 digits; the
# 6-digit code is the last six digits of each vector.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()
RFC_VECTORS = {
    59: "94287082",
    1111111109: "07081804",
    1111111111: "14050471",
    1234567890: "89005924",
    2000000000: "69279037",
    20000000000: "65353130",
}
KEY = b"k" * 32


def test_rfc6238_vectors() -> None:
    for at, eight in RFC_VECTORS.items():
        assert totp.code_at(RFC_SECRET, totp.step_at(at)) == eight[-6:]


def test_rfc4226_counter_vectors() -> None:
    expected = ["755224", "287082", "359152", "969429", "338314", "254676"]
    assert [totp.code_at(RFC_SECRET, n) for n in range(6)] == expected


def test_verify_window_and_single_use() -> None:
    now = 1_700_000_000.0
    step = totp.step_at(now)
    for delta in (-1, 0, 1):
        assert (
            totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, step + delta), now=now) == step + delta
        )
    for delta in (-2, 2):
        assert totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, step + delta), now=now) is None
    code = totp.code_at(RFC_SECRET, step)
    # a used step is rejected, and so is any earlier one still inside the window
    assert totp.verify(RFC_SECRET, code, last_step=step, now=now) is None
    assert (
        totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, step - 1), last_step=step, now=now) is None
    )
    assert (
        totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, step + 1), last_step=step, now=now)
        == step + 1
    )


def test_verify_input_handling() -> None:
    now = 1_700_000_000.0
    code = totp.code_at(RFC_SECRET, totp.step_at(now))
    assert totp.verify(RFC_SECRET, f" {code[:3]} {code[3:]} ", now=now) is not None
    for bad in ("", "12345", "1234567", "abcdef", "１２３４５６", code + "0", "-" * 6):
        assert totp.verify(RFC_SECRET, bad, now=now) is None


def test_new_secret_and_uri() -> None:
    a, b = totp.new_secret(), totp.new_secret()
    assert a != b and len(a) == 32 and a.isalnum() and a.upper() == a
    assert len(totp.code_at(a, 1)) == 6
    uri = totp.otpauth_uri(a, "a b@example.com", "Kesiv & Co")
    assert uri.startswith("otpauth://totp/Kesiv%20%26%20Co%3Aa%20b%40example.com?")
    assert f"secret={a}" in uri and "issuer=Kesiv%20%26%20Co" in uri
    assert "algorithm=SHA1&digits=6&period=30" in uri


def test_secret_is_bound_to_tenant_and_user() -> None:
    tenant, user = uuid.uuid4(), uuid.uuid4()
    secret = totp.new_secret()
    blob = totp.seal_secret(KEY, tenant, user, secret)
    assert secret.encode() not in blob
    assert totp.open_secret(KEY, tenant, user, blob) == secret
    assert totp.open_secret(KEY, tenant, uuid.uuid4(), blob) is None
    assert totp.open_secret(KEY, uuid.uuid4(), user, blob) is None
    assert totp.open_secret(b"x" * 32, tenant, user, blob) is None
    assert totp.open_secret(KEY, tenant, user, blob[:-1] + bytes([blob[-1] ^ 1])) is None
    assert totp.open_secret(KEY, tenant, user, b"short") is None
    assert totp.seal_secret(KEY, tenant, user, secret) != blob  # fresh nonce every time


def test_recovery_codes() -> None:
    user = uuid.uuid4()
    codes = totp.new_recovery_codes()
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(len(c) == 11 and c[5] == "-" for c in codes)
    h = totp.recovery_hash(KEY, user, codes[0])
    assert h is not None and codes[0].replace("-", "") not in h
    # typed variants hash the same; another user, key or code does not
    assert totp.recovery_hash(KEY, user, codes[0].upper().replace("-", " ")) == h
    assert totp.recovery_hash(KEY, uuid.uuid4(), codes[0]) != h
    assert totp.recovery_hash(b"x" * 32, user, codes[0]) != h
    assert totp.recovery_hash(KEY, user, codes[1]) != h
    for bad in ("", "abc", "0000000000", "abcde-abcd", "abcde-abcdef"):
        assert totp.recovery_hash(KEY, user, bad) is None
