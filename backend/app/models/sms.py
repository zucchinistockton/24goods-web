"""SMS verification codes (migration 0044, ADR-057).

``SmsProduct`` is admin configuration (API writes) plus the availability procurement computed;
``SmsSession`` / ``SmsAttempt`` are written by procurement only. No column ever holds a code: a
code is sealed into ``card_stock`` as soon as it arrives.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, false
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at, updated_at, uuid_pk


class SmsProduct(Base):
    __tablename__ = "sms_products"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"), unique=True)
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("suppliers.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    last_check_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_check_at: Mapped[datetime | None]
    last_check_reason: Mapped[str | None] = mapped_column(String(64))
    stock_hint: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class SmsSession(Base):
    __tablename__ = "sms_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), unique=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"))
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("suppliers.id"))
    state: Mapped[str] = mapped_column(String(16), default="ready")
    attempts_used: Mapped[int] = mapped_column(Integer, default=0)
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    start_deadline: Mapped[datetime]
    started_at: Mapped[datetime | None]
    window_ends_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    fail_reason: Mapped[str | None] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class SmsAttempt(Base):
    __tablename__ = "sms_attempts"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sms_sessions.id"))
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"))
    seq: Mapped[int] = mapped_column(Integer)
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id"))
    upstream_id: Mapped[str] = mapped_column(String(64))
    country: Mapped[int] = mapped_column(Integer)
    operator: Mapped[str] = mapped_column(String(48), default="")
    phone: Mapped[str] = mapped_column(String(32))
    cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    state: Mapped[str] = mapped_column(String(16), default="waiting")
    bought_at: Mapped[datetime]
    expires_at: Mapped[datetime | None]
    code_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]
    upstream_closed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
