"""Procurement settings read straight from ``APP_*`` environment variables.

Not pydantic-based, like :mod:`app.delivery.config`: the process stays small and does not load
the API stack. Supplier secrets are NOT settings fields: :mod:`app.suppliers.registry` reads
them from the environment by supplier code and env at the moment a supplier is built.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True)
class ProcurementSettings:
    # fail safe: without APP_ENV the process behaves as production (no mock adapter, https only)
    env: str = "prod"
    log_level: str = "INFO"
    procurement_database_url: str = (
        "postgresql+asyncpg://app_procurement:app_procurement@localhost:5432/app"
    )
    redis_url: str = "redis://localhost:6379/0"
    procurement_token: str = ""  # shared secret API -> procurement internal API, >= 32 chars
    procurement_host: str = "127.0.0.1"
    procurement_port: int = 8004
    balance_interval_s: float = 300.0  # review-v12 §6: balance refreshed every 5 min
    ledger_interval_s: float = 600.0  # review-v12 §6: balance-logs backfill every 10 min
    probe_timeout_s: float = 45.0
    probes_per_supplier_10min: int = 10
    # review-v12 §11: writes to a production supplier need this explicit opt-in (no dev/demo
    # env file sets it). M1 makes no write calls at all.
    procurement_allow_production_writes: bool = False
    # vault PUBLIC keys + fingerprint key (seal only); a private key file is never configured here
    vault_public_keys_file: str = ""
    vault_active_kid: str = ""
    vault_fingerprint_key: str = ""
    # engine (review-v12 §3, §6)
    jobs_poll_s: float = 2.0
    job_lease_s: float = 120.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ProcurementSettings:
        env = os.environ if environ is None else environ
        values: dict[str, object] = {}
        for f in fields(cls):
            name = "APP_" + f.name.upper()
            raw = env.get(name)
            if raw is None or raw == "":
                continue
            kind = type(f.default)
            if kind is bool:
                low = raw.strip().lower()
                if low not in _TRUE | _FALSE:
                    raise ValueError(f"{name} must be 0 or 1")
                values[f.name] = low in _TRUE
                continue
            try:
                values[f.name] = kind(raw.strip()) if kind is not str else raw
            except ValueError as exc:
                raise ValueError(f"{name} must be {kind.__name__}") from exc
        if env.get("APP_VAULT_PRIVATE_KEYS_FILE"):
            raise ValueError("procurement must never be given vault private keys")
        return cls(**values)  # type: ignore[arg-type]
