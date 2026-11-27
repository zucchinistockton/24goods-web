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
from app.suppliers.herosms import PRODUCTION_BASE_URL as HEROSMS_BASE_URL
from app.suppliers.herosms import HeroSmsClient, HeroSmsConfig, HeroSmsSupplier
from app.suppliers.issuers import CardIssuers
from app.suppliers.manual import ManualSupplier
from app.suppliers.mock import MockSupplier
from app.suppliers.vmcardio import (
    PRODUCTION_BASE_URL as VMCARDIO_PRODUCTION_URL,
)
from app.suppliers.vmcardio import (
    SANDBOX_BASE_URL as VMCARDIO_SANDBOX_URL,
)
from app.suppliers.vmcardio import Holder, VmcardioClient, VmcardioConfig, VmcardioSupplier
from app.suppliers.zovocard import (
    PRODUCTION_BASE_URL,
    SANDBOX_BASE_URL,
    ZovoCardSupplier,
    ZovoClient,
    ZovoConfig,
)
from app.suppliers.zzshu import PRODUCTION_BASE_URL as ZZSHU_BASE_URL
from app.suppliers.zzshu import ZzshuClient, ZzshuConfig, ZzshuSupplier

ADAPTERS: tuple[str, ...] = ("zovocard", "zzshu", "vmcardio", "herosms", "mock", "manual")
# the card issuers (supplier codes, comma separated, in order) whose wallets fund the dedicated
# cards of a zzshu order; the env variable ZZSHU_FUNDING_SUPPLIER overrides it (ADR-055:
# ``zovocard,vmcardio``)
ZZSHU_FUNDING_DEFAULT = "zovocard"
FUNDING_MAX = 4
DEV_ENVS = ("dev", "test")
_MOCKS: dict[tuple[str, str], MockSupplier] = {}


def funding_codes(environ: Mapping[str, str]) -> list[str]:
    """``ZZSHU_FUNDING_SUPPLIER`` as an ordered list of distinct valid supplier codes."""
    raw = environ.get("ZZSHU_FUNDING_SUPPLIER", "").strip() or ZZSHU_FUNDING_DEFAULT
    out: list[str] = []
    for part in raw.split(","):
        code = part.strip()
        if code and code not in out:
            if not SUPPLIER_CODE_RE.fullmatch(code) or code.endswith("_sandbox"):
                raise SupplierConfigError("invalid ZZSHU_FUNDING_SUPPLIER")
            out.append(code)
    if not out or len(out) > FUNDING_MAX:
        raise SupplierConfigError("invalid ZZSHU_FUNDING_SUPPLIER")
    return out


def issuer_adapter(code: str) -> str:
    """The adapter a funding supplier code belongs to (a code is its adapter or ``<adapter>_x``)."""
    return "vmcardio" if code == "vmcardio" or code.startswith("vmcardio_") else "zovocard"


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
    funding: Supplier | None = None,
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
    if adapter == "herosms":
        return _build_herosms(code, env, environ, app_env, allow_production_writes, http=http)
    if adapter == "vmcardio":
        return _build_vmcardio(code, env, environ, app_env, allow_production_writes, http=http)
    if adapter == "zzshu":
        return _build_zzshu(
            code, env, environ, app_env, allow_production_writes, http=http, funding=funding
        )
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


def _build_herosms(
    code: str,
    env: str,
    environ: Mapping[str, str],
    app_env: str,
    allow_production_writes: bool,
    *,
    http: httpx.AsyncClient | None,
) -> Supplier:
    """``HEROSMS_APP_SECRET`` (the API key) / ``HEROSMS_BASE_URL`` (ADR-057). HeroSMS has no
    sandbox: a sandbox row only works against a dev simulator URL."""
    sec = load_secrets(code, env, environ, default_base=HEROSMS_BASE_URL)
    if not sec.base_url.startswith("https://") and app_env not in DEV_ENVS:
        raise SupplierConfigError("supplier base URL must be https:// outside dev/test")
    cfg = HeroSmsConfig(
        base_url=sec.base_url,
        api_key=sec.app_secret,
        env=env,
        allow_production_writes=allow_production_writes,
    )
    return HeroSmsSupplier(code, HeroSmsClient(cfg, http=http))


