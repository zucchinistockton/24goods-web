"""Issue #30 (review-v12 §22): progress model, staff timeline and buyer view of direct orders.

Unit part: the mapping table, sanitising, recording / dedupe / cap, the webhook whitelist and the
redemption proxy's ``progress``. Integration part (real engine against the mock vendor): the
timeline rows, the buyer view, secret sweep, cross-tenant 404 and the realtime publish."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from app.procurement import engine, redeem
from app.suppliers import direct_progress as dp
from app.suppliers.zovocard.webhook import normalise
from tests.test_procurement_direct import (
    CLAUDE,
    _drive,
    _job,
    _vendor,
    _zovo,
    claude_session,
    dup,  # noqa: F401  (fixture)
    new_marker,
)
from tests.test_procurement_vcc import _sql

JOBS = "/api/v1/admin/procurement/jobs"


# ------------------------------------------------------------------ the mapping table
def _m(job_state="accepted_pending", **kw):
    kw.setdefault("sent", True)
    return dp.map_progress(job_state=job_state, **kw)


@pytest.mark.parametrize(
    ("kw", "step", "percent", "outcome", "reason"),
    [
        # our own steps
        (dict(job_state=None), "paid", 10, "active", None),
        (dict(job_state="queued", sent=False), "paid", 10, "active", None),
        (dict(job_state="queued", sent=False, half=True), "half_check", 15, "active", None),
        (
            dict(job_state="queued", error_code="waiting_for_card", sent=False),
            "waiting_supplier",
            20,
            "active",
            None,
        ),
        (dict(job_state="submitting"), "sent", 30, "active", None),
        # the vendor's steps (documented statuses)
        (dict(status="queued"), "queued", 40, "active", None),
        (dict(status="awaiting_supplier", stage="cdk_accepted"), "queued", 40, "active", None),
        (dict(status="funding_pending"), "funding", 55, "active", None),
        (dict(status="dispatching"), "processing", 70, "active", None),
        (dict(status="running"), "processing", 70, "active", None),
        (dict(status="requires_action"), "processing", 70, "active", None),
        (dict(status="plus_paid"), "payment_submitted", 85, "active", None),
        (
            dict(status="running", payment_state="submitted"),
            "payment_submitted",
            85,
            "active",
            None,
        ),
        (dict(status="queued", funding_state="card_ready"), "processing", 70, "active", None),
        # the vendor's "completed" is a hint: the buyer sees completion once OUR job is fulfilled
        (dict(status="completed"), "payment_submitted", 85, "active", None),
        (dict(job_state="fulfilled", status="completed"), "completed", 100, "completed", None),
        # failures keep the percent of the furthest step
        (
            dict(job_state="needs_manual", status="declined", stage="running"),
            "failed",
            70,
            "failed",
            "declined",
        ),
        (dict(job_state="needs_manual", status="cancelled"), "failed", 40, "failed", "cancelled"),
        (dict(job_state="failed", status="failed_precharge"), "failed", 40, "failed", "declined"),
        (dict(job_state="failed"), "failed", 30, "failed", "not_completed"),
        (
            dict(job_state="voided", order_refunded=True, status="running"),
            "refunded",
            70,
            "failed",
            "refunded",
        ),
        # needs attention: coarse reason only
        (
            dict(job_state="needs_manual", error_code="pending_too_long", status="running"),
            "attention",
            70,
            "attention",
            "review",
        ),
        (
            dict(job_state="needs_manual", error_code="session_expired", sent=False),
            "attention",
            10,
            "attention",
            "account",
        ),
    ],
)
def test_mapping_table(kw, step, percent, outcome, reason):
    p = dp.map_progress(**{"job_state": "accepted_pending", "sent": True, **kw})
    assert (p.step, p.percent, p.outcome, p.reason) == (step, percent, outcome, reason)
    assert p.terminal is (outcome == "completed" or step in ("failed", "refunded"))


@pytest.mark.parametrize(
    "vendor",
    [
        dict(status="martian"),
        dict(status="martian", stage="x" * 500, payment_state="??", funding_state="\x00"),
        dict(status=12345, stage=["list"], payment_state={"a": 1}, funding_state=True),
        dict(status="", stage=None),
    ],
)
def test_unknown_vendor_values_map_to_a_safe_step(vendor):
    p = _m(**vendor)
    # never success, never failure, always a known step with a sane percent
    assert p.step in (*dp.VENDOR_STEPS[:-1], "sent")
    assert p.outcome == "active" and not p.terminal and 0 < p.percent < 100
    assert p.step != "payment_submitted" or "paid" in str(vendor)


def test_unknown_stage_text_is_never_trusted_as_completion():
    assert _m(status="queued", stage="completed").step != "completed"
    assert dp.vendor_rank("nonsense", "completed") != "completed"


def test_buyer_view_is_coarse_and_marks_the_current_step():
    v = dp.buyer_view(_m(status="funding_pending"), at={"paid": "2026-11-17T10:00:00Z"})
    assert set(v) == {"step", "percent", "outcome", "terminal", "reason", "steps", "completed_at"}
    states = {s["key"]: s["state"] for s in v["steps"]}
    assert states["paid"] == "done" and states["funding"] == "current"
    assert states["completed"] == "todo" and v["percent"] == 55
    assert {k for s in v["steps"] for k in s} == {"key", "state", "at"}
    bad = dp.buyer_view(_m("needs_manual", status="declined", stage="running"))
    assert bad["steps"][-1] == {"key": "failed", "state": "failed", "at": None}
    assert all(s["state"] != "todo" for s in bad["steps"])
    done = dp.buyer_view(_m("fulfilled", status="completed"), at={"completed": "T"})
    assert done["completed_at"] == "T" and all(s["state"] == "done" for s in done["steps"])


# ------------------------------------------------------------------ sanitising
def test_public_message_is_sanitised():
    raw = "Paid\x00 ok\r\n\u202e visit https://evil.example/x?t=abc and www.evil.test, mail a@b.co"
    out = dp.clean_message(raw + " card 5378 7212 3456 7890 token " + "A" * 40)
    assert out is not None
    assert "http" not in out and "evil" not in out and "@" not in out and "\x00" not in out
    assert "\n" not in out and "\u202e" not in out
    assert "5378" not in out and "AAAA" not in out and "****" in out
    assert len(dp.clean_message("x " * 500) or "") <= dp.MSG_CAP
    assert dp.clean_message(None) is None and dp.clean_message(123) is None
    assert dp.clean_message("   \x01  ") is None
    assert dp.clean_code("a b/c\n" + "z" * 100) == ("a_b_c_" + "z" * 100)[:48]


def test_vendor_view_is_a_whitelist():
    order = {
        "status": "running", "stage": "dispatch", "payment_state": "pending",
        "funding_state": "card_ready", "account_email": "x@y.z", "card_number": "5378720000001234",
        "credential": {"session": "SECRETSESSION"},
    }  # fmt: skip
    events = [
        {"timeline_event_id": 7, "category": "payment", "step": "submit", "public_code": "PAY_1",
         "public_message": "Submitted https://x.example", "to_status": "running",
         "payment_fact": "submitted", "otp": "123456", "card_number": "5378720000001234"},
        {"public_message": "no id: dropped"},
        "junk",
    ]  # fmt: skip
    out = dp.vendor_view(order, events)
    blob = json.dumps(out)
    for leak in ("x@y.z", "5378720000001234", "SECRETSESSION", "123456", "https", "otp"):
        assert leak not in blob
    assert out["status"] == "running" and out["funding_state"] == "card_ready"
    assert out["events"] == [
        {"tid": "7", "cat": "payment", "step": "submit", "code": "PAY_1", "msg": "Submitted",
         "to": "running", "fact": "submitted"}
    ]  # fmt: skip


def test_webhook_progress_fields_are_kept_and_the_message_sanitised():
    ev = normalise(
        {
            "type": "gpt_direct.event", "event_id": "e1", "order_id": 9, "status": "running",
            "stage": "dispatch", "timeline_event_id": 5, "public_code": "STEP_A",
            "public_message": "Go to https://phish.example now", "payment_fact": "none",
            "card_number": "5378720000001234", "code_prefix": "ZC-AAAA",
        },
        "s",
    )  # fmt: skip
    assert ev.payload["public_message"] == "Go to now" and ev.payload["timeline_event_id"] == "5"
    assert "card_number" not in ev.payload and "code_prefix" not in ev.payload
    v = dp.vendor_from_webhook(ev.payload)
    assert v["events"][0]["tid"] == "5" and v["status"] == "running"


# ------------------------------------------------------------------ recording / dedupe / cap
def test_record_adds_only_changes_and_dedupes():
    doc, n = dp.record(None, {"status": "queued", "stage": "cdk_accepted", "events": []}, "poll")
    assert n == 1 and doc["events"][0]["src"] == "poll" and doc["v"]["status"] == "queued"
    again, n = dp.record(doc, {"status": "queued", "stage": "cdk_accepted", "events": []}, "poll")
    assert n == 0 and again["events"] == doc["events"]  # nothing changed
    doc, n = dp.record(doc, {"status": "running", "stage": "cdk_accepted", "events": []}, "webhook")
    assert n == 1 and doc["events"][-1]["src"] == "webhook" and doc["v"]["status"] == "running"
    # a webhook without payment_state keeps the last known one
    doc, _ = dp.record(doc, {"payment_state": "pending", "events": []}, "poll")
    assert doc["v"] == {"status": "running", "stage": "cdk_accepted", "payment_state": "pending"}
    doc2, n = dp.record(doc, {"status": "running", "events": []}, "webhook")
    assert n == 0 and doc2["v"]["payment_state"] == "pending"


def test_vendor_timeline_events_dedupe_by_id():
    ev = {"tid": "11", "code": "A", "msg": "m"}
    doc, n = dp.record(None, {"events": [ev]}, "poll")
    assert n == 1
    doc, n = dp.record(doc, {"events": [ev, {"tid": "12", "code": "B"}]}, "poll")
    assert n == 1 and [e.get("tid") for e in doc["events"]] == ["11", "12"]
    doc, n = dp.record(doc, {"events": [ev, {"tid": "12", "code": "B"}]}, "webhook")
    assert n == 0


def test_entries_are_capped():
    doc: dict = {}
    for i in range(dp.MAX_ENTRIES + 60):
        doc, _ = dp.record(doc, {"events": [{"tid": str(i), "code": "C"}]}, "poll")
    assert len(doc["events"]) == dp.MAX_ENTRIES and len(doc["seen"]) <= dp.MAX_SEEN
    assert doc["events"][-1]["tid"] == str(dp.MAX_ENTRIES + 59)
    # an id that fell off the entry list is still known: no duplicate
    doc, n = dp.record(doc, {"events": [{"tid": "0", "code": "C"}]}, "poll")
    assert n == 0


# ------------------------------------------------------------------ redemption proxy
def test_redeem_result_carries_the_same_step_model():
    shaped = redeem.shape_result(
        {
            "order": {"id": 5, "status": "running", "stage": "dispatch",
                      "payment_state": "submitted", "completed_at": None,
                      "card_number": "5378720000001234"},
            "events": [{"type": "x", "status": "running", "created_at": "2026-11-17T10:00:00Z",
                        "public_message": "https://x.example"}],
        }
    )  # fmt: skip
    prog = shaped["progress"]
    assert prog["step"] == "payment_submitted" and prog["percent"] == 85
    assert next(s["key"] for s in prog["steps"]) == "sent"  # the redemption starts at "sent"
    assert "5378" not in json.dumps(shaped) and "https" not in json.dumps(shaped)
    done = redeem.shape_result(
        {"order": {"id": 5, "status": "completed", "completed_at": 1784397600}}
    )
    assert (
        done["progress"]["outcome"] == "completed"
        and done["progress"]["completed_at"] == 1784397600
    )
    failed = redeem.shape_result({"order": {"id": 5, "status": "declined", "stage": "running"}})
    assert failed["progress"]["outcome"] == "failed" and failed["progress"]["reason"] == "declined"
    odd = redeem.shape_result({"order": {"status": "martian"}})
    assert odd["progress"]["outcome"] == "active"
    assert (
        redeem.shape_redeem({"id": 3, "status": "awaiting_supplier", "stage": "cdk_accepted"})[
            "progress"
        ]["step"]
        == "queued"
    )


# ------------------------------------------------------------------ engine + API
async def _info(buyer, order):
    r = await buyer.get(f"/api/v1/orders/{order['id']}/direct")
    assert r.status_code == 200, r.text
    return r.json()


async def test_timeline_buyer_view_secrets_and_publish(env, dup, monkeypatch):  # noqa: F811
    marker = new_marker()
    vendor = _vendor()
    vendor.direct_events = [
        {"timeline_event_id": 1, "category": "payment", "step": "pay",
         "public_code": "VENDOR_PAY_9",
         "public_message": "Paying via https://pay.example/x?tok=1 card 5378720000001234",
         "to_status": "running", "payment_fact": "submitted"},
    ]  # fmt: skip
    vendor.direct_order_over = {"payment_state": "pending", "funding_state": "card_ready"}
    published: list[dict] = []

    async def spy(redis, tenant_id, order_id):
        published.append({"tenant_id": str(tenant_id), "order_id": str(order_id)})
        return True

    monkeypatch.setattr(engine, "notify_progress_changed", spy)
    await _zovo(dup, vendor, CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(marker))
    await _drive(env, dup, order, buyer)
    job = await _job(dup, order)
    assert job.state == "fulfilled"

    # staff timeline: changes only, the vendor event once, sanitised, no vendor body
    doc = job.progress
    events = doc["events"]
    assert next(e["src"] for e in events) == "poll"
    assert sum(1 for e in events if e.get("tid") == "1") == 1  # polled several times, one entry
    ev = next(e for e in events if e.get("tid") == "1")
    assert ev["code"] == "VENDOR_PAY_9" and "http" not in ev["msg"] and "5378" not in ev["msg"]
    statuses = [e.get("status") for e in events if "status" in e and "tid" not in e]
    assert statuses == list(dict.fromkeys(statuses))  # no identical consecutive entries
    assert doc["v"]["funding_state"] == "card_ready"

    # staff job view
    staff, _ = await env.login("agent")
    detail = (await staff.get(f"{JOBS}/{job.id}")).json()
    assert detail["supplier_timeline"] == events and detail["buyer_progress"]["step"] == "completed"
    assert detail["vendor_state"]["payment_state"] == "pending"

    # buyer view: coarse keys only
    info = await _info(buyer, order)
    prog = info["progress"]
    assert prog["outcome"] == "completed" and prog["percent"] == 100 and prog["completed_at"]
    assert {k for s in prog["steps"] for k in s} == {"key", "state", "at"}
    blob = json.dumps(info)
    for leak in (
        "VENDOR_PAY_9",
        "pay.example",
        "5378",
        "card_ready",
        "fee",
        marker,
        "needs_manual",
    ):
        assert leak not in blob, leak

    # secret sweep: the stored document and both responses never hold the session
    assert marker not in json.dumps(doc) and marker not in json.dumps(detail)

    # the notification carries ids only and goes out on a step change only
    assert published and all(set(p) == {"tenant_id", "order_id"} for p in published)
    assert (await _job(dup, order)).progress["pub"] == "completed"
    n = len(published)
    before = (await _job(dup, order)).updated_at
    await engine._publish_progress(dup.ctx, job.tenant_id, job.id)  # nothing changed
    assert len(published) == n
    assert (await _job(dup, order)).updated_at == before  # no write on an idle pass
    await _sql(env, "UPDATE procurement_jobs SET state = 'failed' WHERE id = :j", j=job.id)
    await engine._publish_progress(dup.ctx, job.tenant_id, job.id)  # the step changed
    assert len(published) == n + 1
    assert (await _job(dup, order)).progress["pub"] == "failed"


async def test_webhook_records_a_timeline_entry(env, dup):  # noqa: F811
    vendor = _vendor()
    await _zovo(dup, vendor, CLAUDE)
    _buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await dup.run()
    job = await _job(dup, order)
    assert job.state == "accepted_pending"
    from app.core.db import tenant_session
    from app.models import SupplierWebhookEvent

    eid = uuid.uuid4()
    async with tenant_session(dup.ctx.maker, job.tenant_id) as db:
        db.add(
            SupplierWebhookEvent(
                id=eid, tenant_id=job.tenant_id, supplier_id=job.supplier_id,
                dedup_key="e:" + eid.hex, kind="gpt_direct.progress",
                payload={"order_id": job.upstream_ref, "status": "funding_pending",
                         "stage": "funding", "event_id": str(eid)},
            )
        )  # fmt: skip
        await db.commit()
    async with tenant_session(dup.ctx.maker, job.tenant_id) as db:
        await engine.handle_event(dup.ctx, db, {"event_id": str(eid)})
        await db.commit()
    job = await _job(dup, order)
    last = job.progress["events"][-1]
    assert last["src"] == "webhook" and last["status"] == "funding_pending"
    assert "event_id" not in json.dumps(job.progress)


async def test_cross_tenant_timeline_and_progress_are_404(env, dup):  # noqa: F811
    vendor = _vendor()
    await _zovo(dup, vendor, CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    job = await _job(dup, order)
    # another tenant's direct job (with its own timeline) is invisible to this tenant's staff
    fj = await _foreign_direct_job(env)
    staff, _ = await env.login("agent")
    assert (await staff.get(f"{JOBS}/{fj}")).status_code == 404
    assert (await staff.get(f"{JOBS}/{job.id}")).status_code == 200
    # RLS: the other tenant's role cannot read this tenant's timeline whatever it asks
    async with env.api_session(env.other_tenant_id) as db:
        assert (
            await db.scalar(
                text("SELECT count(*) FROM procurement_jobs WHERE id = :j"), {"j": job.id}
            )
            == 0
        )
        assert (
            await db.scalar(
                text("SELECT progress FROM procurement_jobs WHERE id = :j"), {"j": job.id}
            )
            is None
        )
    # another buyer of the SAME tenant gets no progress of this order either
    peer, _ = await env.login("buyer")
    assert (await peer.get(f"/api/v1/orders/{order['id']}/direct")).status_code == 404
    assert (await buyer.get(f"/api/v1/orders/{order['id']}/direct")).status_code == 200


async def _foreign_direct_job(env) -> uuid.UUID:
    buyer = await env.user("buyer", tenant_id=env.other_tenant_id)
    oid, jid = uuid.uuid4(), uuid.uuid4()
    async with env.owner_session(env.other_tenant_id) as db:
        p = await env.product(mode="upstream_direct", tenant_id=env.other_tenant_id)
        await db.execute(
            text(
                "INSERT INTO orders (id, tenant_id, order_no, buyer_id, product_id, title, qty, "
                "amount_minor, subtotal_minor, currency, status, delivery_mode) VALUES "
                "(:i, :t, :n, :b, :p, 'x', 1, 100, 100, 'CNY', 'paid', 'upstream_direct')"
            ),
            {"i": oid, "t": env.other_tenant_id, "n": "G" + uuid.uuid4().hex[:12],
             "b": buyer.id, "p": p.id},
        )  # fmt: skip
        await db.execute(
            text(
                "INSERT INTO procurement_jobs (id, tenant_id, order_id, unit_no, kind, state, step,"
                " idempotency_key, progress) VALUES (:j, :t, :o, 1, 'direct', 'accepted_pending', "
                "'pending', :k, CAST(:pr AS jsonb))"
            ),
            {"j": jid, "t": env.other_tenant_id, "o": oid, "k": "pj:" + jid.hex + ":submit",
             "pr": json.dumps({"events": [{"at": "T", "src": "poll", "status": "running"}]})},
        )  # fmt: skip
        await db.commit()
    return jid
