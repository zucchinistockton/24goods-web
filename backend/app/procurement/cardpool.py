"""Card pool service of the procurement process (issue #29 part 2, review-v12 §21.8).

The console never holds vendor keys: it asks the internal API, which lands here. Reads are GETs;
the two writes (``card-rules`` PUT, ``reset-usage`` POST) are refused for a production supplier
unless ``APP_PROCUREMENT_ALLOW_PRODUCTION_WRITES`` is on (the same guard the client enforces; it is
checked here first so a refusal is a clean answer, and the client still blocks it as a backstop).
No card number is returned: the vendor card list is reduced to id + last four at the parser.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import select

from app.core.db import tenant_session
from app.models import ProcurementJob
from app.procurement import service
from app.procurement.context import ProcurementContext
from app.procurement.funding import funding_card_clash
from app.suppliers.zovocard import cardpool as cp


class ProductionWritesDisabled(Exception):
    """A write to a production supplier without the explicit opt-in. Nothing was sent."""


class SupplierDisabled(Exception):
    """A write through a supplier row that is switched off. Nothing was sent."""


class NotFundingCard(ValueError):
    """A card id that is not one of OUR funding cards (unknown to the vendor list, or a buyer's)."""


async def _client(
    ctx: ProcurementContext,
    tenant_id: uuid.UUID,
    supplier_id: uuid.UUID | None,
    *,
    write: bool = False,
):
    if supplier_id is None:
        ref, supplier = await service._zovo_supplier(ctx, tenant_id)
    else:
        ref = await service.load_ref(ctx, tenant_id, supplier_id)
        if write and not ref.enabled:  # reads may still show a disabled row; writes never go out
            raise SupplierDisabled
        try:
            supplier = service.make_supplier(ctx, ref)
        except service.SupplierConfigError:
            raise service.SupplierNotFound from None
    client = getattr(supplier, "client", None)
    if ref.adapter != "zovocard" or client is None:
        await supplier.aclose()
        raise service.SupplierNotFound
    return ref, supplier, client


def _guard(ctx: ProcurementContext, env: str, client) -> None:
    """Production, or a ``sandbox`` row pointing at a non-sandbox https host, needs the opt-in."""
    if (
        env == "production" or client.config.production_target
    ) and not ctx.settings.procurement_allow_production_writes:
        raise ProductionWritesDisabled


async def _buyer_cards(
    ctx: ProcurementContext, tenant_id: uuid.UUID
) -> tuple[set[str], frozenset[str]]:
    """What marks a card as a buyer's: the vendor card id of ANY ``vcc`` job of the tenant (whatever
    its supplier row: rows may share one vendor account), and the masked number of every non-voided
    one (a card pasted by hand has no id). Other tenants' jobs never count."""
    async with tenant_session(ctx.maker, tenant_id) as db:
        rows = (
            await db.execute(
                select(
                    ProcurementJob.upstream_ref, ProcurementJob.card_mask, ProcurementJob.state
                ).where(ProcurementJob.tenant_id == tenant_id, ProcurementJob.kind == "vcc")
            )
        ).all()
    ids = {str(r.upstream_ref) for r in rows if r.upstream_ref}
    masks = frozenset(r.card_mask for r in rows if r.card_mask and r.state != "voided")
    return ids, masks


async def funding_cards(
    client, buyer_ids: set[str], buyer_masks: frozenset[str] = frozenset()
) -> tuple[list[dict[str, Any]], int]:
    """Our funding cards and how many vendor cards were left out as buyers' (by id or by mask)."""
    cards = await cp.read_cards(client, buyer_masks)
    ours = [c for c in cards if str(c["id"]) not in buyer_ids and not c["buyer_mask"]]
    for c in ours:
        c.pop("buyer_mask", None)
    return ours, len(cards) - len(ours)


async def pool(
    ctx: ProcurementContext, tenant_id: uuid.UUID, supplier_id: uuid.UUID | None, product: str
) -> dict[str, Any]:
    """Our funding cards with their usage for ``product`` (one usage read per card, bounded)."""
    ref, supplier, client = await _client(ctx, tenant_id, supplier_id)
    try:

        async def work() -> dict[str, Any]:
            ours, excluded = await funding_cards(client, *(await _buyer_cards(ctx, tenant_id)))
            shown = ours[: cp.MAX_USAGE_READS]
            for card in shown:
                try:
                    card["usage"] = await cp.read_usage(client, card["id"], product)
                except Exception:  # one unreadable card must not hide the others
                    card["usage"] = {"readable": False}
            return {
                "supplier": {"id": str(ref.id), "code": ref.code, "env": ref.env},
                "product": product,
                "cards": shown,
                "truncated": len(ours) > len(shown),
                "excluded_buyer_cards": excluded,
            }

        return await asyncio.wait_for(work(), ctx.settings.probe_timeout_s * 2)
    finally:
        await supplier.aclose()


async def schedule(
    ctx: ProcurementContext,
    tenant_id: uuid.UUID,
    supplier_id: uuid.UUID | None,
    product: str,
    plan: str,
) -> dict[str, Any]:
    _ref, supplier, client = await _client(ctx, tenant_id, supplier_id)
    try:
        return await asyncio.wait_for(
            cp.read_schedule(client, product, plan), ctx.settings.probe_timeout_s
        )
    finally:
        await supplier.aclose()


async def rules(
    ctx: ProcurementContext, tenant_id: uuid.UUID, supplier_id: uuid.UUID | None
) -> dict[str, Any]:
    ref, supplier, client = await _client(ctx, tenant_id, supplier_id)
    try:
        got = await asyncio.wait_for(cp.read_rules(client), ctx.settings.probe_timeout_s)
        return {
            "supplier": {"id": str(ref.id), "code": ref.code, "env": ref.env},
            "rules": got,
        }
    finally:
        await supplier.aclose()


async def set_rule(
    ctx: ProcurementContext,
    tenant_id: uuid.UUID,
    supplier_id: uuid.UUID | None,
    product: str,
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Merge ``patch`` into the vendor's current rule for ``product`` and save it. Returns
    ``{before, after}`` for the audit row (after = the vendor's re-read, else the merged value)."""
    ref, supplier, client = await _client(ctx, tenant_id, supplier_id, write=True)
    try:
        _guard(ctx, ref.env, client)
        patch = cp.check_rule_patch(patch)
        before = (await asyncio.wait_for(cp.read_rules(client), ctx.settings.probe_timeout_s)).get(
            product
        )
        if before is None:
            raise LookupError("rule not readable")
        merged = {**before, **patch}
        await asyncio.wait_for(cp.write_rule(client, product, merged), ctx.settings.probe_timeout_s)
        try:
            after = (await cp.read_rules(client)).get(product) or merged
        except Exception:
            after = merged
        return {"supplier": str(ref.id), "env": ref.env, "before": before, "after": after}
    finally:
        await supplier.aclose()


async def reset_failed(
    ctx: ProcurementContext,
    tenant_id: uuid.UUID,
    supplier_id: uuid.UUID | None,
    card_ids: list[int],
) -> dict[str, Any]:
    """Reset the FAILED usage of explicit funding cards (``scope=failed``, never ``all``)."""
    ref, supplier, client = await _client(ctx, tenant_id, supplier_id, write=True)
    try:
        _guard(ctx, ref.env, client)
        ours, _ = await funding_cards(client, *(await _buyer_cards(ctx, tenant_id)))
        mine = {c["id"] for c in ours}
        if not card_ids or any(c not in mine for c in card_ids):
            raise NotFundingCard("not a funding card of this supplier")
        async with tenant_session(ctx.maker, tenant_id) as db:  # defence in depth: id clash
            for cid in card_ids:
                if await funding_card_clash(db, tenant_id, ref.id, cid):
                    raise NotFundingCard("a buyer's card")
        n = await asyncio.wait_for(cp.reset_failed(client, card_ids), ctx.settings.probe_timeout_s)
        return {"supplier": str(ref.id), "env": ref.env, "card_ids": card_ids, "reset": n}
    finally:
        await supplier.aclose()
