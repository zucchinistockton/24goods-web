"""Procurement (review-v12 §4, migration 0023_procurement). All tables tenant-scoped, FORCE RLS.

Supplier secrets are never stored here: they live in the procurement env file, keyed by
``code`` and ``env``. Amounts are ``numeric`` (Decimal) next to a currency, never float.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, false
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at, updated_at, uuid_pk

AMOUNT = Numeric(20, 6)


class SupplierAccount(Base):
    """One configured supplier (table ``suppliers``)."""

    __tablename__ = "suppliers"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    code: Mapped[str] = mapped_column(String(32))
    adapter: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(80))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    env: Mapped[str] = mapped_column(String(16), default="sandbox")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    min_spendable: Mapped[Decimal] = mapped_column(AMOUNT, default=Decimal(0))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    health: Mapped[str] = mapped_column(String(16), default="unknown")
    health_detail: Mapped[str | None] = mapped_column(String(64))
    last_probe_at: Mapped[datetime | None]
    last_balance: Mapped[Decimal | None] = mapped_column(AMOUNT)
    last_balance_at: Mapped[datetime | None]
    breaker_open: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    breaker_reason: Mapped[str | None] = mapped_column(String(64))
    breaker_at: Mapped[datetime | None]
    breaker_kind: Mapped[str | None] = mapped_column(String(8))  # hard | balance
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class SupplierOffer(Base):
    __tablename__ = "supplier_offers"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id"))
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"))
    variant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("product_variants.id"))
    kind: Mapped[str] = mapped_column(String(16))
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    cost_estimate: Mapped[Decimal | None] = mapped_column(AMOUNT)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    last_check_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_check_at: Mapped[datetime | None]
    # 0027: the supplier named a redemption cost for this offer (the breaker may reserve it)
    cost_known: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class ProcurementJob(Base):
    __tablename__ = "procurement_jobs"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"))
    unit_no: Mapped[int] = mapped_column(Integer)
    offer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("supplier_offers.id"))
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("suppliers.id"))
    kind: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(24), default="queued")
    step: Mapped[str] = mapped_column(String(16), default="submit")
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    client_request_id: Mapped[str | None] = mapped_column(String(80))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    upstream_ref: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_poll_at: Mapped[datetime | None]
    preflight_expires_at: Mapped[datetime | None]
    cost: Mapped[Decimal | None] = mapped_column(AMOUNT)
    currency: Mapped[str | None] = mapped_column(String(3))
    card_stock_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)
    manual_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    tried_offer_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list
    )
    upstream_status: Mapped[str | None] = mapped_column(String(32))
    alerted_at: Mapped[datetime | None]
    # 0027: send tracking (see the migration docstring)
    maybe_sent: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    recover: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    lease_until: Mapped[datetime | None]
    first_sent_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class SupplierWebhookEvent(Base):
    """Verified supplier webhook (table ``supplier_events``); card numbers masked, no OTP."""

    __tablename__ = "supplier_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id"))
    dedup_key: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    received_at: Mapped[datetime] = created_at()
    processed_at: Mapped[datetime | None]


class SupplierLedgerEntry(Base):
    """Supplier cost ledger: ``balance_change`` rows only (review-v12 §6)."""

    __tablename__ = "supplier_ledger"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id"))
    external_id: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(48))
    amount: Mapped[Decimal] = mapped_column(AMOUNT)
    currency: Mapped[str] = mapped_column(String(3))
    balance_after: Mapped[Decimal | None] = mapped_column(AMOUNT)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("procurement_jobs.id"))
    ref_id: Mapped[str | None] = mapped_column(String(64))
    card_mask: Mapped[str | None] = mapped_column(String(24))
    occurred_at: Mapped[datetime]
    created_at: Mapped[datetime] = created_at()
