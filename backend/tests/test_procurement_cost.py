"""review-v12 M2: procurement cost report (ledger by day/supplier/type, margin, CSV)."""

from __future__ import annotations

# ruff: noqa: F811  (fixtures are imported from test_procurement_m2)
import csv
import io
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text

from app.suppliers.mock import MockSupplier
from tests.test_procurement_jobs import _foreign_job
from tests.test_procurement_m2 import Up, up  # noqa: F401

COST = "/api/v1/admin/procurement/cost"


def _t(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 11, day, hour, 0, tzinfo=timezone.utc)


async def _ledger(
    env, sid, ext: str, typ: str, amount: str, *, day: int, cur: str = "USD",
    job_id=None, tenant_id=None, card: str | None = None,
) -> None:  # fmt: skip
    tid = tenant_id or env.tenant_id
    async with env.owner_session(tid) as db:
        await db.execute(
            text(
                "INSERT INTO supplier_ledger (tenant_id, supplier_id, external_id, type, amount, "
                "currency, job_id, card_mask, occurred_at) VALUES (:t, :s, :e, :ty, :a, :c, :j, "
                ":cm, :o)"
            ),
            {"t": tid, "s": sid, "e": ext, "ty": typ, "a": Decimal(amount), "c": cur,
             "j": job_id, "cm": card, "o": _t(day)},
        )  # fmt: skip
        await db.commit()


async def _setup(env, up: Up):
    sid, _ = await up.supplier(MockSupplier("mock_a"))
    _b, order = await up.paid_order()
    await up.run()
    (job,) = await up.jobs(order["id"])
    return sid, order, job


