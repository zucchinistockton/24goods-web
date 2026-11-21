"""BEpusdt admin API: the receiving wallets behind the crypto channels (owner 2026-11-21).

BEpusdt v1.24 keeps its wallets (``bep_wallet``: name, trade type, address, on / off) in its own
database and manages them through its admin API (verified against v1.24.3 on 2026-11-21):

* ``GET /<admin_secure>`` sets a ``session`` cookie (the admin entry path is secret);
* ``POST /api/auth/login {username, password}`` -> ``data.token`` and ``data.types`` (every trade
  type the build supports);
* then ``POST /api/wallet/list {page, size, sort}`` / ``add`` / ``mod`` / ``del`` with the RAW token
  in ``Authorization`` (no "Bearer").

Settings ``bepusdt_admin_user`` / ``bepusdt_admin_password`` / ``bepusdt_admin_entry`` (api env
only). The answers carry addresses only: no keys exist in BEpusdt (it watches public addresses).
"""

from __future__ import annotations

import time
from typing import Any

import httpx

TOKEN_TTL_S = 600.0
TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class BepusdtAdminError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.message = message[:200]


class BepusdtAdmin:
    def __init__(self, base_url: str, user: str, password: str, entry: str) -> None:
        self.base = base_url.rstrip("/")
        self.user, self.password, self.entry = user, password, entry.strip().strip("/")
        self._token: tuple[float, str, httpx.Cookies] | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base and self.user and self.password and self.entry)

    async def _login(self, c: httpx.AsyncClient) -> str:
        hit = self._token
        if hit is not None and time.monotonic() - hit[0] < TOKEN_TTL_S:
            c.cookies.update(hit[2])
            return hit[1]
        await c.get(f"{self.base}/{self.entry}")
        creds = {"username": self.user, "password": self.password}
        r = await c.post(f"{self.base}/api/auth/login", json=creds)
        data = _ok(r).get("data")
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise BepusdtAdminError("bepusdt_login_failed")
        self._token = (time.monotonic(), token, httpx.Cookies(c.cookies))
        return token

    async def call(self, path: str, body: dict[str, Any]) -> Any:
        if not self.configured:
            raise BepusdtAdminError("bepusdt_admin_off")
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as c:
                token = await self._login(c)
                url = f"{self.base}{path}"
                r = await c.post(url, json=body, headers={"Authorization": token})
                if r.status_code == 403:  # token expired: log in once more
                    self._token = None
                    token = await self._login(c)
                    r = await c.post(url, json=body, headers={"Authorization": token})
                return _ok(r).get("data")
        except httpx.HTTPError as exc:
            raise BepusdtAdminError("bepusdt_unreachable", type(exc).__name__) from None

    async def wallets(self) -> list[dict[str, Any]]:
        data = await self.call("/api/wallet/list", {"page": 1, "size": 100, "sort": "asc"})
        rows: Any = data.get("list", []) if isinstance(data, dict) else data
        rows = rows if isinstance(rows, list) else []
        return [_wallet(w) for w in rows if isinstance(w, dict)]

    async def add(self, *, name: str, trade_type: str, address: str, remark: str = "") -> None:
        await self.call(
            "/api/wallet/add",
            {
                "name": name,
                "trade_type": trade_type,
                "address": address,
                "remark": remark,
                "other_notify": 0,
            },
        )

    async def modify(self, row: dict[str, Any]) -> None:
        await self.call("/api/wallet/mod", row)

    async def delete(self, wallet_id: int) -> None:
        await self.call("/api/wallet/del", {"id": wallet_id})


def _ok(r: httpx.Response) -> dict[str, Any]:
    try:
        j = r.json()
    except ValueError:
        raise BepusdtAdminError("bepusdt_bad_answer", r.text[:120]) from None
    if not isinstance(j, dict):
        raise BepusdtAdminError("bepusdt_bad_answer")
    if r.status_code != 200 or j.get("code") not in (200, 0):
        msg = j.get("msg") or j.get("message") or r.status_code
        raise BepusdtAdminError("bepusdt_refused", str(msg))
    return j


def _wallet(w: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": w.get("id"),
        "name": str(w.get("name") or "")[:64],
        "trade_type": str(w.get("trade_type") or "")[:32],
        "address": str(w.get("address") or "")[:128],
        "enabled": w.get("status") == 1,
        "remark": str(w.get("remark") or "")[:128],
        "other_notify": w.get("other_notify") if isinstance(w.get("other_notify"), int) else 0,
        "updated_at": str(w.get("updated_at") or "")[:32],
    }
