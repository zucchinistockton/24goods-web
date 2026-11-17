"""ZovoCard OpenAPI HTTP client: the one place for auth, envelopes, errors and rate limiting.

* Auth: ``X-API-Key: <app_secret>`` (+ ``X-App-Id`` when configured).
* Every write carries an ``Idempotency-Key`` matching ``^[A-Za-z0-9._:-]{1,80}$``.
* Envelope ``{"code": 0, "msg": ..., "data": ...}``; an HTTP 2xx whose body says ``code != 0``
  is an error (vendor §3 "two misbehaving responses").
* Errors go through :func:`app.suppliers.zovocard.errors.classify` (status + error_code only).
* Token bucket: 60 requests/min per process (vendor limit 100/min/key; stricter value wins).
* Timeouts: 10 s connect, 40 s read (the vendor holds idempotent replays up to 30 s).
* Production guard: any non-GET request is refused when ``env == "production"`` unless
  ``APP_PROCUREMENT_ALLOW_PRODUCTION_WRITES=1`` (review-v12 §11). No dev/demo env sets it.
* Logging: method, path, status, error_code and a hash of the idempotency key. Never a body,
  a query string or a header value.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, NoReturn

import httpx

from app.suppliers.base import IDEMPOTENCY_KEY_RE, Outcome, SupplierError
from app.suppliers.zovocard.errors import classify

log = logging.getLogger("app.suppliers.zovocard")

PRODUCTION_BASE_URL = "https://zovocard.com/openapi/v1"
SANDBOX_BASE_URL = "https://sandbox.zovocard.com/openapi/v1"
MAX_BODY = 4 * 1024 * 1024  # a plans payload is ~20 KB; refuse anything absurd
_PATH_RE = re.compile(r"^/[A-Za-z0-9/_.-]{1,200}$")
# vendor error codes end up in health_detail and audit rows: only this alphabet is kept
_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
MALFORMED_CODE = "malformed_error_code"


def sanitize_error_code(value: object) -> str | None:
    """None when absent; the vendor code when it is plain; a fixed marker otherwise (the marker is
    not in the error table, so it classifies like any unknown code)."""
    if value is None or value == "":
        return None
    if isinstance(value, str) and _CODE_RE.fullmatch(value):
        return value
    return MALFORMED_CODE


class ProductionWriteBlocked(SupplierError):
    """A write against a production supplier without the explicit opt-in. Nothing was sent."""

    def __init__(self) -> None:
        super().__init__(
            "production writes are disabled (APP_PROCUREMENT_ALLOW_PRODUCTION_WRITES)",
            outcome=Outcome.UNAVAILABLE,
            error_code="production_writes_disabled",
        )


@dataclass(frozen=True)
class ZovoConfig:
    base_url: str
    app_secret: str = field(repr=False)
    env: str = "sandbox"  # sandbox | production
    app_id: str = ""
    allow_production_writes: bool = False
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 40.0
    rate_per_min: int = 60
    burst: int = 10

    def __post_init__(self) -> None:
        if self.env not in ("sandbox", "production"):
            raise ValueError("env must be sandbox or production")
        if not self.base_url.startswith(("https://", "http://")):
            raise ValueError("base_url must be an http(s) URL")
        if not self.app_secret:
            raise ValueError("app_secret is required")

    @property
    def production_target(self) -> bool:
        """Writes need the opt-in. ``env=production`` is production; so is a ``sandbox`` row whose
        host is not the vendor's own sandbox (it may well be production). Exempt are only the
        vendor sandbox over https and OUR simulator over plain http: a single-label host (a
        container or test name such as ``mockzovo``) or a loopback / private IP literal. Any
        other http host, e.g. ``http://api.<vendor>``, counts as production (re-audit of #45)."""
        if self.env == "production":
            return True
        from app.suppliers.zovocard.direct import real_sandbox  # local: keeps imports one-way

        if self.base_url.startswith("https://"):
            return not real_sandbox(self.base_url, self.env)
        return not _simulator_host(self.base_url)


def _simulator_host(url: str) -> bool:
    """A plain-http base URL that can only be our own simulator: a single-label hostname (never
    resolvable as a public internet name) or a loopback / private IP literal."""
    import ipaddress
    from urllib.parse import urlsplit

    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host  # a container / test name; a public host always has a dot
    return ip.is_loopback or (ip.is_private and not ip.is_link_local)


@dataclass(frozen=True)
class ZovoResponse:
    status: int
    data: Any
    replayed: bool = False


class TokenBucket:
    """Async token bucket: ``rate_per_min`` sustained, ``burst`` tokens at most."""

    def __init__(
        self,
        rate_per_min: int,
        burst: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_min <= 0 or burst <= 0:
            raise ValueError("rate and burst must be positive")
        self._rate = rate_per_min / 60.0
        self._cap = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._cap, self._tokens + (now - self._last) * self._rate)
        self._last = now

    async def acquire(self) -> None:
        async with self._lock:
            self._refill()
            while self._tokens < 1.0:
                await self._sleep((1.0 - self._tokens) / self._rate)
                self._refill()
            self._tokens -= 1.0