async def test_summary_by_day_supplier_and_type(env, up):
    admin, _ = await env.login("admin")
    sid, _order, job = await _setup(env, up)
    await _ledger(env, sid, "1", "gpt_direct_cdk", "-5.5", day=10, job_id=job.id)
    await _ledger(env, sid, "2", "gpt_direct_cdk", "-4.5", day=10)
    await _ledger(env, sid, "3", "recharge", "50", day=11)
    await _ledger(env, sid, "4", "card_recharge", "-20", day=12, cur="EUR")
    r = await admin.get(
        f"{COST}/summary", params={"date_from": "2026-11-01", "date_to": "2026-11-30"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["group"] == "day" and body["date_from"] == "2026-11-01"
    by = {(i["key"], i["currency"]): i for i in body["items"]}
    assert by[("2026-11-10", "USD")] == {
        "key": "2026-11-10", "currency": "USD", "count": 2,
        "debit": "10", "credit": "0", "net": "-10",
    }  # fmt: skip
    assert (
        by[("2026-11-11", "USD")]["credit"] == "50" and by[("2026-11-12", "EUR")]["debit"] == "20"
    )
    assert next(i["key"] for i in body["items"]) == "2026-11-12"  # newest first
    bt = (
        await admin.get(
            f"{COST}/summary",
            params={"group": "type", "date_from": "2026-11-01", "date_to": "2026-11-30"},
        )
    ).json()
    assert {(i["key"], i["currency"]) for i in bt["items"]} == {
        ("gpt_direct_cdk", "USD"), ("recharge", "USD"), ("card_recharge", "EUR")
    }  # fmt: skip
    bs = (
        await admin.get(
            f"{COST}/summary",
            params={"group": "supplier", "date_from": "2026-11-01", "date_to": "2026-11-30"},
        )
    ).json()
    usd = next(i for i in bs["items"] if i["currency"] == "USD")
    assert usd["supplier_code"] == "mock_a" and usd["supplier_id"] == str(sid)
    assert usd["debit"] == "10" and usd["credit"] == "50" and usd["net"] == "40"
    # the date window and the supplier filter apply
    one = (
        await admin.get(
            f"{COST}/summary", params={"date_from": "2026-11-11", "date_to": "2026-11-11"}
        )
    ).json()
    assert [i["key"] for i in one["items"]] == ["2026-11-11"]
    none = (
        await admin.get(
            f"{COST}/summary",
            params={
                "supplier_id": str(uuid.uuid4()),
                "date_from": "2026-11-01",
                "date_to": "2026-11-30",
            },
        )
    ).json()
    assert none["items"] == []
    for bad in ({"date_from": "2026-11-12", "date_to": "2026-11-01"},
                {"date_from": "2020-01-01", "date_to": "2026-11-01"},
                {"group": "week"}):  # fmt: skip
        assert (await admin.get(f"{COST}/summary", params=bad)).status_code == 422, bad


async def test_margin_per_order_matches_currency_and_falls_back_to_job_cost(env, up):
    admin, _ = await env.login("admin")
    sid, order, job = await _setup(env, up)  # the product costs 50.00 CNY
    today = datetime.now(timezone.utc).date()
    window = {"date_from": f"{today.year}-01-01", "date_to": today.isoformat()}
    # no ledger yet: the cost the supplier reported when it issued (mock: 1.00 USD)
    r = await admin.get(f"{COST}/margin", params=window)
    assert r.status_code == 200, r.text
    (item,) = r.json()["items"]
    assert (
        item["order_id"] == order["id"]
        and item["revenue"] == "50"
        and item["revenue_currency"] == "CNY"
    )
    assert item["cost"] == [{"currency": "USD", "amount": "1", "source": "job"}]
    assert item["margin"] is None  # revenue and cost are in different currencies
    assert item["jobs"] == 1 and item["unreconciled_jobs"] == 1
    # a ledger row in the order's own currency decides the margin
    await _ledger(env, sid, "9", "gpt_direct_cdk", "-12.5", day=10, cur="CNY", job_id=job.id)
    await _ledger(env, sid, "10", "gpt_direct_refund", "2.5", day=11, cur="CNY", job_id=job.id)
    (item,) = (await admin.get(f"{COST}/margin", params=window)).json()["items"]
    assert {c["currency"]: c["amount"] for c in item["cost"]}["CNY"] == "10"
    assert item["unreconciled_jobs"] == 0
    assert item["margin"] == {"amount": "40", "currency": "CNY"}
    assert (await admin.get(f"{COST}/margin", params={**window, "page_size": 1, "page": 2})).json()[
        "items"
    ] == []


async def test_unreconciled_lists_unmatched_debits_only(env, up):
    admin, _ = await env.login("admin")
    sid, _order, job = await _setup(env, up)
    await _ledger(env, sid, "1", "gpt_direct_cdk", "-5", day=10, job_id=job.id)  # matched
    await _ledger(env, sid, "2", "card_recharge", "-7", day=11, card="414700****5678")
    await _ledger(env, sid, "3", "open_card", "-1", day=12)
    await _ledger(env, sid, "4", "recharge", "50", day=13)  # a credit is not a cost
    r = await admin.get(f"{COST}/unreconciled")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2 and [i["external_id"] for i in body["items"]] == ["3", "2"]
    assert body["items"][1]["card_mask"] == "414700****5678" and body["items"][1]["amount"] == "-7"
    assert body["items"][0]["supplier_code"] == "mock_a/sandbox"
    page = (await admin.get(f"{COST}/unreconciled", params={"page": 2, "page_size": 1})).json()
    assert [i["external_id"] for i in page["items"]] == ["2"]


async def test_csv_export_guards_formulas_and_audits(env, up):
    admin, _ = await env.login("admin")
    sid, _order, _job = await _setup(env, up)
    await _ledger(env, sid, "=1+1", '=HYPERLINK("http://x")', "-5.5", day=10, card="@cmd")
    await _ledger(env, sid, "2", "recharge", "50", day=11)
    r = await admin.get(
        f"{COST}/export.csv", params={"date_from": "2026-11-01", "date_to": "2026-11-30"}
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][:5] == ["occurred_at", "supplier", "external_id", "type", "amount"]
    by = {row[2].lstrip("'"): row for row in rows[1:]}
    evil = by["=1+1"]
    assert evil[2].startswith("'=") and evil[3].startswith("'=") and evil[7] == "'@cmd"
    assert evil[4] == "-5.5" and evil[5] == "USD"  # an amount is a number, not a formula cell
    assert by["2"][4] == "50"
    assert (
        await env.scalar(
            "SELECT count(*) FROM audit_log WHERE action = 'procurement.cost_exported'"
        )
        == 1
    )


async def test_cost_report_roles_and_cross_tenant_isolation(env, up):
    admin, _ = await env.login("admin")
    agent, _ = await env.login("agent")
    buyer, _ = await env.login("buyer")
    sid, order, _job = await _setup(env, up)
    await _ledger(env, sid, "1", "card_recharge", "-3", day=10)
    # another tenant's supplier, ledger rows and job
    from tests.test_procurement_v12 import _insert_supplier

    fsid = await _insert_supplier(env, env.other_tenant_id)
    await _ledger(env, fsid, "77", "card_recharge", "-999", day=10, tenant_id=env.other_tenant_id)
    await _ledger(env, fsid, "78", "recharge", "123456", day=10, tenant_id=env.other_tenant_id)
    await _foreign_job(env)
    q = {"date_from": "2026-11-01", "date_to": "2026-11-30"}
    for path in ("/summary", "/margin", "/unreconciled", "/export.csv"):
        for c in (agent, buyer):
            assert (await c.get(COST + path, params=q)).status_code == 403, path
    s = (await admin.get(f"{COST}/summary", params={**q, "group": "supplier"})).json()
    assert [i["supplier_id"] for i in s["items"]] == [str(sid)]
    assert "999" not in str(s) and "123456" not in str(s)
    u = (await admin.get(f"{COST}/unreconciled")).json()
    assert u["total"] == 1 and u["items"][0]["external_id"] == "1"
    csv_text = (await admin.get(f"{COST}/export.csv", params=q)).text
    assert "999" not in csv_text and "123456" not in csv_text and "77" not in csv_text
    today = datetime.now(timezone.utc).date()
    window = {"date_from": f"{today.year}-01-01", "date_to": today.isoformat()}
    m = (await admin.get(f"{COST}/margin", params=window)).json()
    assert [i["order_id"] for i in m["items"]] == [order["id"]]
    # a supplier filter for another tenant's supplier yields nothing
    none = (await admin.get(f"{COST}/summary", params={**q, "supplier_id": str(fsid)})).json()
    assert none["items"] == []
