"""Recent purchases strip on the storefront (owner request 2026-11-18): REAL orders delivered in the
last hour, newest first. Shown per buyer: the product (and option) bought, when, and a masked
buyer name (first character + ``***``). Never an order number, an amount, an account, a contact
or anything a buyer typed. Only active products of the platform shop (a reseller storefront shows
nothing: its sales are not the platform's to show, and vice versa). Cached briefly per tenant and
language; an empty list hides the section.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import pick
from app.models import Order, Product, User

log = logging.getLogger("app.catalog.recent")

WINDOW = timedelta(hours=1)
LIMIT = 10
CACHE_S = 30


def mask_buyer(display_name: str | None) -> str:
    name = (display_name or "").strip()
    first = name[:1] if name[:1].isalnum() else ""
    return f"{first or '*'}***"


async def recent_purchases(
    db: AsyncSession, redis: Any, tenant_id: uuid.UUID, lang: str
) -> list[dict[str, Any]]:
    key = f"cache:recent-purchases:{tenant_id}:{lang}"
    if redis is not None:
        try:
            cached = await redis.get(key)
            if cached:
                return list(json.loads(cached))
        except Exception as exc:  # the cache is optional
            log.info("recent purchases cache unreadable error=%s", type(exc).__name__)
    since = datetime.now(timezone.utc) - WINDOW
    rows = (
        await db.execute(
            select(
                Order.product_id,
                Order.variant_name,
                Order.delivered_at,
                Product.title_i18n,
                User.display_name,
            )
            .join(Product, Product.id == Order.product_id)
            .join(User, User.id == Order.buyer_id)
            .where(
                Order.tenant_id == tenant_id,
                Order.kind == "goods",
                Order.status.in_(("delivered", "completed")),
                Order.delivered_at >= since,
                Order.reseller_id.is_(None),
                Product.status == "active",
            )
            .order_by(Order.delivered_at.desc())
            .limit(LIMIT)
        )
    ).all()
    items = [
        {
            "product_id": str(r.product_id),
            "title": pick(r.title_i18n, lang),
            "variant": r.variant_name,
            "at": r.delivered_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "buyer": mask_buyer(r.display_name),
        }
        for r in rows
    ]
    if redis is not None:
        try:
            await redis.set(key, json.dumps(items), ex=CACHE_S)
        except Exception as exc:
            log.info("recent purchases cache not written error=%s", type(exc).__name__)
    return items
