"""Storefront availability of ``upstream_cdk`` products (review-v12 §4 "stock display").

"In stock" iff an enabled ``cdk`` offer of an enabled supplier whose breaker is closed exists for
the target and its last availability check (run by procurement) passed. Checkout re-checks this
cached state; it never calls a supplier. The displayed stock is the per-order cap
(``MAX_UPSTREAM_QTY``) or 0: there is no local pool to count.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import OutOfStock, Validation
from app.models import SupplierAccount, SupplierOffer
from app.modules.common.modes import MAX_UPSTREAM_QTY


def _usable():  # type: ignore[no-untyped-def]
    return (
        SupplierOffer.enabled.is_(True),
        SupplierOffer.kind == "cdk",
        SupplierOffer.last_check_ok.is_(True),
        SupplierAccount.enabled.is_(True),
        SupplierAccount.breaker_open.is_(False),
    )


def _base():  # type: ignore[no-untyped-def]
    return select(SupplierOffer.product_id, SupplierOffer.variant_id).join(
        SupplierAccount, SupplierAccount.id == SupplierOffer.supplier_id
    )


async def product_stock(db: AsyncSession, product_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Product level: available when any variant (or the variant-less target) is."""
    ids = list(product_ids)
    if not ids:
        return {}
    rows = await db.execute(_base().where(SupplierOffer.product_id.in_(ids), *_usable()))
    ok = {pid for pid, _ in rows.all()}
    return {pid: (MAX_UPSTREAM_QTY if pid in ok else 0) for pid in ids}


async def variant_stock(db: AsyncSession, variant_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
    ids = list(variant_ids)
    if not ids:
        return {}
    rows = await db.execute(_base().where(SupplierOffer.variant_id.in_(ids), *_usable()))
    ok = {vid for _, vid in rows.all()}
    return {vid: (MAX_UPSTREAM_QTY if vid in ok else 0) for vid in ids}


async def target_available(
    db: AsyncSession, product_id: uuid.UUID, variant_id: uuid.UUID | None
) -> bool:
    match = (
        SupplierOffer.variant_id == variant_id
        if variant_id is not None
        else SupplierOffer.variant_id.is_(None)
    )
    row = await db.scalar(
        _base().where(SupplierOffer.product_id == product_id, match, *_usable()).limit(1)
    )
    return row is not None


async def require_upstream_stock(
    db: AsyncSession, product_id: uuid.UUID, variant_id: uuid.UUID | None, qty: int
) -> None:
    if qty > MAX_UPSTREAM_QTY:
        raise Validation(f"qty must be at most {MAX_UPSTREAM_QTY} for this product")
    if not await target_available(db, product_id, variant_id):
        raise OutOfStock("Not available right now", extra={"product_id": str(product_id)})
