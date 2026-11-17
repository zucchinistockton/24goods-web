"""Internal procurement API (backend network only, never routed by Caddy).

``POST /internal/probe {"tenant_id", "supplier_id"}`` with ``X-Internal-Token`` (constant-time
compare against ``APP_PROCUREMENT_TOKEN``, distinct from the delivery token). The API process
calls it for the admin "Test connection" button, so the API never holds supplier secrets
(same shape as the delivery reveal call, docs/architecture.md §5).
"""

from __future__ import annotations

import hmac
import json
import logging
import uuid

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.errors import error_body
from app.core.ratelimit import hit
from app.procurement import engine, service
from app.procurement.context import ProcurementContext

log = logging.getLogger("app.procurement.internal")

MAX_BODY = 4096
NO_STORE = {"Cache-Control": "no-store"}


def _err(status: int, code: str, message: str, headers: dict[str, str] | None = None):
    return JSONResponse(error_body(code, message), status_code=status, headers=headers)


def create_procurement_app(ctx: ProcurementContext) -> Starlette:
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    def _token_ok(request: Request) -> bool:
        expected = ctx.settings.procurement_token
        given = request.headers.get("x-internal-token", "")
        return len(expected) >= 32 and hmac.compare_digest(given.encode(), expected.encode())

    async def check_offer(request: Request) -> JSONResponse:
        if not _token_ok(request):
            log.warning("check-offer: bad internal token")
            return _err(401, "unauthorized", "Bad internal token")
        body = await request.body()
        if len(body) > MAX_BODY:
            return _err(422, "validation", "body too large")
        try:
            data = json.loads(body)
            tenant_id = uuid.UUID(str(data["tenant_id"]))
            offer_id = uuid.UUID(str(data["offer_id"]))
        except (ValueError, KeyError, TypeError):
            return _err(422, "validation", "tenant_id and offer_id (UUID) are required")
        allowed, reset = await hit(
            ctx.redis, "proc:check", str(offer_id), ctx.settings.probes_per_supplier_10min, 600
        )
        if not allowed:
            return _err(429, "rate_limited", "Too many checks", {"Retry-After": str(reset)})
        try:
            result = await service.check_offer(ctx, tenant_id, offer_id)
        except service.SupplierNotFound:
            return _err(404, "not_found", "Offer not found")
        return JSONResponse(result, headers=NO_STORE)

    async def probe(request: Request) -> JSONResponse:
        if not _token_ok(request):
            log.warning("probe: bad internal token")
            return _err(401, "unauthorized", "Bad internal token")
        body = await request.body()
        if len(body) > MAX_BODY:
            return _err(422, "validation", "body too large")
        try:
            data = json.loads(body)
            tenant_id = uuid.UUID(str(data["tenant_id"]))
            supplier_id = uuid.UUID(str(data["supplier_id"]))
        except (ValueError, KeyError, TypeError):
            return _err(422, "validation", "tenant_id and supplier_id (UUID) are required")
        allowed, reset = await hit(
            ctx.redis,
            "proc:probe",
            str(supplier_id),
            ctx.settings.probes_per_supplier_10min,
            600,
        )
        if not allowed:
            return _err(429, "rate_limited", "Too many probes", {"Retry-After": str(reset)})
        try:
            report = await service.probe(ctx, tenant_id, supplier_id)
        except service.SupplierNotFound:
            return _err(404, "not_found", "Supplier not found")
        return JSONResponse({"report": report.as_dict()}, headers=NO_STORE)

    async def regions(request: Request) -> JSONResponse:
        """``POST /internal/regions {"tenant_id", "product"}``: the vendor's live regions."""
        if not _token_ok(request):
            return _err(401, "unauthorized", "Bad internal token")
        body = await request.body()
        if len(body) > MAX_BODY:
            return _err(422, "validation", "body too large")
        try:
            data = json.loads(body)
            tenant_id = uuid.UUID(str(data["tenant_id"]))
            product = str(data.get("product") or "gpt")
        except (ValueError, KeyError, TypeError, AttributeError):
            return _err(422, "validation", "tenant_id (UUID) is required")
        if product not in ("gpt", "claude", "grok"):
            return _err(422, "validation", "unknown product")
        allowed, reset = await hit(ctx.redis, "proc:regions", str(tenant_id), 30, 600)
        if not allowed:
            return _err(429, "rate_limited", "Too many reads", {"Retry-After": str(reset)})
        try:
            out = await service.payment_regions(ctx, tenant_id, product)
        except service.SupplierNotFound:
            return _err(404, "not_found", "No enabled ZovoCard supplier")
        except Exception as exc:  # the vendor failing is "unavailable", never a 500 with detail
            log.warning("regions: vendor read failed error=%s", type(exc).__name__)
            return _err(502, "supplier_unavailable", "The supplier could not be read")
        return JSONResponse({"regions": out}, headers=NO_STORE)

    async def direct_order(request: Request) -> JSONResponse:
        """Read-only state of a direct job's supplier order (the console's hand-fulfil guard)."""
        if not _token_ok(request):
            return _err(401, "unauthorized", "Bad internal token")
        body = await request.body()
        if len(body) > MAX_BODY:
            return _err(422, "validation", "body too large")
        try:
            data = json.loads(body)
            tenant_id = uuid.UUID(str(data["tenant_id"]))
            job_id = uuid.UUID(str(data["job_id"]))
        except (ValueError, KeyError, TypeError):
            return _err(422, "validation", "tenant_id and job_id (UUID) are required")
        allowed, reset = await hit(ctx.redis, "proc:direct-order", str(job_id), 20, 600)
        if not allowed:
            return _err(429, "rate_limited", "Too many reads", {"Retry-After": str(reset)})
        try:
            out = await engine.direct_order_state(ctx, tenant_id, job_id)
        except LookupError:
            return _err(404, "not_found", "Job not found")
        return JSONResponse(out, headers=NO_STORE)

    return Starlette(
        routes=[
            Route("/internal/direct-order", direct_order, methods=["POST"]),
            Route("/internal/regions", regions, methods=["POST"]),
            Route("/healthz", healthz),
            Route("/internal/probe", probe, methods=["POST"]),
            Route("/internal/check-offer", check_offer, methods=["POST"]),
        ]
    )