def _build_vmcardio(
    code: str,
    env: str,
    environ: Mapping[str, str],
    app_env: str,
    allow_production_writes: bool,
    *,
    http: httpx.AsyncClient | None,
) -> Supplier:
    """``VMCARDIO_APP_ID`` / ``VMCARDIO_APP_SECRET`` / ``VMCARDIO_BASE_URL`` (``..._SANDBOX_...``
    for sandbox) and the cardholder data ``VMCARDIO_CARDHOLDER_*`` / ``VMCARDIO_CARD_*``
    (procurement
    env only; the address is optional as a whole)."""
    default = VMCARDIO_PRODUCTION_URL if env == "production" else VMCARDIO_SANDBOX_URL
    sec = load_secrets(code, env, environ, default_base=default)
    if not sec.app_id:
        raise SupplierConfigError("VMCARDIO_APP_ID is not set")
    if not sec.base_url.startswith("https://") and app_env not in DEV_ENVS:
        raise SupplierConfigError("supplier base URL must be https:// outside dev/test")

    def val(name: str, default: str = "") -> str:
        return environ.get(f"VMCARDIO_{name}", "").strip() or default

    holder = Holder(
        first_name=val("CARDHOLDER_FIRST_NAME", Holder.first_name),
        last_name=val("CARDHOLDER_LAST_NAME", Holder.last_name),
        address_line_one=val("CARD_ADDRESS_LINE1"),
        city=val("CARD_CITY"),
        state=val("CARD_STATE"),
        post_code=val("CARD_POSTCODE"),
        country=val("CARD_COUNTRY", "US"),
    )
    cfg = VmcardioConfig(
        base_url=sec.base_url,
        app_id=sec.app_id,
        app_secret=sec.app_secret,
        env=env,
        allow_production_writes=allow_production_writes,
    )
    return VmcardioSupplier(code, VmcardioClient(cfg, http=http), holder)


def _build_zzshu(
    code: str,
    env: str,
    environ: Mapping[str, str],
    app_env: str,
    allow_production_writes: bool,
    *,
    http: httpx.AsyncClient | None,
    funding: Supplier | None,
) -> Supplier:
    """``ZZSHU_APP_SECRET`` (the API key) / ``ZZSHU_BASE_URL``; the cards that pay its orders come
    from the ZovoCard supplier ``ZZSHU_FUNDING_SUPPLIER`` (default ``zovocard``, same env)."""
    sec = load_secrets(code, env, environ, default_base=ZZSHU_BASE_URL)
    if not sec.base_url.startswith("https://") and app_env not in DEV_ENVS:
        raise SupplierConfigError("supplier base URL must be https:// outside dev/test")
    owns = funding is None
    if funding is None:
        issuers: list[tuple[str, Supplier]] = []
        for i, fcode in enumerate(funding_codes(environ)):
            try:
                issuers.append(
                    (
                        fcode,
                        build_supplier(
                            code=fcode,
                            adapter=issuer_adapter(fcode),
                            env=env,
                            environ=environ,
                            app_env=app_env,
                            allow_production_writes=allow_production_writes,
                            http=http,
                        ),
                    )
                )
            except SupplierConfigError:
                if i == 0:
                    raise  # the first issuer is required, a later one is skipped when unset
        funding = CardIssuers(issuers, owns=True)
    cfg = ZzshuConfig(
        base_url=sec.base_url,
        api_key=sec.app_secret,
        env=env,
        allow_production_writes=allow_production_writes,
    )
    return ZzshuSupplier(code, ZzshuClient(cfg, http=http), funding, owns_funding=owns)


__all__ = [
    "ADAPTERS",
    "FUNDING_MAX",
    "SupplierConfigError",
    "SupplierSecrets",
    "build_supplier",
    "funding_codes",
    "issuer_adapter",
    "load_secrets",
]
