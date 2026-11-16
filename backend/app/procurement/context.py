from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.cardvault import CardSealer
from app.core.credvault import CredentialOpener
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
    # seal-only vault (public key + fingerprint key); None = the engine refuses to fulfil
    sealer: CardSealer | None = None
    # buyer-credential opener (upstream_direct): the ONLY place a buyer's session is decrypted
    cred_opener: CredentialOpener | None = None
    # tests: build suppliers from a factory (SupplierRef -> Supplier) instead of the registry
    supplier_factory: Callable[[Any], Any] | None = None
    # monotonic time of the last credential sweep (None = never)
    cred_swept_at: float | None = None
    # engine: one supplier instance per (adapter, code, env), closed on shutdown
    suppliers: dict[tuple[str, str, str], Any] = field(default_factory=dict)
