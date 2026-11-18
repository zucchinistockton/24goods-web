"""Publish realtime events to Redis pub/sub. Failures never break the request."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.procurement.notify import PROC_PROGRESS_CHANNEL

log = logging.getLogger("app.realtime")

PREFIX = "rt:"
__all__ = ["PREFIX", "PROC_PROGRESS_CHANNEL"]  # (the channel constant is re-exported)


def user_channel(user_id: uuid.UUID | str) -> str:
    return f"{PREFIX}user:{user_id}"


def staff_channel(tenant_id: uuid.UUID | str) -> str:
    return f"{PREFIX}staff:{tenant_id}"


def public_channel(tenant_id: uuid.UUID | str) -> str:
    return f"{PREFIX}public:{tenant_id}"


async def publish(redis: Redis, channel: str, event: str, data: Any) -> None:
    try:
        await redis.publish(channel, json.dumps({"event": event, "data": data}, default=str))
    except RedisError as exc:
        # the channel name carries a user id: log the event type only
        log.warning("realtime publish failed event=%s error=%s", event, type(exc).__name__)


async def publish_message(
    redis: Redis, tenant_id: uuid.UUID, buyer_id: uuid.UUID, message: dict[str, Any]
) -> None:
    data = {"conversation_id": message["conversation_id"], "message": message}
    await publish(redis, user_channel(buyer_id), "message", data)
    await publish(redis, staff_channel(tenant_id), "message", data)


async def publish_order(
    redis: Redis,
    tenant_id: uuid.UUID,
    buyer_id: uuid.UUID,
    buyer_view: dict[str, Any],
    staff_view: dict[str, Any],
) -> None:
    await publish(redis, user_channel(buyer_id), "order", buyer_view)
    await publish(redis, staff_channel(tenant_id), "order", staff_view)


async def publish_order_progress(redis: Redis, buyer_id: uuid.UUID, data: dict[str, Any]) -> None:
    """API side: a direct order's buyer-facing progress step changed (issue #30). ``data`` is the
    coarse progress view only (``order_id`` + step / percent / outcome / reason), never vendor
    detail; the buyer's page refetches ``GET /orders/{ref}/direct`` on it."""
    await publish(redis, user_channel(buyer_id), "order_progress", data)


async def publish_staff_progress(redis: Redis, tenant_id: uuid.UUID, data: dict[str, Any]) -> None:
    """The same coarse step on the tenant's staff channel, so the console follows a direct order
    live (owner request 2026-11-18); the console refetches the job detail it is allowed to see."""
    await publish(redis, staff_channel(tenant_id), "order_progress", data)


async def publish_after_sales(
    redis: Redis,
    tenant_id: uuid.UUID,
    buyer_id: uuid.UUID,
    buyer_view: dict[str, Any],
    staff_view: dict[str, Any],
) -> None:
    await publish(redis, user_channel(buyer_id), "after_sales", buyer_view)
    await publish(redis, staff_channel(tenant_id), "after_sales", staff_view)


async def publish_conversation(
    redis: Redis,
    tenant_id: uuid.UUID,
    buyer_id: uuid.UUID,
    buyer_view: dict[str, Any] | None,
    staff_view: dict[str, Any] | None,
) -> None:
    if buyer_view is not None:
        await publish(redis, user_channel(buyer_id), "conversation", buyer_view)
    if staff_view is not None:
        await publish(redis, staff_channel(tenant_id), "conversation", staff_view)


async def publish_agent_status(
    redis: Redis, tenant_id: uuid.UUID, best: dict[str, Any], agent: dict[str, Any]
) -> None:
    """Buyers get the aggregated best status (same as GET /support/status); staff get the
    individual agent's status (``agent_id`` added)."""
    await publish(redis, public_channel(tenant_id), "agent_status", best)
    await publish(redis, staff_channel(tenant_id), "agent_status", agent)


async def publish_typing(
    redis: Redis,
    tenant_id: uuid.UUID,
    buyer_id: uuid.UUID,
    *,
    to_staff: bool,
    data: dict[str, Any],
) -> None:
    """Ephemeral typing hint (never stored): buyer -> staff channel, staff -> the buyer."""
    channel = staff_channel(tenant_id) if to_staff else user_channel(buyer_id)
    await publish(redis, channel, "typing", data)
