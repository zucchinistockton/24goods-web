"""Buyer-credential vault for ``upstream_direct`` (review-v12 §18; AGENTS.md §1.1 level of care).

A buyer's account session (a ChatGPT session text is about 37 KB) is a live credential for the
buyer's account. It is sealed at checkout with a **procurement PUBLIC key** that is a DIFFERENT
keypair from the card vault's: the API process can seal but never open it, the delivery worker
(card-vault private keys) cannot open it either, and only the procurement process holds the private
half, which it uses in memory at the moment of the supplier call.

The OPEN side lives in :mod:`app.procurement.credopen`, which only the procurement process
imports (an API-side test asserts it). Same primitive as :mod:`app.core.cardvault` (X25519 + HKDF + AES-256-GCM, kid for rotation); the
associated data binds a blob to ``(tenant, "buyer-credential", order)`` so it can neither be moved
to another order nor be mistaken for a card secret. Nothing here logs, caches or stores plaintext.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

from app.core.cardvault import CardContext, CardSealer, VaultError, kid_of

# the "product" slot of the AAD: a credential blob can never validate as a card secret
DOMAIN = "buyer-credential"
MAX_CREDENTIAL_BYTES = 64 * 1024

__all__ = [
    "DOMAIN",
    "MAX_CREDENTIAL_BYTES",
    "CredentialSealer",
    "VaultError",
    "kid_of",
]


def _ctx(tenant_id: str, order_id: str) -> CardContext:
    return CardContext(str(tenant_id), DOMAIN, str(order_id))


class CredentialSealer:
    """Encrypt-only (the API). There is no ``open`` on this class."""

    def __init__(self, public_keys: dict[str, X25519PublicKey], active_kid: str) -> None:
        # CardSealer wants a fingerprint key; credentials are never fingerprinted, so it is a
        # random per-process value that nothing ever uses.
        self._inner = CardSealer(public_keys, active_kid, os.urandom(32))

    @property
    def active_kid(self) -> str:
        return self._inner.active_kid

    def seal(self, secret: str, *, tenant_id: str, order_id: str) -> bytes:
        if not secret or len(secret.encode("utf-8", "replace")) > MAX_CREDENTIAL_BYTES:
            raise ValueError("credential size out of range")
        return self._inner.seal(secret, _ctx(tenant_id, order_id))
