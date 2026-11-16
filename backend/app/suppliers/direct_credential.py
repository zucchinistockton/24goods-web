"""Offline validation of the account session a buyer supplies for ``upstream_direct``.

Pure: no network, no logging, no I/O. It runs at checkout (before anything is sealed) and again in
the adapter just before a send. ``DirectCredential.canonical`` is the credential itself: it is
excluded from ``repr`` and may only be sealed or forwarded to the supplier, never logged, audited
or stored in plaintext. Error codes are stable machine strings (they map to i18n keys) and never
contain input text.

Products (the vendor's closed list, review-v12 §18): ``gpt`` (the ``chatgpt.com/api/auth/session``
document, checked by :mod:`app.suppliers.gpt_session`), ``claude`` (an ``sk-ant-`` session key) and
``grok`` (the ``sso`` cookie: ``eyJ...`` or text containing ``sso=``). The JWT / key contents are
never verified here: the supplier's preflight is the truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.suppliers.gpt_session import MAX_SESSION_BYTES, GptSessionError, parse_gpt_session

# ENUM LOCK (vendor pitfall 1: an unknown ``product`` is silently turned into ``gpt`` and CHARGED).
# Only these literals are ever sent, always explicitly, validated on this side.
DIRECT_PRODUCTS: tuple[str, ...] = ("gpt", "claude", "grok")
MIN_CREDENTIAL_CHARS = 8

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CLAUDE = re.compile(r"^sk-ant-[A-Za-z0-9_\-=.%]{16,4096}$")
_GROK_JWT = re.compile(r"^eyJ[A-Za-z0-9_\-=.]{20,8192}$")
_GROK_COOKIE = re.compile(r"^[\x20-\x7e]{12,8192}$")


class CredentialError(ValueError):
    """``code`` is a stable machine code; it never contains input text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DirectCredential:
    product: str
    email_masked: str | None  # only the ChatGPT session names the account; never the full e-mail
    canonical: str = field(repr=False)


def validate_credential(product: str, raw: object) -> DirectCredential:
    """Check ``raw`` for ``product``; raises :class:`CredentialError`. Size, charset, shape only."""
    if product not in DIRECT_PRODUCTS:
        raise CredentialError("unsupported_product")
    if not isinstance(raw, str):
        raise CredentialError("invalid_format")
    if len(raw.encode("utf-8", "replace")) > MAX_SESSION_BYTES:
        raise CredentialError("too_large")
    text = raw.strip()
    if len(text) < MIN_CREDENTIAL_CHARS:
        raise CredentialError("too_short")
    if _CONTROL.search(text):
        raise CredentialError("invalid_format")
    if product == "gpt":
        try:
            parsed = parse_gpt_session(text)
        except GptSessionError as exc:
            raise CredentialError(exc.code) from None
        return DirectCredential("gpt", parsed.email_masked, parsed.canonical)
    if product == "claude":
        if not _CLAUDE.fullmatch(text):
            raise CredentialError("invalid_format")
        return DirectCredential("claude", None, text)
    if not (_GROK_JWT.fullmatch(text) or ("sso=" in text and _GROK_COOKIE.fullmatch(text))):
        raise CredentialError("invalid_format")
    return DirectCredential("grok", None, text)


def mask_email(value: object) -> str | None:
    """``u***@example.com`` from whatever the supplier reports (already masked is kept short)."""
    if not isinstance(value, str) or "@" not in value:
        return None
    local, _, domain = value.strip().partition("@")
    if not local or not domain or len(domain) > 120:
        return None
    return f"{local[:1]}***@{domain}"[:64]


__all__ = [
    "DIRECT_PRODUCTS",
    "CredentialError",
    "DirectCredential",
    "mask_email",
    "validate_credential",
]
