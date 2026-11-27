"""``python -m app.procurement``: supplier health loop + internal API (APP_PROCUREMENT_PORT).

``python -m app.procurement probe --supplier <code> --env <sandbox|production> [--adapter X]``
runs one read-only probe with the secrets in the environment (no database needed) and prints a
redacted JSON report: no secret, no response body. Exit code 0 when the probe is green.

M2: also runs the upstream_cdk engine (app/procurement/engine.py).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
from datetime import datetime, timezone

from app.core.logging import configure_logging
from app.procurement.config import ProcurementSettings
from app.suppliers.base import SUPPLIER_CODE_RE, SUPPLIER_ENVS, ProbeReport
from app.suppliers.registry import ADAPTERS, SupplierConfigError, build_supplier

log = logging.getLogger("app.procurement")


async def _serve(settings: ProcurementSettings) -> None:
    import uvicorn

    from app.core.db import make_engine, make_sessionmaker
    from app.core.memtrim import trim
    from app.core.redis import make_redis
    from app.procurement.context import ProcurementContext
    from app.procurement.credopen import load_credential_opener
    from app.procurement.engine import run_engine
    from app.procurement.internal import create_procurement_app
    from app.procurement.keys import SealerConfigError, build_sealer
    from app.procurement.service import run_loop
    from app.procurement.sms import run_sms_loop

    if len(settings.procurement_token) < 32:
        raise SystemExit("APP_PROCUREMENT_TOKEN (>= 32 chars) is required")
    try:
        sealer = build_sealer(
            public_keys_file=settings.vault_public_keys_file,
            active_kid=settings.vault_active_kid,
            fp_key=settings.vault_fingerprint_key,
        )
    except SealerConfigError as exc:
        raise SystemExit(f"procurement cannot seal: {exc}") from None
    try:
        cred_opener = load_credential_opener(settings.cred_private_keys_file)
    except SealerConfigError as exc:
        raise SystemExit(f"procurement cannot read credential keys: {exc}") from None
    if cred_opener is None:
        log.warning("no session private key: upstream_direct jobs cannot run")
    engine = make_engine(settings.procurement_database_url, pool_size=3, max_overflow=2)
    redis = make_redis(settings.redis_url)
    ctx = ProcurementContext(
        settings,
        make_sessionmaker(engine),
        redis,
        environ=os.environ,
        sealer=sealer,
        cred_opener=cred_opener,
    )
    stop = asyncio.Event()
    loop_task = asyncio.create_task(run_loop(ctx, stop), name="supplier-health")
    engine_task = asyncio.create_task(run_engine(ctx, stop), name="procurement-engine")
    sms_task = asyncio.create_task(run_sms_loop(ctx, stop), name="sms-loop")
    server = uvicorn.Server(
        uvicorn.Config(
            create_procurement_app(ctx),
            host=settings.procurement_host,
            port=settings.procurement_port,
            proxy_headers=False,
            server_header=False,
            access_log=False,
            log_config=None,
            lifespan="off",
            loop="asyncio",
            http="h11",
        )
    )
    log.info("procurement service on %s:%d", settings.procurement_host, settings.procurement_port)
    trim()
    redeem_app = None
    redeem_server = None
    if settings.redeem_enabled:
        from app.procurement.redeem import create_redeem_app

        # a SECOND listener: it serves only /redeem/*, the internal API is never on it
        redeem_app = create_redeem_app(ctx)
        redeem_server = uvicorn.Server(
            uvicorn.Config(
                redeem_app,
                host=settings.redeem_host,
                port=settings.redeem_port,
                proxy_headers=False,
                server_header=False,
                access_log=False,
                log_config=None,
                lifespan="off",
                loop="asyncio",
                http="h11",
                limit_concurrency=64,
                timeout_keep_alive=5,
            )
        )
        log.info("redeem proxy on %s:%d", settings.redeem_host, settings.redeem_port)
    try:
        if redeem_server is None:
            await server.serve()
        else:
            # stop both when either exits (uvicorn handles SIGTERM per server)
            async def _both() -> None:
                tasks = [
                    asyncio.create_task(server.serve()),
                    asyncio.create_task(redeem_server.serve()),
                ]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    server.should_exit = True
                    redeem_server.should_exit = True
                    await asyncio.gather(*tasks, return_exceptions=True)

            await _both()
    finally:
        if redeem_app is not None:
            with contextlib.suppress(Exception):
                await redeem_app.state.close()
        stop.set()
        for task in (loop_task, engine_task, sms_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for sup in ctx.suppliers.values():
            with contextlib.suppress(Exception):
                await sup.aclose()
        await redis.aclose()
        await engine.dispose()


async def probe_cli(
    code: str, env: str, adapter: str, app_env: str, environ: dict[str, str] | None = None
) -> ProbeReport:
    try:
        supplier = build_supplier(
            code=code,
            adapter=adapter,
            env=env,
            environ=os.environ if environ is None else environ,
            app_env=app_env,
            allow_production_writes=False,  # a probe only reads
        )
    except SupplierConfigError as exc:
        return ProbeReport(
            code,
            env,
            ok=False,
            errors=(f"not_configured: {exc}",),
            checked_at=datetime.now(timezone.utc),
        )
    try:
        return await supplier.probe()
    finally:
        await supplier.aclose()


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.procurement")
    sub = p.add_subparsers(dest="cmd")
    pr = sub.add_parser("probe", help="read-only connection test, prints a redacted report")
    pr.add_argument("--supplier", required=True, help="supplier code, e.g. zovocard")
    pr.add_argument("--env", required=True, choices=SUPPLIER_ENVS)
    pr.add_argument("--adapter", choices=ADAPTERS, help="defaults to the supplier code")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = ProcurementSettings.from_env()
    if args.cmd == "probe":
        configure_logging("WARNING")
        if not SUPPLIER_CODE_RE.fullmatch(args.supplier):
            print("invalid supplier code", file=sys.stderr)
            return 2
        adapter = args.adapter or args.supplier
        if adapter not in ADAPTERS:
            print("unknown adapter; pass --adapter", file=sys.stderr)
            return 2
        report = asyncio.run(probe_cli(args.supplier, args.env, adapter, settings.env))
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        return 0 if report.ok else 1
    configure_logging(settings.log_level)
    asyncio.run(_serve(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
