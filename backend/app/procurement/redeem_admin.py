"""Redemption admin in the procurement process (issue #31, review-v12 §23).

The console never holds vendor keys: it sends the pasted CDK to the internal API, which lands here.
The code lives in memory for the one vendor call and is never logged, stored or echoed. Reads use
``GET /gpt-direct/cdks`` (the vendor's own list, ``q`` search) and compare the code locally; the two
writes (``disable`` / ``enable``) are refused for a production supplier unless
``APP_PROCUREMENT_ALLOW_PRODUCTION_WRITES`` is on (checked here first; the client blocks it again as
a backstop).

[I] The list item shape is inferred from the doc's own words ("full code" in the list): ``id``,
``code``, ``status``, ``plan``. Statuses documented: unused, reserved, consumed, review, frozen,
disabled. ``enable`` is documented as "restore a code the account may enable to ``unused``".
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
import uuid
from typing import Any

from sqlalchemy import text

from app.core.db import tenant_session
from app.procurement import cardpool
from app.procurement import redeem_log as rl
from app.procurement.context import ProcurementContext
from app.suppliers.base import SupplierError
from app.suppliers.zovocard.client import sanitize_error_code

log = logging.getLogger("app.procurement.redeem_admin")

CODE_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
STATUS_RE = re.compile(r"^[a-z_]{1,24}$")


class BadCode(ValueError):
    """The pasted text is not a CDK."""


class CdkRejected(Exception):
    """The vendor refused the disable / enable (already used, not disabled, ...). Carries only a
    short machine code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def sweep_attempts(ctx: ProcurementContext) -> int:
    """Delete log rows older than the retention (unpinned session, ``procurement_sweep``)."""
    try:
        async with tenant_session(ctx.maker, None) as db:
            res = await db.execute(
                text("DELETE FROM redeem_attempts WHERE at < now() - make_interval(days => :d)"),
                {"d": rl.RETENTION_DAYS},
            )
            await db.commit()
            return int(res.rowcount or 0)  # type: ignore[attr-defined]
    except Exception as exc:
        log.error("redeem log sweep failed error=%s", type(exc).__name__)
        return 0


def _status(value: Any) -> str:
    s = str(value).lower() if isinstance(value, str) else ""
    return s if STATUS_RE.fullmatch(s) else "unknown"


async def _find(client: Any, code: str, timeout: float) -> dict[str, Any] | None:
    """The vendor's CDK row for exactly this code, reduced to ``{id, status, plan}``."""
    if not CODE_RE.fullmatch(code):
        raise BadCode
    resp = await asyncio.wait_for(
        client.get("/gpt-direct/cdks", {"q": code, "page": "1", "page_size": "10"}), timeout
    )
    data = resp.data if isinstance(resp.data, dict) else {}
    for item in data.get("list") or []:
        if not isinstance(item, dict):
            continue
        got = item.get("code")
        if isinstance(got, str) and hmac.compare_digest(got.encode(), code.encode()):
            cid = item.get("id")
            if isinstance(cid, bool) or not isinstance(cid, (int, str)) or not str(cid).isdigit():
                return None
            plan = item.get("plan")
            return {
                "id": int(cid),
                "status": _status(item.get("status")),
                "plan": sanitize_error_code(plan) if isinstance(plan, str) else None,
            }
    return None


async def cdk_status(ctx: ProcurementContext, tenant_id: uuid.UUID, code: str) -> dict[str, Any]:
    """Read-only: ``{found, id?, status?, plan?, supplier, env}``."""
    ref, supplier, client = await cardpool._client(ctx, tenant_id, None)
    try:
        row = await _find(client, code, ctx.settings.probe_timeout_s)
    finally:
        await supplier.aclose()
    return {"found": row is not None, **(row or {}), "supplier": str(ref.id), "env": ref.env}


async def cdk_set(
    ctx: ProcurementContext, tenant_id: uuid.UUID, code: str, action: str
) -> dict[str, Any]:
    """Disable or enable the vendor CDK for ``code``. Returns ``{before, after, id, supplier, env}``
    for the audit row (statuses only, never the code)."""
    if action not in ("disable", "enable"):
        raise ValueError("unknown action")
    ref, supplier, client = await cardpool._client(ctx, tenant_id, None, write=True)
    try:
        cardpool._guard(ctx, ref.env, client)
        row = await _find(client, code, ctx.settings.probe_timeout_s)
        if row is None:
            raise LookupError("cdk not found")
        try:
            resp = await asyncio.wait_for(
                client.request(
                    "POST",
                    f"/gpt-direct/cdks/{row['id']}/{action}",
                    json_body={},
                    idempotency_key=f"redeem-admin:{action}:{uuid.uuid4().hex}",
                ),
                ctx.settings.probe_timeout_s,
            )
        except SupplierError as exc:
            raise CdkRejected(sanitize_error_code(exc.error_code) or "rejected") from None
        d = resp.data if isinstance(resp.data, dict) else {}
        after = _status(d.get("status")) if d.get("status") else None
        if after is None:  # re-read: the answer did not say
            again = await _find(client, code, ctx.settings.probe_timeout_s)
            after = again["status"] if again else "unknown"
        return {
            "id": row["id"],
            "before": row["status"],
            "after": after,
            "supplier": str(ref.id),
            "env": ref.env,
        }
    finally:
        await supplier.aclose()
