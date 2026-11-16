"""3DS / one-time codes for a virtual card (review-v12 §6, §17): "3DS over SSE".

The vendor forwards a ``card_3ds_otp`` webhook the moment a bank asks for a code. The code can
authorise a payment, so it is treated like the card secret itself:

* it is read from the RAW verified webhook body here and nowhere else (``supplier_events`` keeps
  only the masked whitelist payload, see ``suppliers/zovocard/webhook.py``);
* it goes ONLY to the buyer who owns the card (webhook card -> ``vcc`` job -> order -> buyer), and
  only while the order is delivered;
* it lives in Redis for at most ``OTP_TTL_S`` seconds (a late-opened page can still fetch it) and is
  published once on that buyer's realtime channel (pub/sub, nothing persisted);
* it never reaches PostgreSQL, a log line, an audit row, an exception message or an outbox payload.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Mapping
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.models import Order, ProcurementJob
from app.modules.common.views import REVEALABLE
from app.modules.realtime.publish import publish, user_channel

log = logging.getLogger("app.card_otp")

OTP_TTL_S = 300
_OTP_RE = re.compile(r"^[0-9A-Za-z]{3,12}$")
_PREFIX = "otp3ds:"


def otp_key(order_id: uuid.UUID | str, unit_no: int) -> str:
    return f"{_PREFIX}{order_id}:{unit_no}"


def extract_otp(item: Mapping[str, Any]) -> tuple[str, str] | None:
    """``(card_id, otp)`` of a ``card_3ds_otp`` item, or ``None`` (malformed: dropped silently)."""
    if item.get("event") != "card_3ds_otp":
        return None
    card, otp = item.get("card_id"), item.get("otp")
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
    """Hand a code to the card's owner. ``False`` when no delivered order owns the card (the code
    is then simply dropped: it is never kept for later or shown to anyone else)."""
    row = (
        await db.execute(
            select(
                Order.id,
                Order.buyer_id,
                Order.status,
                ProcurementJob.unit_no,
                ProcurementJob.card_mask,
            )
            .join(ProcurementJob, ProcurementJob.order_id == Order.id)
            .where(
                ProcurementJob.tenant_id == tenant_id,
                ProcurementJob.supplier_id == supplier_id,
                ProcurementJob.kind == "vcc",
                ProcurementJob.upstream_ref == card_id,
                ProcurementJob.state == "fulfilled",
                Order.tenant_id == tenant_id,
            )
        )
    ).first()
    if row is None or row.status not in REVEALABLE or row.buyer_id is None:
        return False
    body = {"otp": otp, "card": row.card_mask, "unit": row.unit_no}
    try:
        await redis.set(otp_key(row.id, row.unit_no), json.dumps(body), ex=OTP_TTL_S)
    except RedisError as exc:
        log.warning("card otp not stored error=%s", type(exc).__name__)
        return False
    await publish(
        redis,
        user_channel(row.buyer_id),
        "card_otp",
        {"order_id": str(row.id), "expires_in": OTP_TTL_S, **body},
    )
    await audit(  # who got a code, never the code
        db,
        tenant_id=tenant_id,
        action="card.otp_forwarded",
        target=f"order:{row.id}",
        detail={"unit": row.unit_no},
    )
    return True


async def pending_for(redis: Redis, order_id: uuid.UUID) -> list[dict[str, Any]]:
    """Live codes of an order (owner already checked by the caller): ``[{card, unit, otp,
    expires_in}]``. Empty when none arrived or they expired."""
    out: list[dict[str, Any]] = []
    try:
        async for key in redis.scan_iter(match=f"{_PREFIX}{order_id}:*", count=50):
            raw = await redis.get(key)
            ttl = await redis.ttl(key)
            if raw is None or ttl is None or ttl <= 0:
                continue
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            out.append({**item, "expires_in": int(ttl)})
    except RedisError as exc:
        log.warning("card otp read failed error=%s", type(exc).__name__)
        return []
    return sorted(out, key=lambda i: i.get("unit", 0))
