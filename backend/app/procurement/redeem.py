"""Public CDK redemption proxy (review-v12 §8): ``/redeem/*`` -> ZovoCard ``/api/v1/cdk/*``.

Runs inside the procurement process on its OWN listener (``APP_REDEEM_PORT``), never on
the internal API port. Caddy on ``redeem.<domain>`` is the only client. The four vendor steps share
one egress IP (the procurement network) and one constant ``X-Redemption-Device`` (config).

Rules (each has a test in tests/test_redeem.py):

* stateless apart from Redis rate-limit counters (``rl:proc:redeem-*`` keys, hashed identifiers);
* the buyer's credential and the CDK code live only in the request/response in flight: never in a
  log line, audit row, Redis key, exception message or response body of ours;
* strict input: body cap, JSON object only, unknown keys refused, per-field length and charset;
* upstream answers are reduced to whitelisted fields and a small fixed error-code set;
* no CORS, no cookies, ``Cache-Control: no-store``; a cross-origin ``Origin`` is refused;
* every attempt is logged to ``redeem_attempts`` (issue #31, §23) with a MASKED code, the keyed
  code fingerprint and a keyed hash of the client's /24 or /48 only; the console's maintenance
  switch answers ``maintenance`` before any vendor call, and ``GET /redeem/config`` serves the
  announcement text. The code -> token link lives in this process's memory for 20 minutes.

Environment selection: the proxy cannot tell cheaply and safely which supplier environment issued
a code (card rows hold only sealed secrets and the procurement role cannot read them), so it uses
the single enabled ZovoCard supplier of the platform tenant (``APP_REDEEM_SUPPLIER_ENV``
pins sandbox/production when both are enabled; an ambiguous setup answers ``unavailable``).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import time
import uuid
from collections import OrderedDict
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app.core.db import tenant_session
from app.core.middleware import client_ip_from
from app.core.ratelimit import hit
from app.models import RedeemAttempt, Tenant
from app.procurement import redeem_log as rl
from app.procurement import service
from app.procurement.context import ProcurementContext
from app.suppliers import direct_progress as dp
from app.suppliers.registry import SupplierConfigError, load_secrets
from app.suppliers.zovocard import PRODUCTION_BASE_URL, SANDBOX_BASE_URL
from app.suppliers.zovocard.client import sanitize_error_code

log = logging.getLogger("app.procurement.redeem")

# a real ChatGPT session text is ~37 KB (measured 2026-11-16) and grows when JSON-escaped
MAX_BODY = 96 * 1024
BODY_DEADLINE_S = 10.0  # whole request body
STEPS = ("preview", "preflight", "redeem", "result")
DEVICE_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
CODE_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=:-]{8,512}$")
# the vendor's preflight token is a compact JWE of ~9.8 KB (measured 2026-11-20 on production)
PREFLIGHT_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=:-]{8,32768}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
SESSION_MAX = 65536
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ISO_RE = re.compile(r"^[0-9T:.+\-Z ]{1,40}$")
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
TERMINAL = frozenset(
    {"completed", "succeeded", "declined", "failed", "failed_precharge", "cancelled"}
)

# fixed error vocabulary: code -> (HTTP status, message). The SPA localises by code.
ERRORS: dict[str, tuple[int, str]] = {
    "invalid_request": (400, "The request is malformed"),
    "forbidden": (403, "Cross-origin requests are not allowed"),
    "not_found": (404, "Nothing found for this token"),
    "method_not_allowed": (405, "Method not allowed"),
    "payload_too_large": (413, "The request is too large"),
    "code_unavailable": (422, "This code is invalid, used or disabled"),
    "credential_invalid": (422, "The account credential was not accepted"),
    "account_ineligible": (422, "This account cannot take this plan right now"),
    "rejected": (422, "The redemption was rejected"),
    "session_expired": (409, "The redemption session expired; start again"),
    "conflict": (409, "This request id was already used with other parameters"),
    "rate_limited": (429, "Too many requests, try again later"),
    "maintenance": (503, "Redemption is temporarily unavailable"),
    "unavailable": (503, "The redemption service is unavailable"),
    "timeout": (504, "The redemption service did not answer in time"),
}

_CODE_UNAVAILABLE = frozenset(
    {"CDK_UNAVAILABLE", "CDK_NOT_ACCESSIBLE", "CDK_NOT_FOUND", "CDK_DISABLED", "CDK_USED",
     "CDK_INVALID", "INVALID_CDK", "CDK_EXPIRED"}
)  # fmt: skip
_SESSION_EXPIRED = frozenset(
    {"REDEMPTION_SESSION_INVALID", "PREFLIGHT_REQUIRED", "PREFLIGHT_INVALID",
     "PREFLIGHT_EXPIRED", "REDEMPTION_TOKEN_EXPIRED"}
)  # fmt: skip
_CREDENTIAL_INVALID = frozenset(
    {"CREDENTIAL_REQUIRED", "GPT_SESSION_INVALID", "CLAUDE_SESSION_INVALID",
     "GROK_SESSION_INVALID", "PRECHECK_REJECTED"}
)  # fmt: skip
_ACCOUNT_INELIGIBLE = frozenset(
    {"GPT_PLAN_ALREADY_ACTIVE", "GPT_RENEW_TARGET_NOT_PRO", "GPT_RENEW_SUBSCRIPTION_EXPIRED",
     "GPT_CREDIT_REQUIRES_SUBSCRIPTION", "grok_half_ineligible"}
)  # fmt: skip

# non-ok attempt rows one platform tenant may write a minute, whatever the client networks
LOG_TENANT_PER_MIN = 600
LOG_CLASS_PER_MIN = 30


class RedeemError(Exception):
    """Carries only a code from :data:`ERRORS` (never vendor text, never buyer input)."""

    def __init__(self, code: str, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


def parse_limits(spec: str) -> dict[str, int]:
    """``"preview=10,redeem=6,..."`` -> {step: n}; every step must be present and positive."""
    out: dict[str, int] = {}
    for part in spec.split(","):
        step, _, raw = part.strip().partition("=")
        if step not in STEPS or not raw.isdigit() or int(raw) < 1:
            raise ValueError("invalid redeem limits spec")
        out[step] = int(raw)
    if set(out) != set(STEPS):
        raise ValueError("redeem limits must name every step")
    return out


def map_upstream_error(step: str, status: int, error_code: str | None) -> str:
    """Vendor status + ``error_code`` -> one code of :data:`ERRORS`."""
    if error_code in _CODE_UNAVAILABLE:
        return "code_unavailable"
    if error_code in _SESSION_EXPIRED:
        return "session_expired"
    if error_code in _CREDENTIAL_INVALID:
        return "credential_invalid"
    if error_code in _ACCOUNT_INELIGIBLE:
        return "account_ineligible"
    if error_code == "IDEMPOTENCY_CONFLICT":
        return "conflict"
    if status == 404 or error_code == "not_found":
        # production answers an unknown code at preview with HTTP 400 ``not_found`` (2026-11-17)
        return "code_unavailable" if step == "preview" else "not_found"
    if status == 429:
        return "rate_limited"
    if status in (401, 403) or status >= 500 or status < 400:
        return "unavailable"
    return "rejected"


def _s(value: Any, limit: int = 64) -> str | None:
    """A short ASCII-safe string from a vendor field (or None)."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return sanitize_error_code(str(value)[:limit])


