"""Build a :class:`Supplier` from a supplier row's ``code`` / ``adapter`` / ``env``.

Secrets never come from the database: they are read from the procurement process environment
by code and env (review-v12 §4)::

    <CODE>_APP_SECRET / <CODE>_APP_ID / <CODE>_BASE_URL                      production
    <CODE>_SANDBOX_APP_SECRET / <CODE>_SANDBOX_APP_ID / <CODE>_SANDBOX_BASE_URL  sandbox

e.g. ``ZOVOCARD_APP_SECRET`` and ``ZOVOCARD_SANDBOX_APP_SECRET`` for code ``zovocard``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx

from app.suppliers.base import SUPPLIER_CODE_RE, SUPPLIER_ENVS, Supplier, supplier_code_problem
from app.suppliers.manual import ManualSupplier
from app.suppliers.mock import MockSupplier
from app.suppliers.zovocard import (
    PRODUCTION_BASE_URL,
    SANDBOX_BASE_URL,
    ZovoCardSupplier,
    ZovoClient,
    ZovoConfig,
)

ADAPTERS: tuple[str, ...] = ("zovocard", "mock", "manual")
DEV_ENVS = ("dev", "test")
_MOCKS: dict[tuple[str, str], MockSupplier] = {}


class SupplierConfigError(RuntimeError):
    """Missing / invalid supplier configuration. The message never contains a secret."""


@dataclass(frozen=True)
class SupplierSecrets:
    app_secret: str = field(repr=False)
    app_id: str
    base_url: str


def load_secrets(
    code: str, env: str, environ: Mapping[str, str], *, default_base: str = ""
) -> SupplierSecrets:
    if not SUPPLIER_CODE_RE.fullmatch(code) or code.endswith("_sandbox"):
        raise SupplierConfigError("invalid supplier code")
    if env not in SUPPLIER_ENVS:
        raise SupplierConfigError("env must be sandbox or production")
    prefix = code.upper() + ("_SANDBOX_" if env == "sandbox" else "_")
    secret = environ.get(prefix + "APP_SECRET", "").strip()
    if not secret:
        raise SupplierConfigError(f"{prefix}APP_SECRET is not set")
    base = environ.get(prefix + "BASE_URL", "").strip() or default_base
    if not base:
        raise SupplierConfigError(f"{prefix}BASE_URL is not set")
    return SupplierSecrets(
        app_secret=secret, app_id=environ.get(prefix + "APP_ID", "").strip(), base_url=base
    )


def build_supplier(
    *,
    code: str,
    adapter: str,
    env: str,
    environ: Mapping[str, str],
    app_env: str,
    allow_production_writes: bool = False,
    http: httpx.AsyncClient | None = None,
) -> Supplier:
    if adapter not in ADAPTERS:
        raise SupplierConfigError("unknown adapter")
    problem = supplier_code_problem(code, adapter)
    if problem is not None:
        raise SupplierConfigError(f"supplier code refused: {problem}")
    if env not in SUPPLIER_ENVS:
        raise SupplierConfigError("env must be sandbox or production")
    if adapter == "manual":
        return ManualSupplier(code, env=env)
    if adapter == "mock":
        if app_env not in DEV_ENVS:
            raise SupplierConfigError("the mock supplier only runs with APP_ENV=dev|test")
        # one instance per process and (code, env): the mock keeps its "upstream" state, so a
        # replay/query sees what an earlier submit did (dev stack, tests)
        key = (code, env)
        if key not in _MOCKS:
            _MOCKS[key] = MockSupplier(code, env=env)
        return _MOCKS[key]
    default = PRODUCTION_BASE_URL if env == "production" else SANDBOX_BASE_URL
    sec = load_secrets(code, env, environ, default_base=default)
    if not sec.base_url.startswith("https://") and app_env not in DEV_ENVS:
        raise SupplierConfigError("supplier base URL must be https:// outside dev/test")
    cfg = ZovoConfig(
        base_url=sec.base_url,
        app_secret=sec.app_secret,
        app_id=sec.app_id,
        env=env,
        allow_production_writes=allow_production_writes,
    )
    return ZovoCardSupplier(code, ZovoClient(cfg, http=http))


__all__ = [
    "ADAPTERS",
    "SupplierConfigError",
    "SupplierSecrets",
    "build_supplier",
    "load_secrets",
]
