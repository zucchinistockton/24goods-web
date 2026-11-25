"""ZPay (易支付-compatible) adapter on top of :mod:`app.payments.epay`.

* ``build_pay_url`` -> signed ``{gateway}/submit.php?...`` the browser navigates to.
* ``query_order``  -> server-to-server re-query
  ``GET {api}/api.php?act=order&pid&key&out_trade_no``.
  That URL contains the merchant key: it is never logged, and transport errors are
  re-raised without the URL (``from None``).
* ``verify_notify`` -> hardened notify verification (pinned MD5, pid, order, status, amount).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from app.payments import egress, epay
from app.payments.bounded import bounded_decimal, bounded_integer, response_bytes

log = logging.getLogger("app.payments.zpay")

CHANNEL = "zpay_alipay"
PAY_TYPE = "alipay"


# two-label public suffixes seen for payment providers: the registrable domain is then 3 labels
_TWO_LABEL_SUFFIXES = frozenset(
    {
        "com.cn",
        "net.cn",
        "org.cn",
        "gov.cn",
        "edu.cn",
        "com.hk",
        "com.tw",
        "co.jp",
        "co.uk",
        "com.au",
    }
)


def _site(gateway_host: str) -> str:
    """The gateway's registrable domain (``api.snpay.cn`` -> ``snpay.cn``): a cashier link on the
    provider's own site (``www.snpay.cn``) is the provider's, a link anywhere else is refused."""
    labels = gateway_host.lower().rstrip(".").split(".")
    if len(labels) < 2 or not all(labels):
        return ""
    n = 3 if ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES else 2
    return ".".join(labels[-n:]) if len(labels) >= n else ""


class ZPayQueryError(Exception):
    """Re-query failed (transport / protocol). Message never contains the key or URL."""


class ZPayUnreachable(ZPayQueryError):
    """The request provably never reached the gateway (DNS failure, connect error/timeout).

    Distinct from a read timeout or a protocol error, after which the gateway may have created
    the order. The original exception stays reachable via ``__cause__``."""


class ZPayRefused(ZPayQueryError):
    """``mapi.php`` answered with a well-formed JSON ``code != 1``: the gateway explicitly declined
    to create the payment, so no order exists there and another gateway may be tried.
    ``gateway_message`` is truncated and has the merchant key redacted."""

    def __init__(self, gateway_message: str = "") -> None:
        super().__init__("gateway refused payment")
        self.gateway_message = gateway_message


def _never_dispatched(exc: BaseException) -> bool:
    from app.core import safefetch

    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True
    return (
        isinstance(exc, safefetch.FetchError)
        and not isinstance(exc, safefetch.UnsafeURL)
        and (isinstance(exc.__cause__, OSError) or exc.message == "host could not be resolved")
    )


@dataclass(frozen=True)
class ZPayConfig:
    pid: str
    key: str
    gateway: str  # browser-facing base, e.g. https://zpayz.cn
    api_base: str  # server-to-server base for api.php
    notify_url: str
    return_url: str
    safe_network: bool = False
    instance_id: str = ""


@dataclass(frozen=True)
class QueryResult:
    found: bool
    paid: bool
    money: Decimal | None
    trade_no: str
    out_trade_no: str


def _first_key(data: Mapping[str, Any], *names: str) -> Any:
    for n in names:
        if n in data and data[n] not in (None, ""):
            return data[n]
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_query_response(data: Any) -> QueryResult:
    """Tolerant parser: ``code``/``status`` may be int or str; money may be str or number."""
    if not isinstance(data, Mapping):
        raise ZPayQueryError("unexpected query response shape")
    code = _as_int(data.get("code"))
    if code != 1:
        message = str(data.get("msg", "")).strip().lower().rstrip(".!！。")
        # 订单不存在 = Xiaodou; 订单号不存在 = snpay (seen on a read-only query 2026-11-25)
        if code == -1 and message in {"order not found", "not found", "订单不存在", "订单号不存在"}:
            return QueryResult(False, False, None, "", str(data.get("out_trade_no") or ""))
        raise ZPayQueryError("query response did not confirm an order status")
    status = _as_int(_first_key(data, "status", "trade_status"))
    if status is None and str(data.get("trade_status", "")).upper() == "TRADE_SUCCESS":
        status = 1
    if status not in (0, 1):
        raise ZPayQueryError("query returned an unknown payment status")
    money_raw = _first_key(data, "money", "total_amount")
    money: Decimal | None
    try:
        money = bounded_decimal(str(money_raw)) if money_raw is not None else None
    except (InvalidOperation, ValueError):
        money = None
    if money is not None and not money.is_finite():
        money = None
    return QueryResult(
        found=True,
        paid=status == 1,
        money=money,
        trade_no=str(_first_key(data, "trade_no", "api_trade_no") or ""),
        out_trade_no=str(data.get("out_trade_no") or ""),
    )


QUERY_TIMEOUT_S = 6.0
# creating a payment may try the direct path and then a configured relay (app/payments/egress.py);
# connecting gets CREATE_CONNECT_S of it so the outer timeout never cuts a connect (which would
# turn "provably not sent" into "unknown"). Queries: QUERY_CONNECT_S of their 6 s; probe: 3.5 of 5.
# 2026-11-22: with the direct route to Xiaodou blocked, a query's old 1.8 s left the relay 0.4 s
# for TLS, but the relay's handshake (it dials Guangzhou first) takes about 0.5 s: every status
# query failed ("relay path not reachable") although creating payments through the relay worked.
QUERY_CONNECT_S = 4.0
# 2026-11-25: snpay answered /mapi.php after ~9 s; a 6 s read gave up, left an "unknown" attempt
# and blocked the order. Creates get a longer READ budget (queries stay at 6 s). Callers: the
# failover loop may try two gateways in a row (2 x 16 s) which stays under Caddy's 45 s
# response_header_timeout; the buyer app sets no request timeout.
CREATE_TIMEOUT_S = 16.0
CREATE_CONNECT_S = 4.5
CREATE_READ_S = 15.0
# Xiaodou answers an unknown out_trade_no with HTTP 404 + {"code":-1,"msg":"订单不存在"}
# and a bad signature with 400 + JSON. Parse those bodies (parse_query_response / create_payment
# decide what they mean; nothing here can mark a payment paid) instead of treating them as
# transport failures.
_JSON_ERROR_STATUSES = frozenset({400, 404})
# 2026-11-23: Xiaodou throttles order creation with HTTP 429 + {"code":-1,"msg":"请求过于频繁..."}.
# For a CREATE that is a clean refusal (the buyer can pick another method) instead of an "unknown"
# attempt that blocks the order. Never for queries: a throttled query must not read as "not found".
_CREATE_ERROR_STATUSES = _JSON_ERROR_STATUSES | {429}


def _error_status_guard(status: int, data: Any) -> Any:
    """A 4xx body may only ever say "no": a success code on an error status is refused (it
    could otherwise read as paid in reconcile/cancel, or hand out a pay URL)."""
    if status >= 400 and isinstance(data, Mapping) and _as_int(data.get("code")) == 1:
        raise ValueError("success code on an HTTP error status")
    return data


# connect fails first (before the overall wait_for) so a dead host is told apart from a slow one
_TIMEOUT = httpx.Timeout(QUERY_TIMEOUT_S, connect=QUERY_TIMEOUT_S * 0.7)


class ZPayClient:
    def __init__(self, config: ZPayConfig, http: httpx.AsyncClient):
        self.config = config
        self._http = http

    def pay_params(self, order_no: str, name: str, amount: Decimal) -> dict[str, str]:
        c = self.config
        params = {
            "pid": c.pid,
            "type": PAY_TYPE,
            "out_trade_no": order_no,
            "notify_url": c.notify_url,
            "return_url": c.return_url,
            "name": name[:60],
            "money": f"{amount:.2f}",
        }
        params["sign"] = epay.sign(params, c.key)
        params["sign_type"] = "MD5"
        return params

    def build_pay_url(self, order_no: str, name: str, amount: Decimal) -> str:
        base = self.config.gateway.rstrip("/")
        return f"{base}/submit.php?{urlencode(self.pay_params(order_no, name, amount))}"

    def verify_notify(self, params: Mapping[str, str], expected: epay.ExpectedOrder) -> str:
        return epay.verify_notify(params, self.config.key, expected)

    def verify_signature(self, params: Mapping[str, str]) -> bool:
        return epay.verify_signature(params, self.config.key)

    async def _json(
        self,
        endpoint: str,
        params: dict[str, str],
        *,
        post: bool = False,
        connect_s: float = 1.8,
        body_statuses: frozenset[int] = _JSON_ERROR_STATUSES,
        read_s: float = QUERY_TIMEOUT_S,
    ) -> Any:
        """Bounded transport. Database-configured gateways use DNS-pinned public HTTPS only."""
        from app.core import safefetch

        url = self.config.api_base.rstrip("/") + endpoint
        try:
            if self.config.safe_network:
                parts = urlsplit(url)
                if parts.scheme != "https":
                    raise ZPayQueryError("HTTPS gateway required")
                port_ok = (parts.port or 443) in safefetch.ALLOWED_PORTS
                if parts.username or parts.password or not port_ok:
                    raise ZPayQueryError("gateway URL not allowed")  # re-checked on every call
                host = (parts.hostname or "").rstrip(".")

                async def fresh_ip() -> str:
                    return (await safefetch.vet_url(url)).ip  # resolves + vets (public IP only)

                async def exchange(pinned: str, connect: float | None) -> Any:
                    timeout = (
                        _TIMEOUT
                        if read_s == QUERY_TIMEOUT_S
                        else httpx.Timeout(read_s, connect=QUERY_TIMEOUT_S * 0.7)
                    )
                    if connect is not None:
                        timeout = httpx.Timeout(read_s, connect=connect)
                    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                        async with client.stream(
                            "POST" if post else "GET",
                            pinned,
                            data=params if post else None,
                            params=None if post else params,
                            headers={"Host": host, "Accept-Encoding": "identity"},
                            extensions={"sni_hostname": host},
                            timeout=timeout,
                        ) as response:
                            raw = await response_bytes(
                                response, 256 * 1024, body_statuses=body_statuses
                            )
                            return _error_status_guard(
                                response.status_code,
                                json.loads(
                                    raw, parse_float=bounded_decimal, parse_int=bounded_integer
                                ),
                            )

                # direct first, configured relay only if the direct connection cannot be opened
                return await egress.send(
                    scheme="https",
                    host=host,
                    port=parts.port or 443,
                    path=parts.path or "/",
                    fresh_ip=fresh_ip,
                    exchange=exchange,
                    never_dispatched=_never_dispatched,
                    connect_deadline_s=connect_s,
                )
            async with self._http.stream(
                "POST" if post else "GET",
                url,
                data=params if post else None,
                params=None if post else params,
                headers={"Accept-Encoding": "identity"},
                timeout=_TIMEOUT
                if read_s == QUERY_TIMEOUT_S
                else httpx.Timeout(read_s, connect=QUERY_TIMEOUT_S * 0.7),
            ) as response:
                raw = await response_bytes(response, 256 * 1024, body_statuses=body_statuses)
                return _error_status_guard(
                    response.status_code,
                    json.loads(raw, parse_float=bounded_decimal, parse_int=bounded_integer),
                )
        except (httpx.HTTPError, ValueError, safefetch.FetchError) as exc:
            if _never_dispatched(exc):
                raise ZPayUnreachable("gateway unreachable") from exc
            # what went wrong, never what was sent (params carry the key)
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            log.warning(
                "zpay %s failed after sending: %s status=%s", endpoint, type(exc).__name__, status
            )
            raise ZPayQueryError("gateway request failed") from None

    async def create_payment(
        self, order_no: str, name: str, amount: Decimal, client_ip: str
    ) -> dict[str, str]:
        params = self.pay_params(order_no, name, amount)
        params["clientip"] = client_ip
        params["device"] = "pc"
        params["sign"] = epay.sign(params, self.config.key)
        data = await asyncio.wait_for(
            self._json(
                "/mapi.php",
                params,
                post=True,
                connect_s=CREATE_CONNECT_S,
                body_statuses=_CREATE_ERROR_STATUSES,
                read_s=CREATE_READ_S,
            ),
            CREATE_TIMEOUT_S,
        )
        if not isinstance(data, dict) or "code" not in data:
            raise ZPayQueryError("gateway refused payment")
        code = _as_int(data.get("code"))
        if code is None:  # non-numeric / boolean / fractional: not a well-formed refusal
            raise ZPayQueryError("gateway refused payment")
        if code != 1:
            msg = str(data.get("msg") or "")[:200]
            if self.config.key:
                msg = msg.replace(self.config.key, "***")
            log.warning("zpay create refused: code=%s msg=%r", code, msg[:120])
            raise ZPayRefused(msg)
        qr, redirect = str(data.get("qrcode") or ""), str(data.get("payurl") or "")
        value, kind = (qr, "qr") if qr else (redirect, "redirect")
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        site = _site(urlsplit(self.config.gateway).hostname or "")
        approved = {
            urlsplit(self.config.gateway).hostname,
            "qr.alipay.com",
            "www.alipay.com",
            "pay.weixin.qq.com",
        }
        if site and (host == site or host.endswith("." + site)):
            # 2026-11-25 snpay: the API host is api.snpay.cn, the cashier link www.snpay.cn
            approved.add(host)
        if (
            not value
            or len(value) > 2048
            or any(ord(c) < 33 for c in value)
            or parsed.username
            or parsed.password
            or parsed.fragment
            or not (
                (parsed.scheme == "https" and host in approved)
                or (kind == "qr" and parsed.scheme in ("weixin", "alipays"))
            )
        ):
            raise ZPayQueryError("unsupported payment presentation")
        return {"kind": kind, "payload": value, "trade_no": str(data.get("trade_no") or "")[:128]}

    async def probe_order(self, order_no: str) -> dict[str, str]:
        """Admin self-test: a signed order query; returns the gateway's code/msg verbatim.

        Never echoes the key (it is redacted from the message) and never raises for a gateway
        answer, only for transport problems (ZPayUnreachable / ZPayQueryError)."""
        c = self.config
        query = {"act": "order", "pid": c.pid, "key": c.key, "out_trade_no": order_no}
        try:
            data = await asyncio.wait_for(self._json("/api.php", query, connect_s=3.5), timeout=5.0)
        except asyncio.TimeoutError:
            raise ZPayQueryError("query timed out") from None
        if not isinstance(data, dict):
            raise ZPayQueryError("unexpected response")
        msg = str(data.get("msg") or "")[:200]
        if c.key:
            msg = msg.replace(c.key, "***")
        return {"code": str(data.get("code", ""))[:16], "msg": msg}

    async def query_order(self, order_no: str) -> QueryResult:
        c = self.config
        query = {"act": "order", "pid": c.pid, "key": c.key, "out_trade_no": order_no}
        try:
            # ZPay waits 5 s for our reply: cap the whole re-query at ~3 s.
            data = await asyncio.wait_for(
                self._json("/api.php", query, connect_s=QUERY_CONNECT_S), timeout=QUERY_TIMEOUT_S
            )
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as exc:
            name = type(exc).__name__
            log.warning("zpay re-query failed order=%s error=%s", order_no, name)
            raise ZPayQueryError(f"query failed: {name}") from None
        if self.config.instance_id and isinstance(data, dict) and str(data.get("code")) == "1":
            if str(data.get("pid", "")) != self.config.pid or data.get("type") != PAY_TYPE:
                raise ZPayQueryError("query merchant or method mismatch")
        parsed = parse_query_response(data)
        if parsed.found and parsed.out_trade_no != order_no:
            raise ZPayQueryError("query order binding mismatch")
        return parsed
