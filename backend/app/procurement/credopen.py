"""The OPEN side of the buyer-credential vault (``upstream_direct``): procurement ONLY.

Nothing outside ``app/procurement`` (and the tests) may import this module: the API seals with the
public key and must never be able to open a buyer's session (``tests/test_vault_security.py``
asserts that no API code references an opener or imports this module).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from app.core.cardvault import CardContext, CardOpener
from app.core.credvault import DOMAIN
from app.procurement.keys import SealerConfigError


class CredentialOpener:
    """Decrypt (procurement only)."""

    def __init__(self, private_keys: dict[str, X25519PrivateKey]) -> None:
        self._inner = CardOpener(private_keys)

    def open(self, blob: bytes, *, tenant_id: str, order_id: str) -> str:
        return self._inner.open(blob, CardContext(str(tenant_id), DOMAIN, str(order_id)))

    def account_binding(self, tenant_id: str, identity: str) -> str:
        """Keyed binding of a buyer account identity (half-price checks, issue #27): an HMAC under
        a key derived (HKDF) from the lexicographically first PRIVATE session key, which only this
        process holds, so the database alone (or the API, which holds the shared internal token)
        can neither compute nor dictionary-check it. Domain-separated and tenant-scoped. Adding a
        later key id keeps the binding; removing the first key id invalidates open checks (they
        live 30 minutes, and a mismatch is a refusal before any spend)."""
        kid = min(self._inner._privs)
        raw = self._inner._privs[kid].private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=b"24goods/half-account/v1"
        ).derive(raw)
        msg = str(tenant_id).encode() + b"\x1f" + identity.encode()
        return hmac.new(key, msg, hashlib.sha256).hexdigest()


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
