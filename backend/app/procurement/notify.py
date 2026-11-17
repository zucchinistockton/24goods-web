"""Procurement -> API progress notification (issue #30, ADR-043).

The ONLY thing the procurement process publishes to Redis: ``{"tenant_id", "order_id"}`` on one
fixed channel (its ACL grants ``&rt:procprogress`` and nothing else). No buyer id, no step data:
the API resolves the buyer and recomputes the progress itself. Kept free of API-stack imports
(``tests/test_suppliers_v12.py``)."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

log = logging.getLogger("app.procurement.notify")

PROC_PROGRESS_CHANNEL = "rt:procprogress"


async def notify_progress_changed(redis: Any, tenant_id: uuid.UUID, order_id: uuid.UUID) -> bool:
    """True only when Redis accepted the message, so the caller can retry on its next pass;
    never raises."""
    msg = json.dumps({"tenant_id": str(tenant_id), "order_id": str(order_id)})
    try:
        await redis.publish(PROC_PROGRESS_CHANNEL, msg)
    except Exception as exc:
        log.warning("progress notify failed error=%s", type(exc).__name__)
        return False
    return True
