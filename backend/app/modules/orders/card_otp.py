"""3DS / one-time codes for a virtual card: DISABLED (owner decision 2026-11-17).

The vendor still sends ``card_3ds_otp`` webhooks. A code can authorise a payment, and buyers
misused cards, so no code ever reaches a buyer: :func:`forward` drops every code before any
lookup. Nothing is read from the database, stored in Redis, published, audited or logged.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.suppliers.zovocard.webhook import first_otp

OTP_TTL_S = 300
_OTP_RE = re.compile(r"^[0-9A-Za-z]{3,12}$")


def extract_otp(item: Mapping[str, Any]) -> tuple[str, str] | None:
    """``(card_id, otp)`` of a ``card_3ds_otp`` item, or ``None`` (malformed: dropped silently)."""
    if item.get("event") != "card_3ds_otp":
        return None
    card, otp = item.get("card_id"), first_otp(item)
    if isinstance(card, bool) or not isinstance(card, (int, str)) or str(card) == "":
        return None
    if not isinstance(otp, str) or not _OTP_RE.fullmatch(otp.strip()):
        return None
    return str(card)[:128], otp.strip()


async def forward(
    db: AsyncSession,
    redis: Redis,
    *,
    tenant_id: uuid.UUID,
    supplier_id: uuid.UUID,
    card_id: str,
    otp: str,
) -> bool:
    """Always ``False``: the code is dropped (never kept, never shown to anyone)."""
    return False
