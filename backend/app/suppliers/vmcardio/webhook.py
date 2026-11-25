"""vmcardio webhook verification and normalisation (ADR-055; vendor pages 6664458m0 + 9364102m0).

Pure functions: the API process imports this with only the vendor's PUBLIC key, never the API
secret. ``X-VM-SIGN`` = Base64 of an RSA-SHA256 (PKCS#1 v1.5) signature over the RAW body.

A webhook is only a hint: nothing here changes money or order state. The payload is reduced to a
whitelist (no card number, no CVV), amounts become decimal strings, and the free text (merchant,
description) is length-capped with every run of four or more digits masked, so a card number or a
3-D Secure code that hides in a description is never kept.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.suppliers.base import WebhookRejected, dec
from app.suppliers.direct_progress import clean_code

MAX_BODY = 64 * 1024
SIGNATURE_HEADER = "x-vm-sign"
KIND = "vmcardio_card_transaction"
_DIGITS = re.compile(r"\d{4,}")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_AMOUNT_KEYS = ("amount", "auth_amount", "settle_amount", "fee")


def load_public_key(pem: str) -> rsa.RSAPublicKey | None:
    """The configured PEM (real newlines, or the two characters ``\\n`` as an env file needs).
    ``None`` = no usable key configured."""
    text = (pem or "").strip().strip('"').replace("\\n", "\n")
    if not text:
        return None
    try:
        key = serialization.load_pem_public_key(text.encode())
    except (ValueError, TypeError):
        return None
    return key if isinstance(key, rsa.RSAPublicKey) else None


def verify_signature(raw: bytes, headers: Mapping[str, str], key: rsa.RSAPublicKey) -> bool:
    given = (headers.get(SIGNATURE_HEADER) or headers.get("X-VM-SIGN") or "").strip()
    if not given or len(raw) > MAX_BODY:
        return False
    try:
        sig = base64.b64decode(given, validate=True)
        key.verify(sig, raw, padding.PKCS1v15(), hashes.SHA256())
    except (binascii.Error, ValueError, InvalidSignature):
        return False
    return True


def _text(value: Any, cap: int) -> str | None:
    if not isinstance(value, str):
        return None
    s = _DIGITS.sub("****", _CTRL.sub(" ", value)).strip()[:cap]
    return s or None


def normalise(data: Mapping[str, Any], *, verified: bool) -> tuple[str, dict[str, Any]]:
    """``(dedup key, payload)`` of one card-transaction webhook. Raises ``WebhookRejected``."""
    if not isinstance(data, Mapping):
        raise WebhookRejected("malformed")
    out: dict[str, Any] = {"verified": verified}
    for key in ("auth_id", "vm_card_id", "status", "type", "currency", "settle_currency"):
        v = clean_code(data.get(key), 64)
        if v:
            out[key] = v
    for key in _AMOUNT_KEYS:
        if key in data:
            try:
                out[key] = str(dec(data[key]))
            except ValueError:
                continue
    for key, cap in (("merchant_name", 64), ("description", 120)):
        v = _text(data.get(key), cap)
        if v:
            out[key] = v
    auth = out.get("auth_id")
    if not auth:
        raise WebhookRejected("no auth_id")
    parts = (auth, out.get("type", "-"), out.get("status", "-"), out.get("vm_card_id", "-"))
    dedup = ":".join(("vmcardio", *parts))
    if len(dedup) > 200:
        dedup = "vmcardio:" + hashlib.sha256(dedup.encode()).hexdigest()
    return dedup, out
