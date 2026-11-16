"""Payment-audit and security-review fixes on v12 M3 ``upstream_vcc`` (2026-11-16)."""

from __future__ import annotations

# ruff: noqa: F811  (fixtures are imported from the M2 / vcc tests)
import uuid

import pytest  # noqa: F401

from app.devtools.mock_zovocard import MockZovo
from tests.test_aftersales import open_ticket
from tests.test_procurement_m2 import Up, _status, up  # noqa: F401
from tests.test_procurement_vcc import (
    OPEN,
    _card,
    _deliver,
    _job,
    _zovo,
    vup,  # noqa: F401
)


async def _refund(env, order):
    admin, _ = await env.login("admin")
    return await admin.post(
        f"/api/v1/admin/orders/{order['id']}/refund",
        json={"reference": "REF-" + uuid.uuid4().hex[:8].upper()},
    )


async def _settle(vup: Up, rounds: int = 6) -> None:
    for _ in range(rounds):
        await vup.make_due()
        await vup.run()


# ------------------------------------------------------------------ F1


async def test_refund_of_a_delivered_card_order_deletes_the_card(env, vup):
    vendor = MockZovo()
    await _zovo(vup, vendor, OPEN)
    buyer, order = await vup.paid_order()
    await _deliver(env, vup, order, buyer)
    assert _card(vendor)["status"] == "ACTIVE"
    t = await open_ticket(buyer, order)
    assert t.status_code in (200, 201), t.text
    admin, _ = await env.login("admin")
    r = await admin.post(
        f"/api/v1/admin/after-sales/{t.json()['id']}/action",
        json={"action": "refund", "note": "refunded", "reference": "REF-" + uuid.uuid4().hex[:8]},
    )
    assert r.status_code == 200, r.text
    job = await _job(vup, order)
    assert job.state == "void_pending"
    await _settle(vup)
    job = await _job(vup, order)
    assert job.state == "voided" and job.upstream_status == "deleted"
    assert _card(vendor)["status"] == "DELETED"
    assert await env.scalar(
        "SELECT count(*) FROM card_stock WHERE order_id = :o AND status = 'void'",
        o=uuid.UUID(order["id"]),
    ) == 1  # fmt: skip
    assert await env.scalar(
        "SELECT count(*) FROM audit_log WHERE action = 'procurement.delivered_card_voided' "
        "AND severity = 'critical'"
    ) == 1  # fmt: skip
    # the buyer can no longer reveal the dead card
    assert (await buyer.post(f"/api/v1/orders/{order['id']}/reveal")).status_code in (409, 403)
