"""Build the seal-only vault for the API from PUBLIC key material.

Deliberately has no code path that reads private keys: the API process can add
stock but can never open it (docs/architecture.md §6, ADR-002).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

from app.core.cardvault import CardSealer
from app.core.config import Settings
from app.core.credvault import CredentialSealer


class VaultConfigError(RuntimeError):
    pass


def _load_public_map(settings: Settings) -> dict[str, str]:
    if settings.vault_public_keys.strip():
        raw = settings.vault_public_keys
    elif settings.vault_public_keys_file:
        raw = Path(settings.vault_public_keys_file).read_text(encoding="utf-8")
    else:
        raise VaultConfigError("APP_VAULT_PUBLIC_KEYS or APP_VAULT_PUBLIC_KEYS_FILE is required")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VaultConfigError("vault public keys must be JSON {kid: base64}") from exc
    if not isinstance(data, dict) or not data:
        raise VaultConfigError("vault public keys must be a non-empty JSON object")
    return {str(k): str(v) for k, v in data.items()}


def build_sealer(settings: Settings) -> CardSealer:
    pubs: dict[str, X25519PublicKey] = {}
    for kid, b64 in _load_public_map(settings).items():
        try:
            raw = base64.b64decode(b64, validate=True)
            pubs[kid] = X25519PublicKey.from_public_bytes(raw)
        except ValueError as exc:
            raise VaultConfigError(f"invalid public key for kid {kid!r}") from exc
    active = settings.vault_active_kid or (next(iter(pubs)) if len(pubs) == 1 else "")
    if not active:
        raise VaultConfigError("APP_VAULT_ACTIVE_KID is required when several keys are configured")
    try:
        fp_key = settings.fingerprint_key()
    except ValueError as exc:
        raise VaultConfigError("APP_VAULT_FINGERPRINT_KEY: " + str(exc)) from exc
    return CardSealer(pubs, active, fp_key)


def build_credential_sealer(settings: Settings) -> CredentialSealer | None:
    """The seal-only keyring for buyer credentials, or ``None`` when none is configured: then
    ``upstream_direct`` products cannot be bought (checkout answers 503 ``direct_unavailable``)."""
    if settings.cred_public_keys.strip():
        raw = settings.cred_public_keys
    elif settings.cred_public_keys_file:
        try:
            raw = Path(settings.cred_public_keys_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise VaultConfigError("cannot read APP_CRED_PUBLIC_KEYS_FILE") from exc
    else:
        return None
    try:
        data = json.loads(raw)
        pubs = {
            str(kid): X25519PublicKey.from_public_bytes(base64.b64decode(b64, validate=True))
            for kid, b64 in data.items()
        }
    except (ValueError, AttributeError, TypeError) as exc:
        raise VaultConfigError("credential public keys must be JSON {kid: base64}") from exc
    if not pubs:
        raise VaultConfigError("no credential public key configured")
    active = settings.cred_active_kid or (next(iter(pubs)) if len(pubs) == 1 else "")
    if active not in pubs:
        raise VaultConfigError("APP_CRED_ACTIVE_KID is missing or unknown")
    return CredentialSealer(pubs, active)
