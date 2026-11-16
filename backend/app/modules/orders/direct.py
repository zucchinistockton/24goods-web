"""``upstream_direct`` checkout side (review-v12 §18): the buyer's account session.

The session is a live credential for the buyer's account. Here it is validated (size, charset, shape
for the vendor product the target's sources buy), sealed AT ONCE with the procurement PUBLIC key and
inserted into ``procurement_credentials``; the API role can write that table and read only its
non-secret columns, so nothing in this process (or a database read by it) can ever open it again.
``orders.buyer_input`` (plaintext, shown to staff) is NOT used for it and stays empty.

Nothing in this module logs, audits or returns the session; ``DirectSeal`` hides it from ``repr``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.credvault import CredentialSealer
from app.core.errors import Conflict, InvalidState, NotFound, Unavailable, Validation
from app.models import Order, ProcurementCredential, ProcurementJob, SupplierOffer
from app.modules.catalog import upstream
from app.modules.common.modes import MAX_DIRECT_QTY
from app.suppliers.direct_credential import CredentialError, validate_credential


@dataclass(frozen=True)
class DirectSeal:
    """A validated session, not yet sealed. ``text`` is the credential: repr-hidden."""

    product: str
    email_masked: str | None
    text: str = field(repr=False)


async def prepare(
    db: AsyncSession,
    sealer: CredentialSealer | None,
    *,
    product_id: uuid.UUID,
    variant_id: uuid.UUID | None,
    qty: int,
    session: str | None,
) -> DirectSeal:
    """Everything that can be refused BEFORE an order exists. Raises specific error codes:
    ``direct_qty_one``, ``session_required``, ``direct_unavailable`` (no key configured),
    ``out_of_stock`` (no usable source), and ``session_<reason>`` for a credential that cannot
    work (``too_large``, ``too_short``, ``invalid_format``, ``not_json``, ``access_token_only``,
    ``missing_session_token``, ``missing_access_token``, ``malformed_access_token``, ``expired``,
    ``missing_account``, ``account_mismatch``)."""
    if qty != MAX_DIRECT_QTY:
        raise Validation("This product is bought one account at a time", code="direct_qty_one")
    if not (session or "").strip():
        raise Validation("The account session is required", code="session_required")
    if sealer is None:
        raise Unavailable("This product cannot be bought right now", code="direct_unavailable")
    vendor_product = await upstream.direct_target_product(db, product_id, variant_id)
    try:
        checked = validate_credential(vendor_product, session)
    except CredentialError as exc:
        raise Validation("The account session cannot be used", code=f"session_{exc.code}") from None
    return DirectSeal(vendor_product, checked.email_masked, checked.canonical)


async def store(db: AsyncSession, sealer: CredentialSealer, order: Order, seal: DirectSeal) -> None:
    """Seal and insert (Core insert, no RETURNING: the API cannot read ``blob`` back)."""
    blob = sealer.seal(seal.text, tenant_id=str(order.tenant_id), order_id=str(order.id))
    try:
        async with db.begin_nested():
            await db.execute(
                insert(ProcurementCredential.__table__).values(
                    id=uuid.uuid4(),
                    tenant_id=order.tenant_id,
                    order_id=order.id,
                    product=seal.product,
                    kid=sealer.active_kid,
                    blob=blob,
                    email_masked=seal.email_masked,
                )
            )
    except IntegrityError:
        raise Conflict(
            "A session is already stored for this order", code="session_exists"
        ) from None


def info_view(
    order: Order, job_mask: str | None, cred_mask: str | None, plan: str | None, state: str | None
) -> dict[str, Any]:
    """What the buyer may see about a direct order: the masked account and the plan, never more."""
    return {
        "order_id": str(order.id),
        "account": job_mask or cred_mask,
        "plan": plan,
        "state": state,
    }


async def owner_info(db: AsyncSession, order: Order) -> dict[str, Any]:
    if order.delivery_mode != "upstream_direct":
        raise NotFound("Order not found")
    cred_mask = await db.scalar(
        select(ProcurementCredential.email_masked).where(
            ProcurementCredential.tenant_id == order.tenant_id,
            ProcurementCredential.order_id == order.id,
        )
    )
    row = (
        await db.execute(
            select(ProcurementJob.account_mask, ProcurementJob.state, SupplierOffer.params)
            .outerjoin(SupplierOffer, SupplierOffer.id == ProcurementJob.offer_id)
            .where(ProcurementJob.tenant_id == order.tenant_id, ProcurementJob.order_id == order.id)
            .order_by(ProcurementJob.created_at)
            .limit(1)
        )
    ).first()
    plan = None
    if row is not None and row.params:
        plan = f"{row.params.get('product')}/{row.params.get('plan')}"
    return info_view(order, row.account_mask if row else None, cred_mask, plan,
                     row.state if row else None)  # fmt: skip


async def resupply(
    db: AsyncSession,
    sealer: CredentialSealer | None,
    order: Order,
    session: str | None,
    *,
    actor_id: uuid.UUID,
    ip: str | None,
) -> None:
    """The buyer replaces a session that expired or was rejected (the stored one is gone: the
    sweeper deletes it at a terminal state or after its maximum age). Only for the buyer's own
    ``paid`` / ``manual_review`` direct order that has no stored session; staff then retry the
    job. Same validation and sealing as at checkout."""
    if order.delivery_mode != "upstream_direct":
        raise NotFound("Order not found")
    if order.status not in ("paid", "manual_review"):
        raise InvalidState("This order does not need a new session", code="session_not_needed")
    if not (session or "").strip():
        raise Validation("The account session is required", code="session_required")
    if sealer is None:
        raise Unavailable("This product cannot be bought right now", code="direct_unavailable")
    vendor_product = await db.scalar(
        select(SupplierOffer.params["product"].astext)
        .join(ProcurementJob, ProcurementJob.offer_id == SupplierOffer.id)
        .where(ProcurementJob.tenant_id == order.tenant_id, ProcurementJob.order_id == order.id)
        .limit(1)
    )
    if vendor_product is None:
        vendor_product = await upstream.direct_target_product(
            db, order.product_id, order.variant_id
        )
    try:
        checked = validate_credential(vendor_product, session)
    except CredentialError as exc:
        raise Validation("The account session cannot be used", code=f"session_{exc.code}") from None
    await store(
        db, sealer, order, DirectSeal(vendor_product, checked.email_masked, checked.canonical)
    )
    await audit(
        db,
        tenant_id=order.tenant_id,
        action="order.direct_session_resupplied",
        actor_id=actor_id,
        actor_role="buyer",
        target=f"order:{order.id}",
        detail={"product": vendor_product},  # never the session
        ip=ip,
    )
