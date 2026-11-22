"""Account pre-check BEFORE payment for every direct order (review-v12 §24): procurement side.

The API validated the buyer's session, sealed it to the procurement public key with the AAD
``preflight:<nonce>`` (a namespace no order or half-price check uses) and sends the sealed blob
here in the request body; nothing is stored. This process opens it in memory for ONE vendor
preflight (read-only upstream) against the target's primary direct source and answers a coarse
outcome: ``passed`` / ``blocked`` (with a reason key) / ``unavailable``. The session, the vendor's
free text and the account's other data never leave this function; only the masked account and
the vendor's plan name come back (the buyer pasted that very account a moment ago).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.core.db import tenant_session
from app.models import SupplierAccount, SupplierOffer, Tenant
from app.procurement import service
from app.procurement.context import ProcurementContext
from app.suppliers.base import SupplierError
from app.suppliers.direct_credential import CredentialError, validate_credential
from app.suppliers.registry import SupplierConfigError

log = logging.getLogger("app.procurement.preflight")

AAD_PREFIX = "preflight:"
BLOCK_REASONS = frozenset(
    {"session_invalid", "plan_active", "purchase_not_yet", "account_blocked", "upgrade_unavailable"}
)


@dataclass(frozen=True)
class Outcome:
    status: str  # passed | blocked | unavailable
    reason: str = ""
    account_mask: str | None = None
    current_plan: str | None = None
    can_purchase_at: str | None = None
    upgrade: dict[str, Any] | None = None

    def view(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "account": self.account_mask,
            "current_plan": self.current_plan,
            "can_purchase_at": self.can_purchase_at,
            "upgrade": self.upgrade,
        }


async def _source(
    ctx: ProcurementContext,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    variant_id: uuid.UUID | None,
) -> tuple[service.SupplierRef, dict[str, Any]] | None:
    """The target's primary enabled direct source on an enabled ZovoCard supplier (the source the
    engine tries first), platform tenant only (a reseller never sells direct)."""
    async with tenant_session(ctx.maker, tenant_id) as db:
        match = (
            SupplierOffer.variant_id == variant_id
            if variant_id is not None
            else SupplierOffer.variant_id.is_(None)
        )
        row = (
            await db.execute(
                select(SupplierOffer.params, SupplierAccount)
                .join(SupplierAccount, SupplierAccount.id == SupplierOffer.supplier_id)
                .where(
                    SupplierOffer.tenant_id == tenant_id,
                    SupplierOffer.product_id == product_id,
                    match,
                    SupplierOffer.enabled.is_(True),
                    SupplierOffer.kind == "direct",
                    SupplierAccount.enabled.is_(True),
                    SupplierAccount.adapter == "zovocard",
                    # a real buyer's session never goes to a sandbox in production (S-L1)
                    *((SupplierAccount.env != "sandbox",) if ctx.settings.env == "prod" else ()),
                )
                .order_by(SupplierOffer.priority, SupplierOffer.created_at)
                .limit(1)
            )
        ).first()
        kind = await db.scalar(select(Tenant.kind).where(Tenant.id == tenant_id))
        if row is None or kind != service.PLATFORM:
            return None
        params, s = row
        # primitives BEFORE the session ends: a rollback expires every ORM attribute
        ref = service.SupplierRef(s.id, s.tenant_id, s.code, s.adapter, s.env, s.health)
        return ref, dict(params or {})


async def _ask_vendor(
    ctx: ProcurementContext, ref: service.SupplierRef, params: dict[str, Any], credential: str
) -> Outcome:
    try:
        supplier = service.make_supplier(ctx, ref)
    except SupplierConfigError:
        return Outcome("unavailable", "not_configured")
    try:
        fn = getattr(supplier, "account_check", None)
        if fn is None:
            return Outcome("unavailable", "not_supported")
        try:
            res = await asyncio.wait_for(
                fn(
                    str(params.get("product") or ""),
                    str(params.get("plan") or ""),
                    params.get("payment_country"),
                    params.get("payment_currency"),
                    credential,
                ),
                ctx.settings.probe_timeout_s,
            )
        except asyncio.TimeoutError:
            return Outcome("unavailable", "timeout")
        except SupplierError as exc:
            return Outcome("unavailable", exc.reason)
    finally:
        await supplier.aclose()
    reason = res.reason if res.status != "blocked" or res.reason in BLOCK_REASONS else ""
    if res.status == "blocked" and not reason:
        reason = "account_blocked"  # an unknown block is a block, never a pass
    status = res.status if res.status in ("passed", "blocked") else "unavailable"
    return Outcome(
        status,
        reason[:48],
        res.account_mask,
        res.current_plan,
        getattr(res, "can_purchase_at", None),
        getattr(res, "upgrade", None) if status == "passed" else None,
    )


async def run(
    ctx: ProcurementContext,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    variant_id: uuid.UUID | None,
    nonce: uuid.UUID,
    blob: bytes,
) -> Outcome:
    """Open, check, forget. Never raises for a vendor, network or vault failure."""
    found = await _source(ctx, tenant_id, product_id, variant_id)
    if found is None:
        return Outcome("unavailable", "no_source")
    ref, params = found
    if ctx.cred_opener is None:
        return Outcome("unavailable", "not_configured")
    credential: str | None = None
    try:
        try:
            credential = ctx.cred_opener.open(
                blob, tenant_id=str(tenant_id), order_id=f"{AAD_PREFIX}{nonce}"
            )
        except Exception:  # wrong key / tampered / other AAD: never echo detail
            return Outcome("unavailable", "unreadable_session")
        product = str(params.get("product") or "")
        try:
            validate_credential(product, credential)
        except CredentialError:
            return Outcome("blocked", "session_invalid")
        out = await _ask_vendor(ctx, ref, params, credential)
    except Exception as exc:  # the exception text is never logged (it could carry data)
        log.warning("preflight failed error=%s", type(exc).__name__)
        out = Outcome("unavailable", "check_failed")
    finally:
        credential = None
    log.info(  # plan state only (no account): enough to explain a refusal later
        "preflight tenant=%s product=%s outcome=%s reason=%s plan=%s buy_from=%s upgrade=%s",
        tenant_id,
        product_id,
        out.status,
        out.reason,
        out.current_plan,
        out.can_purchase_at,
        bool(out.upgrade),
    )
    return out
