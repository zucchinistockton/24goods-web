"""vmcardio (VM card) V2 API client (ADR-055): token cache, envelope, bounded reads, error mapping.

* Auth: ``GET /v2/getAccessToken?app_id&app_secret`` -> ``data.token`` + ``data.expired_time``;
  every other call is a POST with the header ``Authorization: <token>`` (no ``Bearer``). The
  token is
  cached in memory and refreshed before it expires; an "invalid token" answer (code 400003, which
  the vendor lists as "never entered business processing") refreshes it once and repeats the call.
* Envelope ``{"code": 0, "msg": "ok", "data": ...}``; ``code == 0`` is success.
* ``X-Client-Request-Id`` (<= 64 chars of ``[A-Za-z0-9-_.:]``) is unique per app forever: a repeat
  answers ``Duplicate Client Request Id`` and NOT the first result (``error_code``
  ``duplicate_request``): the caller then looks the result up.
* Only the numeric error ``code`` is ever kept from a failure. Bodies are never logged: the card
  detail and card list replies carry full card numbers, so callers parse the few fields they need
  and drop the reply at once.
* Timeouts 5 s connect / 30 s read, no redirects, no ambient proxy, response capped at 1 MiB.
* Production guard like ZovoCard / zzshu: a card-creating write to a production target needs the
  explicit opt-in (``APP_PROCUREMENT_ALLOW_PRODUCTION_WRITES``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from app.suppliers.base import Outcome, SupplierError
from app.suppliers.zovocard.client import ProductionWriteBlocked, _simulator_host

log = logging.getLogger("app.suppliers.vmcardio")

PRODUCTION_BASE_URL = "https://vmapi.vmcardio.com"
SANDBOX_BASE_URL = "https://sandbox-api.vmcardio.com"
MAX_BODY = 1024 * 1024
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_PATH_RE = re.compile(r"^/v2/[A-Za-z]{3,40}$")
TOKEN_MARGIN_S = 600  # the vendor's advice: refresh 600 s before it expires
DEFAULT_TOKEN_TTL_S = 3600

BAD_TOKEN = 400003
NO_PERMISSION = 400004
BAD_APP_ID = 400006
BAD_SECRET = 400007
_STOP_CODES = {NO_PERMISSION, BAD_APP_ID, BAD_SECRET}
DUPLICATE_RE = re.compile(r"duplicate\s+client\s+request", re.IGNORECASE)


@dataclass(frozen=True)
class VmcardioConfig:
    base_url: str
    app_id: str
    app_secret: str = field(repr=False)
    env: str = "production"
    allow_production_writes: bool = False
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 30.0

    def __post_init__(self) -> None:
        if self.env not in ("sandbox", "production"):
            raise ValueError("env must be sandbox or production")
        if not self.base_url.startswith(("https://", "http://")):
            raise ValueError("base_url must be an http(s) URL")
        if not self.app_id or not self.app_secret:
            raise ValueError("app_id and app_secret are required")

    @property
    def production_target(self) -> bool:
        return self.env == "production" or not _simulator_host(self.base_url)


@dataclass(frozen=True)
class VmcardioResponse:
    data: Any = field(repr=False)


@dataclass(frozen=True)
class _Parsed:
    code: int | None
    data: Any = field(repr=False)
    duplicate: bool


class VmcardioClient:
    def __init__(self, config: VmcardioConfig, *, http: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(
                config.read_timeout_s, connect=config.connect_timeout_s, pool=config.read_timeout_s
            ),
            follow_redirects=False,
            trust_env=False,
        )
        self._token: tuple[str, float] | None = None  # (token, monotonic refresh-at)
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()

    # ---- token
    async def token(self, *, force: bool = False) -> str:
        async with self._lock:
            hit = self._token
            if hit is not None and not force and time.monotonic() < hit[1]:
                return hit[0]
            self._token = None
            url = self.config.base_url.rstrip("/") + "/v2/getAccessToken"
            try:
                resp = await self._http.get(
                    url,
                    params={"app_id": self.config.app_id, "app_secret": self.config.app_secret},
                    headers={"Accept": "application/json", "User-Agent": "procurement-client/1"},
                )
            except httpx.HTTPError as exc:
                log.warning("vmcardio auth transport_error=%s", type(exc).__name__)
                raise SupplierError(
                    "supplier unreachable", outcome=Outcome.RETRYABLE, error_code="connect_error"
                ) from None
            parsed = _parse(resp)
            data = parsed.data
            tok = data.get("token") if isinstance(data, dict) else None
            if resp.status_code == 200 and parsed.code == 0 and isinstance(tok, str) and tok:
                assert isinstance(data, dict)
                self._token = (tok, time.monotonic() + _ttl(data.get("expired_time")))
                return tok
            log.warning("vmcardio auth status=%d code=%s", resp.status_code, parsed.code)
            if parsed.code in _STOP_CODES:
                raise SupplierError(
                    "supplier credentials refused",
                    outcome=Outcome.FATAL,
                    status=resp.status_code,
                    error_code=f"vm_{parsed.code}",
                    supplier_stop=True,
                )
            raise SupplierError(
                "supplier token error",
                outcome=Outcome.RETRYABLE,
                status=resp.status_code,
                error_code=(
                    f"vm_{parsed.code}" if parsed.code is not None else f"http_{resp.status_code}"
                ),
            )

    # ---- calls
    async def call(
        self,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        request_id: str | None = None,
        creates: bool = False,
    ) -> VmcardioResponse:
        """One authenticated POST. ``request_id`` makes a write deduplicated forever. ``creates``:
        the write that makes a card (a lost answer is ambiguous). Reads and the other writes
        (refund, delete) map an unclear answer to RETRYABLE."""
        if not _PATH_RE.fullmatch(path):
            raise ValueError("invalid API path")
        if request_id is not None and not REQUEST_ID_RE.fullmatch(request_id):
            raise ValueError("invalid request id")
        if creates and self.config.production_target and not self.config.allow_production_writes:
            log.warning("vmcardio refused production write %s", path)
            raise ProductionWriteBlocked()
        resp = parsed = None
        for attempt in (0, 1):
            tok = await self.token(force=attempt == 1)
            resp = await self._send(path, body, tok, request_id, creates)
            parsed = _parse(resp)
            if parsed.code != BAD_TOKEN or attempt == 1:
                break  # (a bad token never entered business processing: one retry with a new one)
        assert resp is not None and parsed is not None
        return self._finish(path, resp, parsed, creates)

    async def _send(
        self,
        path: str,
        body: Mapping[str, Any] | None,
        token: str,
        request_id: str | None,
        creates: bool,
    ) -> httpx.Response:
        headers = {
            "Authorization": token,
            "Accept": "application/json",
            "User-Agent": "procurement-client/1",
        }
        if request_id:
            headers["X-Client-Request-Id"] = request_id
        url = self.config.base_url.rstrip("/") + path
        try:
            return await self._http.post(
                url,
                content=dumps_exact(dict(body or {})),
                headers={**headers, "Content-Type": "application/json"},
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            log.warning("vmcardio %s connect_error", path)
            raise SupplierError(
                "supplier unreachable", outcome=Outcome.RETRYABLE, error_code="connect_error"
            ) from None
        except httpx.HTTPError as exc:
            log.warning("vmcardio %s transport_error=%s", path, type(exc).__name__)
            raise SupplierError(
                "supplier timeout after send",
                outcome=Outcome.AMBIGUOUS if creates else Outcome.RETRYABLE,
                error_code="timeout_after_send",
            ) from None

    def _finish(
        self, path: str, resp: httpx.Response, parsed: _Parsed, creates: bool
    ) -> VmcardioResponse:
        if resp.status_code == 200 and parsed.code == 0:
            return VmcardioResponse(parsed.data)
        status, code = resp.status_code, parsed.code
        tag = f"vm_{code}" if code is not None else f"http_{status}"
        stop = False
        if status == 429:
            out = Outcome.RETRYABLE
        elif parsed.duplicate:
            out, tag = Outcome.AMBIGUOUS, "duplicate_request"
        elif code in _STOP_CODES or code == BAD_TOKEN:
            out, stop = Outcome.FATAL, code in _STOP_CODES
        elif status >= 500 or (status == 200 and code is None):
            # a 5xx / an unreadable 200: a card creation may have run, anything else is repeated
            out = Outcome.AMBIGUOUS if creates else Outcome.RETRYABLE
        elif code is not None:
            # a business refusal: the vendor says no BEFORE any card exists (no charge on a failed
            # opening, FAQ 1.2); for a read or a refund it is simply a failure
            out = Outcome.UNAVAILABLE if creates else Outcome.FATAL
        else:
            out = Outcome.FATAL
        log.warning("vmcardio %s status=%d code=%s outcome=%s", path, status, code, out.value)
        raise SupplierError(
            "supplier error", outcome=out, status=status, error_code=tag, supplier_stop=stop
        )


def dumps_exact(body: Mapping[str, Any]) -> bytes:
    """JSON with every ``Decimal`` written as an exact number (``100`` / ``12.34``), never through
    a float. The vendor's amount fields are JSON numbers. A random per-call marker stands in for
    each Decimal while ``json.dumps`` runs and is then replaced by its digits."""
    marker = "~dec" + secrets.token_hex(8) + "~"
    seen: list[str] = []

    def default(o: Any) -> Any:
        if isinstance(o, Decimal) and o.is_finite():
            seen.append(format(o, "f"))
            return f"{marker}{len(seen) - 1}~"
        raise TypeError("not JSON serialisable")

    text = json.dumps(body, default=default, separators=(",", ":"))
    for i, digits in enumerate(seen):
        text = text.replace(f'"{marker}{i}~"', digits)
    return text.encode()


def _parse(resp: httpx.Response) -> _Parsed:
    body = _decode(resp)
    msg = body.get("msg") if isinstance(body, dict) else None
    code = body.get("code") if isinstance(body, dict) else None
    return _Parsed(
        code if isinstance(code, int) and not isinstance(code, bool) else None,
        body.get("data") if isinstance(body, dict) else None,
        isinstance(msg, str) and bool(DUPLICATE_RE.search(msg)),
    )


def _decode(resp: httpx.Response) -> Any:
    raw = resp.content
    if len(raw) > MAX_BODY:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


def _ttl(expired_time: Any) -> float:
    """Seconds until the token should be refreshed. ``expired_time`` is an epoch (s or ms) or a
    lifetime in seconds; anything unreadable counts as one hour."""
    if isinstance(expired_time, bool) or not isinstance(expired_time, int | float):
        life = float(DEFAULT_TOKEN_TTL_S)
    elif expired_time > 10**11:  # epoch milliseconds
        life = expired_time / 1000 - time.time()
    elif expired_time > 10**8:  # epoch seconds
        life = expired_time - time.time()
    else:
        life = float(expired_time)
    return max(5.0, life - TOKEN_MARGIN_S) if life > 2 * TOKEN_MARGIN_S else max(5.0, life / 2)
