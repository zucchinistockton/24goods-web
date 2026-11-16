"""Time-based one-time passwords (RFC 6238 over RFC 4226) for optional two-factor login.

Standard-library only. Parameters are the ones every authenticator app supports: HMAC-SHA1,
6 digits, 30-second steps, and a window of +/- 1 step for clock drift.

Secrets and codes are credentials: nothing here logs, and the caller stores the secret sealed
(``seal_secret``: AES-256-GCM under the settings key, bound to tenant and user) and recovery codes
hashed (``recovery_hash``: keyed HMAC, so a database leak does not reveal usable codes). The card
vault is not involved.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
import uuid
from urllib.parse import quote

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DIGITS = 6
STEP_S = 30
WINDOW = 1  # steps accepted before and after the current one
SECRET_BYTES = 20  # 160 bits, the RFC 4226 recommendation
RECOVERY_CODES = 10
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i
_NONCE_LEN = 12


def new_secret() -> str:
    """A fresh base32 secret (no padding), as authenticator apps expect it."""
    return base64.b32encode(os.urandom(SECRET_BYTES)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def step_at(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_S)


def code_at(secret: str, step: int) -> str:
    """The code for one time step (RFC 4226 dynamic truncation)."""
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**DIGITS).zfill(DIGITS)


def clean_code(code: str) -> str | None:
    """Digits of a typed code (spaces and a dash are tolerated), or None when it cannot match."""
    c = code.strip().replace(" ", "").replace("-", "")
    return c if len(c) == DIGITS and c.isascii() and c.isdigit() else None


def verify(
    secret: str, code: str, *, last_step: int | None = None, now: float | None = None
) -> int | None:
    """The matching time step, or None.

    Every candidate step is compared in constant time and all of them are always checked. A step
    at or before ``last_step`` never matches: a code is accepted once, also inside the window.
    """
    c = clean_code(code)
    if c is None:
        return None
    current = step_at(now)
    matched: int | None = None
    for step in range(current - WINDOW, current + WINDOW + 1):
        ok = hmac.compare_digest(code_at(secret, step), c)
        if ok and (last_step is None or step > last_step) and matched is None:
            matched = step
    return matched


def otpauth_uri(secret: str, account: str, issuer: str) -> str:
    """``otpauth://`` URI for the QR code (rendered by the client, never by a third party)."""
    label = quote(f"{issuer}:{account}", safe="")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP_S}"
    )


# ------------------------------------------------------------------ secret at rest
def _aad(tenant_id: uuid.UUID, user_id: uuid.UUID) -> bytes:
    return f"totp|{tenant_id}|{user_id}".encode()


def seal_secret(key: bytes, tenant_id: uuid.UUID, user_id: uuid.UUID, secret: str) -> bytes:
    nonce = os.urandom(_NONCE_LEN)
    return nonce + AESGCM(key).encrypt(nonce, secret.encode(), _aad(tenant_id, user_id))


def open_secret(key: bytes, tenant_id: uuid.UUID, user_id: uuid.UUID, blob: bytes) -> str | None:
    """The secret, or None when the blob does not authenticate (wrong key, tenant or user)."""
    if len(blob) <= _NONCE_LEN:
        return None
    try:
        plain = AESGCM(key).decrypt(blob[:_NONCE_LEN], blob[_NONCE_LEN:], _aad(tenant_id, user_id))
        return plain.decode()
    except (InvalidTag, UnicodeDecodeError):
        return None


# ------------------------------------------------------------------ recovery codes
def new_recovery_codes(n: int = RECOVERY_CODES) -> list[str]:
    """``xxxxx-xxxxx`` codes with about 49 bits each; shown once, stored only as hashes."""
    out = []
    for _ in range(n):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        out.append(f"{raw[:5]}-{raw[5:]}")
    return out


def clean_recovery(code: str) -> str | None:
    c = code.strip().lower().replace(" ", "").replace("-", "")
    return c if len(c) == 10 and all(ch in _RECOVERY_ALPHABET for ch in c) else None


def recovery_hash(key: bytes, user_id: uuid.UUID, code: str) -> str | None:
    """Keyed hash of a recovery code, bound to its user (hex), or None for malformed input."""
    c = clean_recovery(code)
    if c is None:
        return None
    return hmac.new(key, f"totp-recovery|{user_id}|{c}".encode(), hashlib.sha256).hexdigest()
