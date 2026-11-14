from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.procurement.config import ProcurementSettings


@dataclass
class ProcurementContext:
    settings: ProcurementSettings
    maker: async_sessionmaker[AsyncSession]
    redis: Redis
    # environment holding supplier secrets (os.environ in production; a dict in tests)
    environ: Mapping[str, str] = field(repr=False, default_factory=dict)
    # optional shared transport for supplier clients (tests inject the mock server here)
    http: httpx.AsyncClient | None = None
