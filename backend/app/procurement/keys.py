"""Seal-only vault for procurement: PUBLIC keys + the fingerprint key, never a private key.

Same material and AAD as the API's sealer (app/core/vaultkeys.py), loaded without the pydantic
settings (procurement does not import app.core.config). Procured secrets are sealed with
AAD = (tenant, product, card_id) exactly like an admin card import, so only delivery can open them.
"""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

from app.core.cardvault import CardSealer


class SealerConfigError(RuntimeError):
    pass


def _b64(value: str, min_len: int = 32) -> bytes:
    raw = value.strip()
    try:
        key = base64.b64decode(
            (raw + "=" * (-len(raw) % 4)).replace("-", "+").replace("_", "/"), validate=True
        )
    except (binascii.Error, ValueError):
        raise SealerConfigError("APP_VAULT_FINGERPRINT_KEY must be base64") from None
    if len(key) < min_len:
        raise SealerConfigError("APP_VAULT_FINGERPRINT_KEY must decode to >= 32 bytes")
    return key


def build_sealer(
    *, public_keys_json: str = "", public_keys_file: str = "", active_kid: str, fp_key: str
) -> CardSealer:
    if public_keys_json.strip():
        raw = public_keys_json
    elif public_keys_file:
        try:
            raw = Path(public_keys_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise SealerConfigError(f"cannot read public keys ({type(exc).__name__})") from None
    else:
        raise SealerConfigError("APP_VAULT_PUBLIC_KEYS_FILE is required")
    try:
        data = json.loads(raw)
        pubs = {
            str(kid): X25519PublicKey.from_public_bytes(base64.b64decode(b64, validate=True))
            for kid, b64 in data.items()
        }
    except (ValueError, AttributeError, TypeError):
        raise SealerConfigError("public keys must be JSON {kid: base64 raw X25519}") from None
    if not pubs:
        raise SealerConfigError("no public key configured")
    kid = active_kid or (next(iter(pubs)) if len(pubs) == 1 else "")
    if kid not in pubs:
        raise SealerConfigError("APP_VAULT_ACTIVE_KID is missing or unknown")
    return CardSealer(pubs, kid, _b64(fp_key))
