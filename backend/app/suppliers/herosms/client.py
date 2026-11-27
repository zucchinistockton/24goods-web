"""HeroSMS API client (ADR-057; fields verified against live answers 2026-11-27).

* REST ``{base}/api/v1`` with ``Authorization: ApiKey <key>``; failures are ``{"title", "details"}``
  and the machine code is ``title`` (``NO_NUMBERS``, ``NO_BALANCE``, ``EARLY_CANCEL_DENIED`` ...).
* The balance lives on the SMS-Activate compatible ``/stubs/handler_api.php?action=getBalance``
  (``ACCESS_BALANCE:10.73``); the key travels as a query parameter there, so that URL is never
  logged.
* Only machine codes are kept from a failure; bodies, phone numbers and codes are never logged.
* Timeouts 10 s connect / 25 s read, no redirects, no ambient proxy, response capped at 1 MiB.
* Production guard: buy / cancel / finish need ``allow_production_writes`` (like every supplier).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.suppliers.base import Outcome, SupplierError, clean_detail
from app.suppliers.zovocard.client import ProductionWriteBlocked

log = logging.getLogger("app.suppliers.herosms")

PRODUCTION_BASE_URL = "https://hero-sms.com"
MAX_BODY = 1024 * 1024
PAGE = 25  # the API's maximum page size
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,40}$")
# finished activation statuses (ActivationHistoryStatus): done / cancelled / refunded
FINISHED_STATUS = frozenset({6, 8, 10})


class SmsError(SupplierError):
    """A HeroSMS call failed; ``error_code`` is the provider's machine code or ``http_<n>``."""


