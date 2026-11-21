"""Crypto payment networks and their receiving wallets (owner 2026-11-21: "add other USDT
addresses myself, as versatile as possible").

``GET /admin/payments/crypto`` lists every crypto channel of the catalogue (USDT / USDC on each
network BEpusdt supports) with its shop switch and the BEpusdt wallets. A platform admin adds,
edits, switches and removes receiving wallets here; the shop switch per network stays in the
payment settings (a network shows to buyers only when it is switched on AND has an enabled
wallet). Addresses are checked per network family before BEpusdt sees them. Every change is
audited. No 2FA (owner decision 2026-11-21); a staff session (not an API token) is required.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.deps import client_ip, get_db, get_res
from app.core.errors import NotFound, Unavailable, Validation
from app.core.resources import Resources
from app.models import User
from app.modules.admin.procurement_cards import platform_admin_session
from app.modules.content.site import tenant_settings
from app.modules.payments import channels as ch
from app.payments.bepusdt_admin import BepusdtAdmin, BepusdtAdminError

router = APIRouter(prefix="/admin/payments/crypto", tags=["admin"])

ADDRESS_RE: dict[str, re.Pattern[str]] = {
    "evm": re.compile(r"^0x[0-9a-fA-F]{40}$"),
    "tron": re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$"),
    "solana": re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"),
    "ton": re.compile(r"^((EQ|UQ|kQ|0Q)[A-Za-z0-9_-]{46}|-?[0-9]:[0-9a-fA-F]{64})$"),
    "aptos": re.compile(r"^0x[0-9a-fA-F]{1,64}$"),
}
_TEXT_RE = re.compile(r"[\x00-\x1f\x7f<>]")
_CLIENTS: dict[str, BepusdtAdmin] = {}


def _admin(res: Resources) -> BepusdtAdmin:
    s = res.settings
    key = f"{s.bepusdt_url}|{s.bepusdt_admin_user}|{s.bepusdt_admin_entry}"
    client = _CLIENTS.get(key)
    if client is None:
        client = _CLIENTS[key] = BepusdtAdmin(
            s.bepusdt_url, s.bepusdt_admin_user, s.bepusdt_admin_password, s.bepusdt_admin_entry
        )
    return client


def _fail(exc: BepusdtAdminError) -> Exception:
    if exc.code == "bepusdt_refused":
        return Validation(f"BEpusdt refused: {exc.message}", code="bepusdt_refused")
    return Unavailable("BEpusdt is not reachable right now", code=exc.code)


def valid_address(trade_type: str, address: str) -> bool:
    family = ch.address_family(trade_type)
    return family is not None and bool(ADDRESS_RE[family].fullmatch(address))


@router.get("")
async def overview(
    admin: User = Depends(platform_admin_session),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict[str, Any]:
    settings = await tenant_settings(db, admin.tenant_id)
    client = _admin(res)
    wallets: list[dict[str, Any]] = []
    error: str | None = None
    if client.configured:
        try:
            wallets = await client.wallets()
        except BepusdtAdminError as exc:
            error = exc.code
    else:
        error = "bepusdt_admin_off"
    live = {w["trade_type"] for w in wallets if w["enabled"]}
    return {
        "channels": [
            {
                "id": cid,
                "trade_type": tt,
                "coin": cid[:4].upper(),
                "network": ch.network_of(cid),
                "family": ch.address_family(tt),
                "label": ch.LABELS[cid],
                "enabled": (cfg := ch.channel_cfg(settings, cid)).enabled,
                "surcharge_permille": cfg.permille,
                "has_wallet": tt in live,
            }
            for cid, tt in ch.USDT_CHANNELS.items()
        ],
        "wallets": wallets,
        "error": error,
    }


class WalletIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trade_type: str = Field(max_length=32)
    address: str = Field(min_length=20, max_length=128)
    name: str | None = Field(default=None, max_length=64)
    remark: str | None = Field(default=None, max_length=128)


class WalletPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool | None = None
    address: str | None = Field(default=None, min_length=20, max_length=128)
    name: str | None = Field(default=None, max_length=64)
    remark: str | None = Field(default=None, max_length=128)


def _clean(v: str | None) -> str:
    return _TEXT_RE.sub("", v or "").strip()


@router.post("/wallets")
async def add_wallet(
    body: WalletIn,
    request: Request,
    admin: User = Depends(platform_admin_session),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict[str, Any]:
    address = body.address.strip()
    if body.trade_type not in ch.USDT_CHANNELS.values():
        raise Validation("Unknown network", code="unknown_network")
    if not valid_address(body.trade_type, address):
        raise Validation("That address does not fit this network", code="bad_address")
    cid = body.trade_type.replace(".", "_")
    name = _clean(body.name) or ch.LABELS[cid]["en"]
    try:
        await _admin(res).add(
            name=name[:64], trade_type=body.trade_type, address=address, remark=_clean(body.remark)
        )
    except BepusdtAdminError as exc:
        raise _fail(exc) from None
    await audit(
        db,
        tenant_id=admin.tenant_id,
        action="payments.crypto_wallet_added",
        actor_id=admin.id,
        actor_role=admin.role,
        target=f"bepusdt_wallet:{body.trade_type}",
        after={"trade_type": body.trade_type, "address": address, "name": name},
        ip=client_ip(request),
    )
    await db.commit()
    return await overview(admin, db, res)


async def _row(res: Resources, wallet_id: int) -> dict[str, Any]:
    try:
        rows = await _admin(res).wallets()
    except BepusdtAdminError as exc:
        raise _fail(exc) from None
    row = next((w for w in rows if w["id"] == wallet_id), None)
    if row is None:
        raise NotFound("Wallet not found")
    return row


@router.patch("/wallets/{wallet_id}")
async def edit_wallet(
    wallet_id: int,
    body: WalletPatch,
    request: Request,
    admin: User = Depends(platform_admin_session),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict[str, Any]:
    row = await _row(res, wallet_id)
    new = dict(row)
    if body.address is not None:
        address = body.address.strip()
        if not valid_address(row["trade_type"], address):
            raise Validation("That address does not fit this network", code="bad_address")
        new["address"] = address
    if body.enabled is not None:
        new["enabled"] = body.enabled
    if body.name is not None:
        new["name"] = _clean(body.name)[:64] or row["name"]
    if body.remark is not None:
        new["remark"] = _clean(body.remark)
    payload = {
        "id": wallet_id,
        "name": new["name"],
        "trade_type": row["trade_type"],
        "address": new["address"],
        "status": 1 if new["enabled"] else 0,
        "remark": new["remark"],
        "other_notify": row["other_notify"],
    }
    try:
        await _admin(res).modify(payload)
    except BepusdtAdminError as exc:
        raise _fail(exc) from None
    keys = ("address", "enabled", "name", "remark")
    await audit(
        db,
        tenant_id=admin.tenant_id,
        action="payments.crypto_wallet_updated",
        actor_id=admin.id,
        actor_role=admin.role,
        target=f"bepusdt_wallet:{wallet_id}",
        before={k: row[k] for k in keys},
        after={k: new[k] for k in keys},
        ip=client_ip(request),
    )
    await db.commit()
    return await overview(admin, db, res)


@router.delete("/wallets/{wallet_id}")
async def delete_wallet(
    wallet_id: int,
    request: Request,
    admin: User = Depends(platform_admin_session),
    db: AsyncSession = Depends(get_db),
    res: Resources = Depends(get_res),
) -> dict[str, Any]:
    row = await _row(res, wallet_id)
    try:
        await _admin(res).delete(wallet_id)
    except BepusdtAdminError as exc:
        raise _fail(exc) from None
    await audit(
        db,
        tenant_id=admin.tenant_id,
        action="payments.crypto_wallet_removed",
        actor_id=admin.id,
        actor_role=admin.role,
        target=f"bepusdt_wallet:{wallet_id}",
        before={k: row[k] for k in ("trade_type", "address", "name", "enabled")},
        ip=client_ip(request),
    )
    await db.commit()
    return await overview(admin, db, res)