def key_hash(key: str | None) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12] if key else "-"


def _retry_after(resp: httpx.Response) -> timedelta | None:
    raw = resp.headers.get("retry-after", "")
    if raw.isdigit():
        return timedelta(seconds=min(int(raw), 3600))
    return None


class ZovoClient:
    def __init__(
        self,
        config: ZovoConfig,
        *,
        http: httpx.AsyncClient | None = None,
        bucket: TokenBucket | None = None,
    ) -> None:
        self.config = config
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(
                config.read_timeout_s, connect=config.connect_timeout_s, pool=config.read_timeout_s
            ),
            follow_redirects=False,
            trust_env=False,  # no ambient proxy settings: egress is the compose network
        )
        self._bucket = bucket or TokenBucket(config.rate_per_min, config.burst)

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()

    # ---- public helpers
    async def get(self, path: str, params: Mapping[str, str] | None = None) -> ZovoResponse:
        return await self.request("GET", path, params=params)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        endpoint: str | None = None,
        read_only_post: bool = False,
    ) -> ZovoResponse:
        """``read_only_post``: a POST that changes nothing upstream (the direct preflight): it is
        classified like a read (a timeout is simply repeated) and carries no Idempotency-Key, but it
        still obeys the production-write gate because it carries a buyer's credential."""
        method = method.upper()
        mutating = method != "GET"
        write = mutating and not read_only_post
        if not _PATH_RE.fullmatch(path) or ".." in path:
            raise ValueError("invalid API path")
        if mutating:
            # checked BEFORE anything else: a production write never leaves the process
            if self.config.production_target and not self.config.allow_production_writes:
                log.warning("zovocard refused production write %s %s", method, path)
                raise ProductionWriteBlocked()
        if write:
            if idempotency_key is None or not IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
                raise ValueError("writes need an Idempotency-Key matching ^[A-Za-z0-9._:-]{1,80}$")
        elif idempotency_key is not None:
            raise ValueError("reads carry no Idempotency-Key")

        headers = {
            "X-API-Key": self.config.app_secret,
            "Accept": "application/json",
            "User-Agent": "procurement-client/1",
        }
        if self.config.app_id:
            headers["X-App-Id"] = self.config.app_id
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        url = self.config.base_url.rstrip("/") + path
        kh = key_hash(idempotency_key)

        await self._bucket.acquire()
        try:
            resp = await self._http.request(
                method, url, params=params, json=json_body, headers=headers
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # nothing reached the vendor
            log.warning("zovocard %s %s connect_error=%s key=%s", method, path, _n(exc), kh)
            raise SupplierError(
                "supplier unreachable", outcome=Outcome.RETRYABLE, error_code="connect_error"
            ) from None
        except httpx.HTTPError as exc:
            # the request may have been sent: a write is ambiguous, a read can be repeated
            log.warning("zovocard %s %s transport_error=%s key=%s", method, path, _n(exc), kh)
            raise SupplierError(
                "supplier timeout after send",
                outcome=Outcome.AMBIGUOUS if write else Outcome.RETRYABLE,
                error_code="timeout_after_send",
            ) from None

        body = _decode(resp)
        status = resp.status_code
        code = body.get("code") if isinstance(body, dict) else None
        error_code = sanitize_error_code(body.get("error_code") if isinstance(body, dict) else None)
        replayed = resp.headers.get("idempotent-replayed", "").lower() == "true"

        if 200 <= status < 300 and isinstance(body, dict):
            if code == 0 or (code is None and status == 202):
                log.info("zovocard %s %s status=%d key=%s", method, path, status, kh)
                return ZovoResponse(status=status, data=body.get("data"), replayed=replayed)
            # HTTP 2xx with code != 0: an error in disguise; classify by the body code
            effective = code if isinstance(code, int) and 400 <= code <= 599 else 502
            self._fail(method, path, effective, error_code, write, endpoint, kh, resp)
        if 200 <= status < 300:
            # 2xx that is not a JSON envelope: we cannot know what happened
            self._fail(method, path, 502, None, write, endpoint, kh, resp)
        self._fail(method, path, status, error_code, write, endpoint, kh, resp)

    def _fail(
        self,
        method: str,
        path: str,
        status: int,
        error_code: str | None,
        write: bool,
        endpoint: str | None,
        kh: str,
        resp: httpx.Response,
    ) -> NoReturn:
        rule = classify(status, error_code, write=write, endpoint=endpoint)
        log.warning(
            "zovocard %s %s status=%d http=%d error_code=%s outcome=%s key=%s",
            method,
            path,
            status,
            resp.status_code,
            error_code or "-",
            rule.outcome.value,
            kh,
        )
        raise SupplierError(
            "supplier error",
            outcome=rule.outcome,
            status=status,
            error_code=error_code,
            supplier_stop=rule.supplier_stop,
            retry_after=_retry_after(resp) if status == 429 else None,
        )


def _n(exc: BaseException) -> str:
    return type(exc).__name__


def _decode(resp: httpx.Response) -> Any:
    raw = resp.content
    if len(raw) > MAX_BODY:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
