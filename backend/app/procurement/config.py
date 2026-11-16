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
    # buyer-credential PRIVATE keys for upstream_direct (review-v12 §18): the only process that may
    # open a buyer's account session. NOT the card vault's keys (that stays refused above).
    cred_private_keys_file: str = ""
    # a sealed credential is deleted after this many seconds in any case (default 5 h; the API's
    # database backstop deletes at 6 h, so the engine's own sweep always wins): a session text is
    # a live credential for the buyer's account. Keep ``replay_max_age_s`` within the vendor's
    # idempotency-key retention (unverified)
    cred_max_age_s: float = 18000.0
    cred_sweep_interval_s: float = 30.0  # how often the sweeper looks for credentials to delete
    # an upstream_direct order still pending at the supplier after this long goes to needs_manual
    # (``pending_too_long``): a subscription takes minutes, an order that never moves needs staff
    direct_pending_max_s: float = 7200.0
    # a preflight quote may exceed the offer's cost by this fraction before the order is refused
    direct_quote_tolerance: float = 0.10
    # consecutive declined / failed_precharge orders on ONE source before it is switched off
    direct_decline_limit: int = 3
    # orders at the vendor at the same time on ONE funding card
    direct_card_inflight_max: int = 1
    # engine (review-v12 §3, §6)
    jobs_poll_s: float = 2.0
    job_lease_s: float = 120.0
    # How long a same-key replay (reconcile / resubmit / poll) is still trusted after the first
    # send. The vendor's idempotency cache is not documented to live forever: beyond this age a
    # replay could execute a SECOND purchase, so the job goes to needs_manual (maybe charged)
    # instead. Default 6 h (review-v12 §6: the engine gives up on unresolved sends in ~30 min).
    replay_max_age_s: float = 21600.0
    # No NEW purchase is leased while a supplier's last good balance reading is older than this
    # (default 3 balance intervals): without a fresh balance the breaker is blind (fail closed).
    balance_stale_s: float = 900.0
    # CDK redemption proxy (review-v12 §8): a SECOND, public listener, off by default. It never
    # serves the internal API; the internal API never serves it.
    redeem_enabled: bool = False
    redeem_host: str = "127.0.0.1"
    redeem_port: int = 8006
    # constant X-Redemption-Device sent to the vendor on every step (per deployment, not per buyer)
    redeem_device: str = ""
    # "" = the one enabled ZovoCard supplier of the platform tenant; "sandbox"/"production" pins it
    redeem_supplier_env: str = ""
    # proxies whose X-Forwarded-For is believed (comma-separated CIDRs); "" = use the peer address
    redeem_trusted_proxies: str = ""
    redeem_upstream_timeout_s: float = 20.0
    # per-IP per-minute limits by step, then ONE shared budget per step for all buyers: the vendor
    # limits per egress IP (preview 30, redeem 20, result 60 per minute) and every buyer shares ours
    redeem_ip_limits: str = "preview=10,preflight=10,redeem=6,result=40"
    redeem_global_limits: str = "preview=25,preflight=20,redeem=15,result=55"
    redeem_code_per_10min: int = 10  # preview attempts per code (hashed) before a cool-down
    redeem_token_per_min: int = 40  # per redemption token (hashed), all steps

    def __post_init__(self) -> None:
        # the database backstop (migration 0030) lets the API delete a session older than 6 h
        if not 0 < self.cred_max_age_s <= 21600:
            raise ValueError("APP_CRED_MAX_AGE_S must be between 1 and 21600 (6 h)")

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
