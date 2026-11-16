"""The OPEN side of the buyer-credential vault (``upstream_direct``): procurement ONLY.

Nothing outside ``app/procurement`` (and the tests) may import this module: the API seals with the
public key and must never be able to open a buyer's session (``tests/test_vault_security.py``
asserts that no API code references an opener or imports this module).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from app.core.cardvault import CardContext, CardOpener
from app.core.credvault import DOMAIN
from app.procurement.keys import SealerConfigError


class CredentialOpener:
    """Decrypt (procurement only)."""

    def __init__(self, private_keys: dict[str, X25519PrivateKey]) -> None:
        self._inner = CardOpener(private_keys)

    def open(self, blob: bytes, *, tenant_id: str, order_id: str) -> str:
        return self._inner.open(blob, CardContext(str(tenant_id), DOMAIN, str(order_id)))


def load_credential_opener(path: str) -> CredentialOpener | None:
    """The buyer-credential PRIVATE keyring, or ``None`` when no file is configured (direct jobs
    then go to ``needs_manual``). {kid: base64 raw X25519 private key}; never logged."""
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        keys = {
            str(kid): X25519PrivateKey.from_private_bytes(base64.b64decode(b64, validate=True))
            for kid, b64 in data.items()
        }
    except OSError as exc:
        raise SealerConfigError(f"cannot read session keys ({type(exc).__name__})") from None
    except (ValueError, AttributeError, TypeError):
        raise SealerConfigError("session keys must be JSON {kid: base64 raw X25519}") from None
    if not keys:
        raise SealerConfigError("no session private key configured")
    return CredentialOpener(keys)
