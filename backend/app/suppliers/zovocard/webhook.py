"""ZovoCard webhook verification and normalisation (review-v12 §6; vendor §7).

Pure functions: the API process imports this with only the webhook signing secret, never the
API secret. ``X-Signature`` = lowercase hex HMAC-SHA256 of the RAW body (vendor §7.7), compared
in constant time. After verification the payload is reduced to a whitelist per event kind:
card numbers are masked, OTP / description / merchant text is never kept (a 3DS code can hide in
``description`` / ``merchant_name``), amounts become decimal strings.

Dedup keys (vendor §7.8): GPT/CDK ``event_id``; ``card_transaction`` event+auth_id+type+status;
``card_operation`` event+operation+operation_id+status; ``balance_change`` balance_log_id;
``card_fee`` auth_id+fee_type; ``card_3ds_otp`` matrix_id or HMAC(masked card + otp) so the OTP
itself is never a stored key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from typing import Any

from app.suppliers.base import SupplierEvent, WebhookRejected, dec
from app.suppliers.direct_progress import clean_message

MAX_BODY = 64 * 1024
SIGNATURE_HEADER = "x-signature"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_KEY_BAD = re.compile(r"[^A-Za-z0-9_.:@-]")

# ``code_prefix`` is deliberately absent: part of a code is part of a secret (AGENTS §1.1)
_CDK_FIELDS = (
    "event_id", "type", "cdk_id", "cdk_status", "plan", "order_id",
    "from_status", "to_status", "status", "stage", "occurred_at", "app_id",
    "client_request_id", "service_fee_minor", "service_fee_status", "final_amount_minor",
    "currency", "card_last_four", "funding_hold_status",
    "payment_state", "funding_state", "timeline_event_id", "category", "step", "public_code",
    "payment_fact",
)  # fmt: skip
_FIELDS: dict[str, tuple[str, ...]] = {
    "balance_change": (
        "balance_log_id", "type", "amount", "direction", "currency", "balance_before",
        "balance_after", "ref_id", "card_id", "occurred_at",
    ),
    "card_fee": ("fee_type", "auth_id", "amount", "currency", "direction", "occurred_at"),
    "card_transaction": (
        "auth_id", "card_id", "type", "status", "settle_amount", "settle_currency",
        "auth_time", "source", "channel",
    ),
    "card_operation": ("operation", "operation_id", "status", "card_id", "channel"),
    "card_3ds_otp": ("card_id", "matrix_id", "channel"),
}  # fmt: skip
_AMOUNTS = frozenset({"amount", "balance_before", "balance_after", "settle_amount", "auth_amount"})


def signature(raw: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def verify_signature(raw: bytes, headers: Mapping[str, str], secret: str) -> bool:
    if not secret or len(raw) > MAX_BODY:
        return False
    given = (headers.get(SIGNATURE_HEADER) or headers.get("X-Signature") or "").strip().lower()
    if not _HEX64.fullmatch(given):
        return False
    return hmac.compare_digest(signature(raw, secret), given)


OTP_FIELDS = ("otp", "code", "verification_code")  # one list for the dedup MAC and the forwarder


def first_otp(data: Mapping[str, Any]) -> Any:
    for k in OTP_FIELDS:
        if data.get(k):
            return data[k]
    return None


def mask_pan(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 12:
        return "****"
    return f"{digits[:6]}****{digits[-4:]}"


def _scalar(key: str, v: Any) -> Any:
    if key in _AMOUNTS:
        try:
            return str(dec(v))
        except ValueError:
            return None
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        try:
            return str(dec(v))
        except ValueError:
            return None
    if isinstance(v, str):
        return v[:128]
    return None  # nested objects are not kept


def _key(*parts: Any) -> str:
    return _KEY_BAD.sub("_", ":".join(str(p) for p in parts))[:200]


def normalise(data: Mapping[str, Any], secret: str) -> SupplierEvent:
    """One verified event -> masked whitelist payload + dedup key."""
    event = data.get("event")
    etype = data.get("type")
    if isinstance(data.get("event_id"), str) and isinstance(etype, str):
        kind = etype[:48]
        payload = {k: _scalar(k, data[k]) for k in _CDK_FIELDS if k in data}
        if "public_message" in data:  # sanitised HERE: vendor free text is never stored raw
            payload["public_message"] = clean_message(data["public_message"])
        ref = data.get("cdk_id") if kind.startswith("cdk.") else data.get("order_id")
        if kind.startswith("gpt_direct.") and data.get("cdk_id") is not None:
            ref = data.get("cdk_id")
        return SupplierEvent(
            kind=kind,
            dedup_key=_key(data["event_id"]),
            payload=payload,
            upstream_ref=str(ref) if ref is not None else None,
        )
    if not isinstance(event, str) or event not in _FIELDS:
        raise WebhookRejected("unknown event")
    payload = {k: _scalar(k, data[k]) for k in _FIELDS[event] if k in data}
    if "card_number" in data:
        payload["card_number"] = mask_pan(data.get("card_number"))
    if event == "balance_change":
        if data.get("balance_log_id") is None:
            raise WebhookRejected("balance_change without balance_log_id")
        dedup = _key(event, data["balance_log_id"])
    elif event == "card_transaction":
        dedup = _key(event, data.get("auth_id"), data.get("type"), data.get("status"))
    elif event == "card_operation":
        # delete has no operation id: the card id keeps two cards' events apart (F11)
        dedup = _key(
            event,
            data.get("operation"),
            data.get("operation_id") or f"card{data.get('card_id')}",
            data.get("status"),
        )
    elif event == "card_fee":
        dedup = _key(event, data.get("auth_id"), data.get("fee_type"))
    else:  # card_3ds_otp: the OTP never becomes part of a stored key
        if data.get("matrix_id"):
            dedup = _key(event, data["matrix_id"])
        else:
            otp = str(first_otp(data) or "")
            mac = hmac.new(
                secret.encode(), (mask_pan(data.get("card_number")) + "|" + otp).encode(), "sha256"
            ).hexdigest()[:32]
            dedup = _key(event, mac)
    return SupplierEvent(kind=event, dedup_key=dedup, payload=payload)


def parse_webhook(raw: bytes, headers: Mapping[str, str], secret: str) -> list[SupplierEvent]:
    """Verify then normalise. Raises :class:`WebhookRejected` (answer 401/400 without detail)."""
    if not verify_signature(raw, headers, secret):
        raise WebhookRejected("bad signature")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise WebhookRejected("malformed body") from None
    items = data if isinstance(data, list) else [data]
    if not items or len(items) > 50 or not all(isinstance(i, dict) for i in items):
        raise WebhookRejected("malformed body")
    return [normalise(i, secret) for i in items]


__all__ = ["MAX_BODY", "mask_pan", "normalise", "parse_webhook", "signature", "verify_signature"]
