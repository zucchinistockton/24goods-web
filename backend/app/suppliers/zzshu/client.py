"""card.zzshu.pro third-party API client (ADR-054): auth, envelope, bounded reads, error mapping.

* Auth: ``X-API-Key``. Base ``https://card.zzshu.pro/api/v1/third-party``.
* Envelope ``{code, message, data}``; HTTP 2xx and ``code == 0`` is success, anything else fails.
* The vendor has NO idempotency key: a create whose answer is lost after sending is AMBIGUOUS and
  must never be resent (the caller enforces that).
* Only the error ``code`` (digits) is ever kept from a failure; bodies are never logged. A reply
  can carry the full card number and the buyer's session (the status reply does): the caller
  parses the few fields it needs and drops the rest at once.
* Timeouts 5 s connect / 30 s read, no redirects, no ambient proxy, response capped at 1 MiB.
* Production guard like ZovoCard: a write to a production target needs the explicit opt-in.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx

from app.suppliers.base import Outcome, SupplierError
from app.suppliers.zovocard.client import ProductionWriteBlocked, _simulator_host

log = logging.getLogger("app.suppliers.zzshu")

PRODUCTION_BASE_URL = "https://card.zzshu.pro/api/v1/third-party"
MAX_BODY = 1024 * 1024
_PATH_RE = re.compile(r"^/[A-Za-z0-9/_.-]{1,100}$")

# definite refusals: nothing was created, another source may be tried
_REFUSED = {40020: "region_not_offered", 40305: "maintenance", 40306: "points_exhausted"}
QUEUE_FULL = 42902  # nothing created; retry later
NOT_REACHED = 50300  # did not reach the vendor's backend; nothing created


@dataclass(frozen=True)
class ZzshuConfig:
    base_url: str
    api_key: str = field(repr=False)
    env: str = "production"
    allow_production_writes: bool = False
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 30.0

    def __post_init__(self) -> None:
        if self.env not in ("sandbox", "production"):
            raise ValueError("env must be sandbox or production")
        if not self.base_url.startswith(("https://", "http://")):
            raise ValueError("base_url must be an http(s) URL")
        if not self.api_key:
            raise ValueError("api_key is required")

    @property
    def production_target(self) -> bool:
        return self.env == "production" or not _simulator_host(self.base_url)


@dataclass(frozen=True)
class ZzshuResponse:
    status: int
    data: Any = field(repr=False)


class ZzshuClient:
    def __init__(self, config: ZzshuConfig, *, http: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(
                config.read_timeout_s, connect=config.connect_timeout_s, pool=config.read_timeout_s
            ),
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()

    async def get(self, path: str) -> ZzshuResponse:
        return await self.request("GET", path)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        creates: bool = False,
    ) -> ZzshuResponse:
        """``creates``: the POST that makes an order (a lost answer is ambiguous). The status POST
        is a read: a timeout is simply repeated."""
        method = method.upper()
        if not _PATH_RE.fullmatch(path) or ".." in path:
            raise ValueError("invalid API path")
        if creates and self.config.production_target and not self.config.allow_production_writes:
            log.warning("zzshu refused production write %s", path)
            raise ProductionWriteBlocked()
        headers = {
            "X-API-Key": self.config.api_key,
            "Accept": "application/json",
            "User-Agent": "procurement-client/1",
        }
        url = self.config.base_url.rstrip("/") + path
        try:
            resp = await self._http.request(method, url, json=json_body, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            log.warning("zzshu %s connect_error", path)
            raise SupplierError(
                "supplier unreachable", outcome=Outcome.RETRYABLE, error_code="connect_error"
            ) from None
        except httpx.HTTPError as exc:
            log.warning("zzshu %s transport_error=%s", path, type(exc).__name__)
            raise SupplierError(
                "supplier timeout after send",
                outcome=Outcome.AMBIGUOUS if creates else Outcome.RETRYABLE,
                error_code="timeout_after_send",
            ) from None
        body = _decode(resp)
        code = body.get("code") if isinstance(body, dict) else None
        if 200 <= resp.status_code < 300 and isinstance(body, dict) and code == 0:
            return ZzshuResponse(resp.status_code, body.get("data"))
        raise _fail(path, resp, code, creates)


def _decode(resp: httpx.Response) -> Any:
    raw = resp.content
    if len(raw) > MAX_BODY:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


def _retry_after(resp: httpx.Response) -> timedelta | None:
    v = resp.headers.get("retry-after", "")
    return timedelta(seconds=min(int(v), 900)) if v.isdigit() else None


def _fail(path: str, resp: httpx.Response, code: Any, creates: bool) -> SupplierError:
    status = resp.status_code
    num = code if isinstance(code, int) and not isinstance(code, bool) else None
    tag = f"zz_{num}" if num is not None else None
    if status == 429 or num == QUEUE_FULL:
        out, stop = Outcome.RETRYABLE, False
    elif status == 502 and num == NOT_REACHED:
        out, stop = Outcome.RETRYABLE, False
    elif num in _REFUSED:
        out, stop = Outcome.UNAVAILABLE, False
        tag = _REFUSED[num]
    elif status in (401, 403) and num in (40106, 40107):
        out, stop = Outcome.FATAL, True  # the key is wrong: open the supplier breaker
    elif status >= 500 or (200 <= status < 300):
        # a 5xx / an unreadable 2xx: a create may have been executed, a read is repeated
        out, stop = (Outcome.AMBIGUOUS if creates else Outcome.RETRYABLE), False
        tag = tag or f"http_{status}"
    else:
        out, stop = Outcome.FATAL, False
    log.warning("zzshu %s status=%d code=%s outcome=%s", path, status, num, out.value)
    return SupplierError(
        "supplier error",
        outcome=out,
        status=status,
        error_code=tag,
        supplier_stop=stop,
        retry_after=_retry_after(resp) if out is Outcome.RETRYABLE else None,
    )