def _iso(value: Any) -> str | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 < value < 10**13:
        return value
    if isinstance(value, str) and _ISO_RE.fullmatch(value):
        return value
    return None


def _match(value: Any, regex: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and regex.fullmatch(value) else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def shape_preview(data: Any) -> dict[str, Any]:
    d = _dict(data)
    token = d.get("redemption_token")
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise RedeemError("unavailable")
    cap = d.get("funding_cap_minor")
    return {
        "redemption_token": token,
        "expires_at": _iso(d.get("expires_at")),
        "plan": _s(d.get("plan")),
        "plan_flow": _s(d.get("plan_flow")),
        "funding_cap_minor": cap if isinstance(cap, int) and not isinstance(cap, bool) else None,
    }


def shape_preflight(data: Any) -> dict[str, Any]:
    d = _dict(data)
    token = d.get("preflight_token")
    if token is not None and not (isinstance(token, str) and PREFLIGHT_TOKEN_RE.fullmatch(token)):
        raise RedeemError("unavailable")
    country, currency = d.get("payment_country"), d.get("payment_currency")
    recovery = d.get("subscription_recovery_required")
    return {
        # Grok / Claude codes get no token: redeem then needs the credential again
        "preflight_token": token,
        "preflight_expires_at": _iso(d.get("preflight_expires_at")),
        "payment_country": _match(country, _COUNTRY_RE),
        "payment_currency": _match(currency, _CURRENCY_RE),
        "subscription_recovery_required": recovery if isinstance(recovery, bool) else False,
    }  # fmt: skip


def shape_redeem(data: Any) -> dict[str, Any]:
    d = _dict(data)
    order = _dict(d.get("order"))
    status = _s(order.get("status") or d.get("status"))
    prog = dp.redeem_progress(
        status,
        order.get("stage") or d.get("stage"),
        order.get("payment_state"),
        order.get("funding_state"),
    )
    return {
        "progress": prog,
        # the vendor nests it as data.order.id (verified in the sandbox 2026-11-16)
        "order_id": _s(
            order.get("order_id") or order.get("id") or d.get("id") or d.get("order_id"), 80
        ),
        "status": status,
        "stage": _s(order.get("stage") or d.get("stage")),
        "terminal": status in TERMINAL,
    }


def shape_result(data: Any) -> dict[str, Any]:
    d = _dict(data)
    order = _dict(d.get("order"))
    status = _s(order.get("status"))
    events = []
    raw = d.get("events")
    for ev in (raw if isinstance(raw, list) else [])[:50]:
        e = _dict(ev)
        events.append(
            {
                "type": _s(e.get("type") or e.get("event") or e.get("event_type")),
                "status": _s(e.get("status")),
                "at": _iso(e.get("created_at") or e.get("at")),
            }
        )
    done_at = _iso(order.get("completed_at"))
    prog = dp.redeem_progress(
        status,
        order.get("stage"),
        order.get("payment_state"),
        order.get("funding_state"),
        done_at,
    )
    return {
        "progress": prog,
        "order_id": _s(order.get("order_id") or order.get("id"), 80),
        "status": status,
        "stage": _s(order.get("stage")),
        "cdk_status": _s(order.get("cdk_status")),
        "terminal": status in TERMINAL,
        "events": events,
    }


def _public_error(code: str, retry_after: int | None = None) -> JSONResponse:
    status, message = ERRORS[code]
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    resp = JSONResponse({"error": {"code": code, "message": message}}, status, headers=headers)
    return resp


def _check_str(value: Any, regex: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not regex.fullmatch(value):
        raise RedeemError("invalid_request")
    return value


def _credential(value: Any) -> dict[str, str]:
    """``{"mode": "session", "session": "<text>"}`` and nothing else."""
    if not isinstance(value, dict) or set(value) != {"mode", "session"}:
        raise RedeemError("invalid_request")
    session = value["session"]
    if value["mode"] != "session" or not isinstance(session, str):
        raise RedeemError("invalid_request")
    if not 8 <= len(session) <= SESSION_MAX or _CTRL_RE.search(session):
        raise RedeemError("invalid_request")
    return {"mode": "session", "session": session}


def parse_body(step: str, body: dict[str, Any]) -> dict[str, Any]:
    """Validate one step's JSON object; return the vendor request body (whitelisted keys only)."""
    if step == "preview":
        allowed = {"code"}
    elif step == "preflight":
        allowed = {"redemption_token", "credential"}
    elif step == "redeem":
        allowed = {"redemption_token", "preflight_token", "credential", "client_request_id"}
    else:
        allowed = {"redemption_token"}
    if not set(body) <= allowed:
        raise RedeemError("invalid_request")
    if step == "preview":
        return {"code": _check_str(body.get("code"), CODE_RE)}
    out: dict[str, Any] = {"redemption_token": _check_str(body.get("redemption_token"), TOKEN_RE)}
    if step == "preflight":
        out["credential"] = _credential(body.get("credential"))
    elif step == "redeem":
        out["client_request_id"] = _check_str(body.get("client_request_id"), REQUEST_ID_RE)
        if body.get("preflight_token") is not None:
            out["preflight_token"] = _check_str(body["preflight_token"], PREFLIGHT_TOKEN_RE)
        if body.get("credential") is not None:
            out["credential"] = _credential(body["credential"])
        if "preflight_token" not in out and "credential" not in out:
            raise RedeemError("invalid_request")
    return out


SHAPERS = {
    "preview": shape_preview,
    "preflight": shape_preflight,
    "redeem": shape_redeem,
    "result": shape_result,
}


def create_redeem_app(ctx: ProcurementContext) -> Starlette:
    cfg = ctx.settings
    if cfg.env == "prod" and cfg.redeem_supplier_env not in ("sandbox", "production"):
        # a buyer's account credential must never go to whichever supplier happens to be enabled
        raise ValueError("APP_REDEEM_SUPPLIER_ENV (sandbox | production) is required in prod")
    # httpx logs full request URLs at INFO and the vendor's result call carries the redemption
    # token in its query string: keep these loggers quiet whatever the process log level is.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not DEVICE_RE.fullmatch(cfg.redeem_device):
        raise ValueError("APP_REDEEM_DEVICE must match [A-Za-z0-9._-]{8,64}")
    if cfg.redeem_supplier_env not in ("", "sandbox", "production"):
        raise ValueError("APP_REDEEM_SUPPLIER_ENV must be sandbox or production")
    ip_limits = parse_limits(cfg.redeem_ip_limits)
    global_limits = parse_limits(cfg.redeem_global_limits)
    trusted = tuple(
        ipaddress.ip_network(p.strip(), strict=False)
        for p in cfg.redeem_trusted_proxies.split(",")
        if p.strip()
    )
    hkey = (cfg.procurement_token or "redeem").encode()
    state: dict[str, Any] = {"base": None, "at": 0.0, "own": None, "site": None, "site_at": -1e9}
    # redemption token (keyed hash) -> what the log may know of its code; memory only, 20 minutes,
    # bounded. A restart only costs the masked code on the later steps of an attempt in flight.
    memory: OrderedDict[str, dict[str, Any]] = OrderedDict()
    ipkey = hmac.new(hkey, b"redeem-ip-class", hashlib.sha256).digest()

    def ident(kind: str, value: str) -> str:
        # keyed hash: Redis keys never hold a code/token, nor allow an offline dictionary check
        return hmac.new(hkey, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()[:32]

    def remember(token: str, meta: dict[str, Any]) -> None:
        now = time.monotonic()
        for k in [k for k, v in memory.items() if now - v["at"] > 1200][:200]:
            memory.pop(k, None)
        while len(memory) >= 5000:
            memory.popitem(last=False)
        memory[ident("token", token)] = {**meta, "at": now}

    def recall(token: str | None) -> dict[str, Any] | None:
        got = memory.get(ident("token", token)) if token else None
        if got is not None and time.monotonic() - got["at"] > 1200:
            memory.pop(ident("token", token or ""), None)
            return None
        return got

    async def site() -> tuple[uuid.UUID | None, dict[str, Any]]:
        """``(platform tenant id, normalised console settings)``, cached for a few seconds. A read
        failure keeps the last known settings (defaults at first); several platform tenants =
        ambiguous = no logging, defaults."""
        now = time.monotonic()
        if state["site"] is not None and now - state["site_at"] < cfg.redeem_settings_ttl_s:
            return state["site"]
        try:
            async with tenant_session(ctx.maker, None) as db:
                rows = (
                    await db.execute(
                        select(Tenant.id, Tenant.redeem_settings).where(Tenant.kind == "platform")
                    )
                ).all()
            got = (
                (rows[0].id, rl.normalize_settings(rows[0].redeem_settings))
                if len(rows) == 1
                else (None, rl.default_settings())
            )
        except Exception as exc:
            log.error("redeem: settings read failed error=%s", type(exc).__name__)
            got = state["site"] or (None, rl.default_settings())
        state["site"], state["site_at"] = got, now
        return got

    async def record(
        step: str, outcome: str, reason: str | None, meta: dict[str, Any] | None, ip: str | None
    ) -> None:
        """Best effort and bounded: a failed insert never changes the buyer's answer, and a client
        cannot grow the table faster than 30 non-ok rows a minute."""
        try:
            tid, _ = await site()
            if tid is None:
                return
            if outcome != "ok":
                # budget per client NETWORK (/24, /48), so rotating addresses inside one prefix
                # share it; plus a per-tenant ceiling. Over either: dropped and counted, not written
                cls = rl.ip_class_hash(ipkey, ip) or "unknown"
                if not (
                    await hit(
                        ctx.redis, "proc:redeem-log", ident("cls", cls), LOG_CLASS_PER_MIN, 60
                    )
                )[0] or (
                    not (
                        await hit(
                            ctx.redis,
                            "proc:redeem-log-all",
                            ident("tenant", str(tid)),
                            LOG_TENANT_PER_MIN,
                            60,
                        )
                    )[0]
                ):
                    state["log_dropped"] = int(state.get("log_dropped", 0)) + 1
                    return
            m = meta or {}
            async with tenant_session(ctx.maker, tid) as db:
                db.add(
                    RedeemAttempt(
                        id=uuid.uuid4(),
                        tenant_id=tid,
                        step=step,
                        outcome=outcome,
                        reason=reason[:32] if reason else None,
                        code_head=m.get("head"),
                        code_tail=m.get("tail"),
                        code_fp=m.get("fp"),
                        plan=m.get("plan"),
                        ip_hash=rl.ip_class_hash(ipkey, ip),
                    )
                )
                await db.commit()
        except Exception as exc:
            log.warning("redeem: attempt log failed error=%s", type(exc).__name__)

    def http_client() -> httpx.AsyncClient:
        if ctx.http is not None:
            return ctx.http
        if state["own"] is None:
            state["own"] = httpx.AsyncClient(
                timeout=httpx.Timeout(cfg.redeem_upstream_timeout_s, connect=5.0),
                follow_redirects=False,
                trust_env=False,  # no ambient proxy: egress is the procurement network
            )
        return state["own"]

    async def upstream_base() -> str:
        now = time.monotonic()
        if state["base"] and now - state["at"] < 30:
            return state["base"]
        try:
            refs = [
                r
                for r in await service.enabled_suppliers(ctx)
                if r.adapter == "zovocard"
                and (not cfg.redeem_supplier_env or r.env == cfg.redeem_supplier_env)
            ]
        except Exception as exc:
            log.error("redeem: supplier lookup failed: %s", type(exc).__name__)
            raise RedeemError("unavailable") from None
        if len({(r.code, r.env) for r in refs}) != 1:
            log.error("redeem: expected exactly one enabled zovocard supplier, found %d", len(refs))
            raise RedeemError("unavailable")
        ref = refs[0]
        try:
            sec = load_secrets(
                ref.code,
                ref.env,
                ctx.environ,
                default_base=PRODUCTION_BASE_URL if ref.env == "production" else SANDBOX_BASE_URL,
            )
        except SupplierConfigError:
            raise RedeemError("unavailable") from None
        parts = urlsplit(sec.base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise RedeemError("unavailable")
        if parts.scheme != "https" and cfg.env not in ("dev", "test"):
            raise RedeemError("unavailable")
        base = f"{parts.scheme}://{parts.netloc}/api/v1/cdk"
        state["base"], state["at"] = base, now
        return base

    async def count(name: str, who: str, limit: int, window: int) -> None:
        try:
            allowed, reset = await hit(ctx.redis, name, who, limit, window)
        except Exception as exc:
            log.error("redeem: rate limit store failed: %s", type(exc).__name__)
            raise RedeemError("unavailable") from None  # fail closed
        if not allowed:
            raise RedeemError("rate_limited", retry_after=max(1, reset))

    async def call_vendor(step: str, base: str, body: dict[str, Any] | None, token: str | None):
        headers = {
            "X-Redemption-Device": cfg.redeem_device,
            "Accept": "application/json",
            "User-Agent": "procurement-redeem/1",
        }
        try:
            if step == "result":
                resp = await http_client().get(
                    f"{base}/result", params={"token": token}, headers=headers
                )
            else:
                resp = await http_client().post(f"{base}/{step}", json=body, headers=headers)
        except httpx.TimeoutException as exc:
            log.warning("redeem step=%s upstream=timeout %s", step, type(exc).__name__)
            raise RedeemError("timeout") from None
        except httpx.HTTPError as exc:
            log.warning("redeem step=%s upstream=error %s", step, type(exc).__name__)
            raise RedeemError("unavailable") from None
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        status = resp.status_code
        error_code = (
            sanitize_error_code(payload.get("error_code")) if isinstance(payload, dict) else None
        )
        vendor_code = payload.get("code") if isinstance(payload, dict) else None
        if 200 <= status < 300 and isinstance(payload, dict) and vendor_code == 0:
            return payload.get("data")
        if 200 <= status < 300:  # HTTP 200 with code != 0, or not an envelope: an error
            status = (
                vendor_code if isinstance(vendor_code, int) and 400 <= vendor_code <= 599 else 502
            )
        code = map_upstream_error(step, status, error_code)
        log.info("redeem step=%s upstream_status=%d outcome=%s", step, status, code)
        retry = None
        if code == "rate_limited":
            raw = resp.headers.get("retry-after", "")
            retry = min(int(raw), 300) if raw.isdigit() else 30
        raise RedeemError(code, retry_after=retry)

    def origin_ok(request: Request) -> bool:
        origin = request.headers.get("origin")
        if origin is None:
            return True  # same-origin GETs and non-browser clients send none
        return urlsplit(origin).netloc.lower() == request.headers.get("host", "").lower()

    async def read_json(request: Request) -> dict[str, Any]:
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY:
            raise RedeemError("payload_too_large")
        if (
            request.headers.get("content-type", "").split(";")[0].strip().lower()
            != "application/json"
        ):
            raise RedeemError("invalid_request")
        raw = bytearray()

        async def drain() -> None:
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_BODY:
                    raise RedeemError("payload_too_large")

        try:
            # a body that trickles in must not hold one of the listener's few connections
            await asyncio.wait_for(drain(), BODY_DEADLINE_S)
        except asyncio.TimeoutError:
            raise RedeemError("invalid_request") from None
        try:
            body = json.loads(bytes(raw))
        except (ValueError, RecursionError):
            raise RedeemError("invalid_request") from None
        if not isinstance(body, dict):
            raise RedeemError("invalid_request")
        return body

    def code_meta(code: str, tenant_id: uuid.UUID | None, plan: str | None) -> dict[str, Any]:
        head, tail = rl.mask_code(code)
        fp = None
        if ctx.sealer is not None and tenant_id is not None:
            # the fingerprint scheme of delivered codes (CardSealer.fingerprint): the console
            # matches it to what we sold; it does not reveal the code
            fp = ctx.sealer.fingerprint(str(tenant_id), code)
        return {"head": head or None, "tail": tail or None, "fp": fp, "plan": plan}

    async def handle(request: Request, step: str) -> Response:
        ip: str | None = None
        meta: dict[str, Any] | None = None
        try:
            if not origin_ok(request):
                raise RedeemError("forbidden")
            peer = request.client.host if request.client else None
            ip = client_ip_from(peer, request.headers.get("x-forwarded-for"), trusted) or "unknown"
            tid, conf = await site()
            limit = conf["ip_limits"].get(step, ip_limits[step])
            await count(f"proc:redeem-ip-{step}", ident("ip", ip), limit, 60)
            if conf["maintenance"] and step != "result":
                raise RedeemError("maintenance")  # before any body is read or vendor is called
            # POST only, also for the result: a token never travels in a URL on our side
            parsed = parse_body(step, await read_json(request))
            body: dict[str, Any] | None = parsed
            token = parsed.get("redemption_token")
            if conf["maintenance"] and recall(token) is None:
                # a read for a token we issued before maintenance still works (no new vendor spend
                # on a new code); any other token is blocked
                raise RedeemError("maintenance")
            if step == "preview":
                meta = code_meta(parsed["code"], tid, None)
                await count(
                    "proc:redeem-code",
                    ident("code", parsed["code"]),
                    cfg.redeem_code_per_10min,
                    600,
                )
            else:
                meta = recall(token)
                await count(
                    "proc:redeem-token", ident("token", token), cfg.redeem_token_per_min, 60
                )
            await count(f"proc:redeem-all-{step}", "all", global_limits[step], 60)
            data = await call_vendor(step, await upstream_base(), body, token)
            shaped = SHAPERS[step](data)
        except RedeemError as exc:
            outcome = {"rate_limited": "rate_limited", "maintenance": "maintenance"}.get(
                exc.code,
                "invalid" if exc.code in ("invalid_request", "payload_too_large") else "error",
            )
            # not logged: cross-origin / wrong-method noise, and the normal "no result yet"
            if exc.code not in ("forbidden", "not_found", "method_not_allowed"):
                await record(step, outcome, exc.code, meta, ip)
            return _public_error(exc.code, exc.retry_after)
        except Exception as exc:  # never include str(exc): it could echo input
            log.error("redeem step=%s internal_error=%s", step, type(exc).__name__)
            await record(step, "error", "unavailable", meta, ip)
            return _public_error("unavailable")
        if step == "preview":
            assert meta is not None
            meta["plan"] = shaped.get("plan")
            remember(shaped["redemption_token"], meta)
            await record(step, "ok", None, meta, ip)
        elif step in ("preflight", "redeem"):
            await record(step, "ok", None, meta, ip)
        elif shaped["terminal"] and not (meta or {}).get("done"):  # result: log the end once
            done = shaped["status"] in ("completed", "succeeded")
            reason = None if done else (shaped["progress"] or {}).get("reason") or "not_completed"
            if meta is not None:
                meta["done"] = True
            await record(step, "completed" if done else "failed", reason, meta, ip)
        log.info("redeem step=%s ok", step)
        return JSONResponse({"data": shaped})

    async def config(request: Request) -> Response:
        """``GET /redeem/config``: announcement and maintenance state (display text only)."""
        try:
            peer = request.client.host if request.client else None
            ip = client_ip_from(peer, request.headers.get("x-forwarded-for"), trusted) or "unknown"
            await count("proc:redeem-ip-config", ident("ip", ip), 60, 60)
            payload = rl.public_config((await site())[1])
        except RedeemError as exc:
            return _public_error(exc.code, exc.retry_after)
        except Exception as exc:
            log.error("redeem config internal_error=%s", type(exc).__name__)
            return _public_error("unavailable")
        return JSONResponse({"data": payload})

    def make_route(step: str):
        async def endpoint(request: Request) -> Response:
            resp = await (config(request) if step == "config" else handle(request, step))
            # the config is public display text: 30 s of caching bounds how long a maintenance
            # switch takes to show; every other response is no-store
            resp.headers["Cache-Control"] = (
                "public, max-age=30" if step == "config" and resp.status_code == 200 else "no-store"
            )
            resp.headers["X-Content-Type-Options"] = "nosniff"
            resp.headers["Referrer-Policy"] = "no-referrer"
            return resp

        return endpoint

    async def not_allowed(_: Request, exc: Exception) -> Response:
        resp = _public_error(
            "method_not_allowed" if getattr(exc, "status_code", 404) == 405 else "not_found"
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    async def close() -> None:
        if state["own"] is not None:
            await state["own"].aclose()
            state["own"] = None

    routes = [
        Route("/redeem/preview", make_route("preview"), methods=["POST"]),
        Route("/redeem/preflight", make_route("preflight"), methods=["POST"]),
        Route("/redeem/redeem", make_route("redeem"), methods=["POST"]),
        Route("/redeem/result", make_route("result"), methods=["POST"]),
        Route("/redeem/config", make_route("config"), methods=["GET"]),
    ]
    app = Starlette(routes=routes, exception_handlers={404: not_allowed, 405: not_allowed})
    app.state.close = close
    return app
