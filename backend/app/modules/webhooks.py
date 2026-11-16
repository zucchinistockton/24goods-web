"""Supplier webhooks (review-v12 §6). ``POST /api/v1/webhooks/zovocard``: no session, no CSRF.

Raw body (<= 64 KiB) -> HMAC-SHA256 over the raw bytes, constant-time compare against the
production and the sandbox signing secret (the match also tells which supplier account sent
it) -> masked whitelist payload -> ``INSERT ... ON CONFLICT DO NOTHING`` into supplier_events ->
``supplier.event`` outbox row -> 200 fast. A webhook never fulfils anything by itself: the
procurement engine re-queries the supplier first. The API holds only the webhook signing
secrets, never a supplier API secret. Bad signature: 401 with no detail.
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.deps import client_ip, get_db, get_res
from app.core.ratelimit import hit
from app.core.resources import Resources
from app.models import Outbox, SupplierAccount, SupplierWebhookEvent, Tenant
from app.modules.orders import card_otp
from app.suppliers.base import WebhookRejected
from app.suppliers.zovocard.webhook import MAX_BODY, normalise, verify_signature

log = logging.getLogger("app.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
ZOVOCARD_PATH = "/api/v1/webhooks/zovocard"
PER_IP_PER_MIN = 300


@router.post("/zovocard", response_model=None)
async def zovocard_webhook(
    request: Request, db: AsyncSession = Depends(get_db), res: Resources = Depends(get_res)
) -> Response:
    allowed, _ = await hit(
        res.redis, "webhook:zovocard", client_ip(request) or "-", PER_IP_PER_MIN, 60
    )
    if not allowed:
        return Response(status_code=429)
    declared = request.headers.get("content-length", "0")
    if not declared.isdigit() or int(declared) > MAX_BODY:
        return Response(status_code=413)
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return Response(status_code=413)
    s = res.settings
    env = None
    secret = ""
    # one setting may hold several comma-separated keys: the vendor signs the general callback and
    # each API-key subscription with different secrets. Every candidate is compared in constant
    # time; the FIRST key of the environment stays the dedup key material, whichever one signed.
    for name, value in (
        ("production", s.zovocard_webhook_secret),
        ("sandbox", s.zovocard_sandbox_webhook_secret),
    ):
        secrets = [x.strip() for x in value.split(",") if x.strip()]
        if any([verify_signature(raw, request.headers, sec) for sec in secrets]):
            env, secret = name, secrets[0]
            break
    if env is None:
        log.warning("zovocard webhook: bad signature")
        return Response(status_code=401)
    try:
        data = json.loads(raw)
        items = data if isinstance(data, list) else [data]
        if not items or len(items) > 50 or not all(isinstance(i, dict) for i in items):
            raise WebhookRejected("malformed")
        events = [normalise(i, secret) for i in items]
    except (ValueError, UnicodeDecodeError, WebhookRejected):
        return Response(status_code=400)
    tenant = await db.scalar(
        select(Tenant.id).where(Tenant.id == request.state.tenant_id, Tenant.kind == "platform")
    )
    supplier_id = (
        await db.scalar(
            select(SupplierAccount.id).where(
                SupplierAccount.tenant_id == tenant,
                SupplierAccount.code == "zovocard",
                SupplierAccount.env == env,
            )
        )
        if tenant is not None
        else None
    )
    if tenant is None or supplier_id is None:
        # signed but no matching account here: acknowledge (stop retries), keep nothing
        log.warning("zovocard webhook: no %s supplier account on this tenant", env)
        return Response(status_code=200)
    new = 0
    otps = {}  # event index -> (card id, code), read from the raw items only
    for i, item in enumerate(items):
        found = card_otp.extract_otp(item)
        if found is not None:
            otps[i] = found
    for idx, ev in enumerate(events):
        eid = uuid.uuid4()
        inserted = await db.scalar(
            pg_insert(SupplierWebhookEvent)
            .values(
                id=eid,
                tenant_id=tenant,
                supplier_id=supplier_id,
                dedup_key=ev.dedup_key,
                kind=ev.kind,
                payload=dict(ev.payload),
            )
            .on_conflict_do_nothing(index_elements=["supplier_id", "dedup_key"])
            .returning(SupplierWebhookEvent.id)
        )
        if inserted is None:
            continue  # duplicate delivery (at-least-once): already queued once
        new += 1
        if idx in otps:  # a duplicate delivery never re-sends a code (dedup above)
            card_id, otp = otps.pop(idx)
            await card_otp.forward(
                db, res.redis, tenant_id=tenant, supplier_id=supplier_id, card_id=card_id, otp=otp
            )
            del otp
        db.add(
            Outbox(
                tenant_id=tenant,
                topic="supplier.event",
                key=f"supplier_event:{eid}",
                payload={"event_id": str(eid), "supplier_id": str(supplier_id)},
            )
        )
    if new:
        await audit(
            db,
            tenant_id=tenant,
            action="supplier.webhook_received",
            target=f"supplier:{supplier_id}",
            detail={"events": new, "kinds": sorted({e.kind for e in events})[:10]},
        )
    await db.commit()
    return Response(status_code=200)
