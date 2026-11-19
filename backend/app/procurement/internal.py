"""Internal procurement API (backend network only, never routed by Caddy).

``POST /internal/probe {"tenant_id", "supplier_id"}`` with ``X-Internal-Token`` (constant-time
compare against ``APP_PROCUREMENT_TOKEN``, distinct from the delivery token). The API process
calls it for the admin "Test connection" button, so the API never holds supplier secrets
(same shape as the delivery reveal call, docs/architecture.md §5).
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import re
import uuid

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.errors import error_body
from app.core.ratelimit import hit
from app.procurement import (
    cardpool,
    engine,
    halfcheck,
    preflight,
    redeem_admin,
    service,
    support_session,
)
from app.procurement.context import ProcurementContext

log = logging.getLogger("app.procurement.internal")

MAX_BODY = 4096
MAX_PREFLIGHT_BODY = 200_000  # a sealed session (<= 64 KiB plaintext) in base64 + ids
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

    async def half_check(request: Request) -> JSONResponse:
        """``POST /internal/half-check {"tenant_id", "check_id"}``: read the buyer's eligibility
        with the sealed session (opened here only) and record the outcome."""
        if not _token_ok(request):
            return _err(401, "unauthorized", "Bad internal token")
        body = await request.body()
        if len(body) > MAX_BODY:
            return _err(422, "validation", "body too large")
        try:
            data = json.loads(body)
            tenant_id = uuid.UUID(str(data["tenant_id"]))
            check_id = uuid.UUID(str(data["check_id"]))
        except (ValueError, KeyError, TypeError):
            return _err(422, "validation", "tenant_id and check_id (UUID) are required")
        allowed, reset = await hit(ctx.redis, "proc:half-check", str(tenant_id), 120, 600)
        if not allowed:
            return _err(429, "rate_limited", "Too many checks", {"Retry-After": str(reset)})
        try:
            out = await halfcheck.run_check(ctx, tenant_id, check_id)
        except LookupError:
            return _err(404, "not_found", "Check not found")
        return JSONResponse({"status": out.status, "reason": out.reason}, headers=NO_STORE)

    async def direct_preflight(request: Request) -> JSONResponse:
        """``POST /internal/direct-preflight {"tenant_id", "product_id", "variant_id", "nonce",
        "blob"}``: the account pre-check before payment (review-v12 §24). ``blob`` is the sealed
        session (base64, AAD ``preflight:<nonce>``), opened here in memory and never stored."""
        if not _token_ok(request):
            return _err(401, "unauthorized", "Bad internal token")
        body = await request.body()
        if len(body) > MAX_PREFLIGHT_BODY:
            return _err(422, "validation", "body too large")
        try:
            data = json.loads(body)
            tenant_id = uuid.UUID(str(data["tenant_id"]))
            product_id = uuid.UUID(str(data["product_id"]))
            variant_id = uuid.UUID(str(data["variant_id"])) if data.get("variant_id") else None
            nonce = uuid.UUID(str(data["nonce"]))
            blob = base64.b64decode(str(data["blob"]), validate=True)
        except (ValueError, KeyError, TypeError, binascii.Error):
            return _err(422, "validation", "tenant_id, product_id, nonce and blob are required")
        finally:
            body = b""
        allowed, reset = await hit(ctx.redis, "proc:preflight", str(tenant_id), 300, 600)
        if not allowed:
            return _err(429, "rate_limited", "Too many checks", {"Retry-After": str(reset)})
        out = await preflight.run(ctx, tenant_id, product_id, variant_id, nonce, blob)
        blob = b""
        return JSONResponse(out.view(), headers=NO_STORE)

    async def support_read(request: Request) -> JSONResponse:
        """``POST /internal/support-session {"tenant_id", "order_id"}``: the consented session of a
        failing direct order, for support (review-v12 §26). The API checks the staff role, the
        reason and audits the read; this route opens the sealed copy for that one answer."""
        if not _token_ok(request):
            return _err(401, "unauthorized", "Bad internal token")
        body = await request.body()
        if len(body) > MAX_BODY:
            return _err(422, "validation", "body too large")
        try:
            data = json.loads(body)
            tenant_id = uuid.UUID(str(data["tenant_id"]))
            order_id = uuid.UUID(str(data["order_id"]))
        except (ValueError, KeyError, TypeError):
            return _err(422, "validation", "tenant_id and order_id (UUID) are required")
        allowed, reset = await hit(ctx.redis, "proc:support-read", str(tenant_id), 60, 3600)
        if not allowed:
            return _err(429, "rate_limited", "Too many reads", {"Retry-After": str(reset)})
        try:
            out = await support_session.reveal(ctx, tenant_id, order_id)
        except LookupError:
            return _err(404, "not_found", "No shared session for this order")
        return JSONResponse(out, headers=NO_STORE)

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

    async def charge_check(request: Request) -> JSONResponse:
        """Read-only charge evidence of a job (supplier order, top-ups, card spends): ADR-048."""
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
        allowed, reset = await hit(ctx.redis, "proc:charge-check", str(job_id), 20, 600)
        if not allowed:
            return _err(429, "rate_limited", "Too many reads", {"Retry-After": str(reset)})
        try:
            out = await engine.charge_check(ctx, tenant_id, job_id)
        except LookupError:
            return _err(404, "not_found", "Job not found")
        return JSONResponse(out, headers=NO_STORE)

    async def _card_call(request: Request, bucket: str, limit: int, write: bool = False):
        """Common shell of the card-pool routes (issue #29 part 2): token, size, UUIDs, rate
        limit. Returns ``(data, tenant_id, supplier_id)`` or the error response."""
        if not _token_ok(request):
            return _err(401, "unauthorized", "Bad internal token")
        body = await request.body()
        if len(body) > MAX_BODY:
            return _err(422, "validation", "body too large")
        try:
            data = json.loads(body)
            tenant_id = uuid.UUID(str(data["tenant_id"]))
            raw = data.get("supplier_id")
            supplier_id = uuid.UUID(str(raw)) if raw else None
            if not isinstance(data, dict):
                raise TypeError
        except (ValueError, KeyError, TypeError, AttributeError):
            return _err(422, "validation", "tenant_id (UUID) is required")
        allowed, reset = await hit(ctx.redis, bucket, str(tenant_id), limit, 600)
        if not allowed:
            return _err(429, "rate_limited", "Too many requests", {"Retry-After": str(reset)})
        return data, tenant_id, supplier_id

    def _card_error(exc: Exception) -> JSONResponse:
        if isinstance(exc, service.SupplierNotFound):
            return _err(404, "not_found", "No ZovoCard supplier")
        if isinstance(exc, cardpool.SupplierDisabled):
            return _err(409, "supplier_disabled", "The supplier is disabled")
        if isinstance(exc, cardpool.ProductionWritesDisabled):
            return _err(
                409, "production_writes_disabled", "Writes to a production supplier are disabled"
            )
        if isinstance(exc, (cardpool.NotFundingCard, ValueError)):
            return _err(422, "validation", str(exc)[:120])
        if isinstance(exc, LookupError):
            return _err(502, "supplier_unavailable", "The supplier rules could not be read")
        log.warning("cards: vendor call failed error=%s", type(exc).__name__)
        return _err(502, "supplier_unavailable", "The supplier could not be read")

    async def cards_pool(request: Request) -> JSONResponse:
        got = await _card_call(request, "proc:cards", 60)
        if isinstance(got, JSONResponse):
            return got
        data, tid, sid = got
        product = str(data.get("product") or "gpt")
        if product not in ("gpt", "claude", "grok"):
            return _err(422, "validation", "unknown product")
        try:
            return JSONResponse(await cardpool.pool(ctx, tid, sid, product), headers=NO_STORE)
        except Exception as exc:
            return _card_error(exc)

    async def cards_schedule(request: Request) -> JSONResponse:
        got = await _card_call(request, "proc:cards-schedule", 60)
        if isinstance(got, JSONResponse):
            return got
        data, tid, sid = got
        product, plan = str(data.get("product") or ""), str(data.get("plan") or "")
        if product not in ("gpt", "claude", "grok") or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,48}", plan
        ):
            return _err(422, "validation", "product and plan are required")
        try:
            return JSONResponse(
                await cardpool.schedule(ctx, tid, sid, product, plan), headers=NO_STORE
            )
        except Exception as exc:
            return _card_error(exc)

    async def cards_rules(request: Request) -> JSONResponse:
        got = await _card_call(request, "proc:cards-rules", 60)
        if isinstance(got, JSONResponse):
            return got
        _data, tid, sid = got
        try:
            return JSONResponse(await cardpool.rules(ctx, tid, sid), headers=NO_STORE)
        except Exception as exc:
            return _card_error(exc)

    async def cards_rules_set(request: Request) -> JSONResponse:
        got = await _card_call(request, "proc:cards-write", 30)
        if isinstance(got, JSONResponse):
            return got
        data, tid, sid = got
        product, patch = data.get("product"), data.get("patch")
        if product not in ("gpt", "claude", "grok") or not isinstance(patch, dict):
            return _err(422, "validation", "product and patch are required")
        try:
            return JSONResponse(
                await cardpool.set_rule(ctx, tid, sid, str(product), patch), headers=NO_STORE
            )
        except Exception as exc:
            return _card_error(exc)

    async def cards_reset_failed(request: Request) -> JSONResponse:
        got = await _card_call(request, "proc:cards-write", 30)
        if isinstance(got, JSONResponse):
            return got
        data, tid, sid = got
        ids = data.get("card_ids")
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= cardpool.cp.MAX_RESET_CARDS
            or any(isinstance(i, bool) or not isinstance(i, int) or i < 1 for i in ids)
        ):
            return _err(422, "validation", "card_ids: 1-50 explicit card ids are required")
        try:
            return JSONResponse(
                await cardpool.reset_failed(ctx, tid, sid, sorted(set(ids))), headers=NO_STORE
            )
        except Exception as exc:
            return _card_error(exc)

    def _cdk_error(exc: Exception) -> JSONResponse:
        if isinstance(exc, redeem_admin.BadCode):
            return _err(422, "validation", "That is not a redemption code")
        if isinstance(exc, LookupError) and not isinstance(exc, service.SupplierNotFound):
            return _err(404, "cdk_not_found", "The supplier has no such code")
        if isinstance(exc, redeem_admin.CdkRejected):
            return _err(409, "cdk_rejected", "The supplier refused: " + exc.code[:40])
        return _card_error(exc)

    async def redeem_cdk_status(request: Request) -> JSONResponse:
        """``POST /internal/redeem/cdk-status {"tenant_id", "code"}``: the vendor's status of a code
        (read only). The code is used in memory for this one call."""
        got = await _card_call(request, "proc:redeem-admin", 60)
        if isinstance(got, JSONResponse):
            return got
        data, tid, _ = got
        try:
            return JSONResponse(
                await redeem_admin.cdk_status(ctx, tid, str(data.get("code") or "")),
                headers=NO_STORE,
            )
        except Exception as exc:
            return _cdk_error(exc)

    async def redeem_cdk_set(request: Request) -> JSONResponse:
        """``POST /internal/redeem/cdk-set {"tenant_id", "code", "action": disable|enable}``."""
        got = await _card_call(request, "proc:redeem-admin-write", 30)
        if isinstance(got, JSONResponse):
            return got
        data, tid, _ = got
        action = data.get("action")
        if action not in ("disable", "enable"):
            return _err(422, "validation", "action must be disable or enable")
        try:
            return JSONResponse(
                await redeem_admin.cdk_set(ctx, tid, str(data.get("code") or ""), action),
                headers=NO_STORE,
            )
        except Exception as exc:
            return _cdk_error(exc)

    return Starlette(
        routes=[
            Route("/internal/redeem/cdk-status", redeem_cdk_status, methods=["POST"]),
            Route("/internal/redeem/cdk-set", redeem_cdk_set, methods=["POST"]),
            Route("/internal/cards/pool", cards_pool, methods=["POST"]),
            Route("/internal/cards/schedule", cards_schedule, methods=["POST"]),
            Route("/internal/cards/rules", cards_rules, methods=["POST"]),
            Route("/internal/cards/rules-set", cards_rules_set, methods=["POST"]),
            Route("/internal/cards/reset-failed", cards_reset_failed, methods=["POST"]),
            Route("/internal/direct-order", direct_order, methods=["POST"]),
            Route("/internal/charge-check", charge_check, methods=["POST"]),
            Route("/internal/regions", regions, methods=["POST"]),
            Route("/internal/half-check", half_check, methods=["POST"]),
            Route("/internal/direct-preflight", direct_preflight, methods=["POST"]),
            Route("/internal/support-session", support_read, methods=["POST"]),
            Route("/healthz", healthz),
            Route("/internal/probe", probe, methods=["POST"]),
            Route("/internal/check-offer", check_offer, methods=["POST"]),
        ]
    )