def money(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() and d >= 0 else None


def _ts(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Activation:
    """One rented number as the provider reports it. ``codes`` are plaintext: the caller seals
    the first one at once and never stores or logs them."""

    upstream_id: str
    phone: str
    country: int | None
    operator: str
    price: Decimal | None
    status: int | None
    expires_at: datetime | None
    codes: tuple[str, ...] = field(default=(), repr=False)

    @property
    def finished(self) -> bool:
        return self.status in FINISHED_STATUS


def parse_activation(item: dict[str, Any]) -> Activation | None:
    ext = item.get("id")
    phone = re.sub(r"\D", "", str(item.get("phone") or ""))
    if ext is None or not phone:
        return None
    codes: list[str] = []
    for o in item.get("otpList") or []:
        if isinstance(o, dict):
            code = str(o.get("smsCode") or o.get("code") or "").strip()
            if code:
                codes.append(code[:64])
    country = item.get("country")
    status = item.get("status")
    return Activation(
        upstream_id=str(ext)[:64],
        phone=phone[:20],
        country=country if isinstance(country, int) else None,
        operator=clean_detail(item.get("operator") or "", 48) if item.get("operator") else "",
        price=money(item.get("price") if item.get("price") is not None else item.get("cost")),
        status=status if isinstance(status, int) else None,
        expires_at=_ts(item.get("expiredAt")),
        codes=tuple(codes),
    )


@dataclass(frozen=True)
class HeroSmsConfig:
    base_url: str
    api_key: str = field(repr=False)
    env: str = "production"
    allow_production_writes: bool = False
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 25.0

    def __post_init__(self) -> None:
        if self.env not in ("sandbox", "production"):
            raise ValueError("env must be sandbox or production")
        if not self.base_url.startswith(("https://", "http://")):
            raise ValueError("base_url must be an http(s) URL")
        if not self.api_key:
            raise ValueError("api_key is required")


class HeroSmsClient:
    def __init__(self, cfg: HeroSmsConfig, http: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self._own = http is None
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.read_timeout_s, connect=cfg.connect_timeout_s),
            follow_redirects=False,
            trust_env=False,
        )
        self.base = cfg.base_url.rstrip("/")

    async def aclose(self) -> None:
        if self._own:
            await self._http.aclose()

    # ------------------------------------------------------------------ transport
    def _guard_write(self) -> None:
        if self.cfg.env == "production" and not self.cfg.allow_production_writes:
            raise ProductionWriteBlocked()

    async def _rest(
        self, method: str, path: str, *, params: Any = None, body: dict | None = None
    ) -> Any:
        write = method != "GET"
        if write:
            self._guard_write()
        try:
            r = await self._http.request(
                method,
                f"{self.base}/api/v1{path}",
                params=params,
                json=body,
                headers={
                    "Authorization": f"ApiKey {self.cfg.api_key}",
                    "Accept": "application/json",
                },
            )
        except httpx.TimeoutException as exc:
            # a write may have happened: the caller reconciles through the open list
            raise SmsError(
                "timeout",
                outcome=Outcome.AMBIGUOUS if write else Outcome.RETRYABLE,
                error_code="timeout",
            ) from exc
        except httpx.HTTPError as exc:
            raise SmsError("network", outcome=Outcome.RETRYABLE, error_code="network") from exc
        if len(r.content) > MAX_BODY:
            raise SmsError("body too large", outcome=Outcome.RETRYABLE, error_code="too_large")
        if r.status_code == 204:
            return None
        try:
            j = r.json()
        except (ValueError, json.JSONDecodeError):
            j = None
        if r.is_success:
            return j
        code = None
        if isinstance(j, dict):
            title = str(j.get("title") or "")
            code = title if _CODE_RE.fullmatch(title) else None
        code = code or f"http_{r.status_code}"
        log.info("herosms %s %s -> %s %s", method, path.split("?")[0][:40], r.status_code, code)
        if r.status_code in (401, 403):
            outcome = Outcome.FATAL
        elif r.status_code >= 500 or r.status_code == 429:
            outcome = Outcome.AMBIGUOUS if write else Outcome.RETRYABLE
        else:
            outcome = Outcome.UNAVAILABLE
        raise SmsError(code, outcome=outcome, status=r.status_code, error_code=code)

    # ------------------------------------------------------------------ reads
    async def balance(self) -> Decimal:
        try:
            r = await self._http.get(
                f"{self.base}/stubs/handler_api.php",
                params={"api_key": self.cfg.api_key, "action": "getBalance"},
            )
        except httpx.HTTPError as exc:
            raise SmsError("network", outcome=Outcome.RETRYABLE, error_code="network") from exc
        text = r.text.strip()[:200]
        word, _, rest = text.partition(":")
        if word == "ACCESS_BALANCE":
            v = money(rest)
            if v is not None:
                return v
        code = word if _CODE_RE.fullmatch(word) else f"http_{r.status_code}"
        outcome = (
            Outcome.FATAL if code in ("BAD_KEY", "http_401", "http_403") else Outcome.RETRYABLE
        )
        raise SmsError(code, outcome=outcome, status=r.status_code, error_code=code)

    async def offers(
        self, service: str, countries: list[int]
    ) -> dict[int, tuple[Decimal | None, int]]:
        """``{country: (lowest price, listed count)}``. Listed counts are optimistic (a buy can
        still answer NO_NUMBERS), so they only rule a route OUT, never in."""
        params = {"services": service, "countries": ",".join(str(c) for c in countries)}
        j = await self._rest("GET", "/activations/offers/sms", params=params)
        data = (j or {}).get("data") if isinstance(j, dict) else None
        rows = (data or {}).get(service) if isinstance(data, dict) else None
        out: dict[int, tuple[Decimal | None, int]] = {}
        for cid, o in (rows or {}).items():
            if not isinstance(o, dict) or not str(cid).isdigit():
                continue
            prices = o.get("prices") or {}
            counts = o.get("counts") or {}
            price = money(prices.get("min")) or money(prices.get("retail"))
            try:
                count = int(counts.get("total") or 0)
            except (TypeError, ValueError):
                count = 0
            out[int(cid)] = (price, max(0, count))
        return out

    async def open_activations(self) -> list[Activation]:
        """Every activation the provider still has open (with its codes)."""
        out: list[Activation] = []
        for page in range(1, 9):
            j = await self._rest("GET", "/activations", params={"size": PAGE, "page": page})
            if not isinstance(j, dict):
                break
            for item in j.get("data") or []:
                if isinstance(item, dict):
                    a = parse_activation(item)
                    if a is not None:
                        out.append(a)
            if not (j.get("meta") or {}).get("hasMore"):
                break
        return out

    async def codes(self, upstream_id: str) -> tuple[str, ...]:
        """The codes of one activation (also after it left the open list)."""
        j = await self._rest("GET", f"/activations/{_ext(upstream_id)}/otp", params={"size": PAGE})
        out: list[str] = []
        for o in ((j or {}).get("data") if isinstance(j, dict) else None) or []:
            if isinstance(o, dict):
                code = str(o.get("smsCode") or o.get("code") or "").strip()
                if code:
                    out.append(code[:64])
        return tuple(out)

    # ------------------------------------------------------------------ writes
    async def buy(
        self, *, service: str, country: int, max_price: Decimal, operator: str | None = None
    ) -> Activation:
        body: dict[str, Any] = {
            "service": service,
            "country": country,
            "amount": 1,
            "maxPrice": float(max_price),  # JSON number; the cap is quantized to 4 places
            "verificationType": "sms",
        }
        if operator:
            body["operator"] = operator
        j = await self._rest("POST", "/activations", body=body)
        data = j.get("data") if isinstance(j, dict) and "data" in j else j
        item = data[0] if isinstance(data, list) and data else data
        a = parse_activation(item) if isinstance(item, dict) else None
        if a is None:
            raise SmsError("bad buy answer", outcome=Outcome.AMBIGUOUS, error_code="bad_response")
        return a

    async def cancel(self, upstream_id: str) -> None:
        await self._rest("DELETE", f"/activations/{_ext(upstream_id)}")

    async def finish(self, upstream_id: str) -> None:
        await self._rest("POST", f"/activations/{_ext(upstream_id)}/finish")


def _ext(upstream_id: str) -> str:
    if not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", upstream_id):
        raise SmsError("bad id", outcome=Outcome.FATAL, error_code="bad_id")
    return upstream_id
