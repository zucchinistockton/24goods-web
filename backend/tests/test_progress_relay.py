"""Issue #30 (ADR-043): the procurement process publishes ids only on ONE channel; the API relays
the coarse buyer event. Real test Redis; failures, malformed and cross-tenant messages."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path

import pytest
import redis.asyncio as aioredis
from redis.exceptions import ResponseError

from app.modules.realtime.hub import RealtimeHub
from app.modules.realtime.progress import handle_message
from app.modules.realtime.publish import (
    user_channel,
)
from app.procurement import engine
from app.procurement.notify import PROC_PROGRESS_CHANNEL, notify_progress_changed
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


async def _buyer_id(env, order) -> uuid.UUID:
    return await env.scalar("SELECT buyer_id FROM orders WHERE id = :o", o=uuid.UUID(order["id"]))


async def _paid_direct(env, dup):  # noqa: F811
    await _zovo(dup, _vendor(), CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _drive(env, dup, order, buyer)
    return await _job(dup, order), order


def _hub(env) -> RealtimeHub:
    return RealtimeHub(env.s.redis, lambda raw: handle_message(env.res.maker, env.s.redis, raw))


async def test_procurement_publish_reaches_the_buyer_channel(env, dup):  # noqa: F811
    job, order = await _paid_direct(env, dup)
    buyer_id = await _buyer_id(env, order)
    hub = _hub(env)
    sub = hub.subscribe([user_channel(buyer_id)])
    await hub.wait_ready()
    # procurement side (real Redis) -> API subscriber -> buyer channel
    assert await notify_progress_changed(dup.ctx.redis, job.tenant_id, job.order_id) is True
    item = await asyncio.wait_for(sub.queue.get(), 3)
    assert item["event"] == "order_progress"
    assert item["data"] == {
        "order_id": order["id"],
        "step": "completed",
        "percent": 100,
        "outcome": "completed",
        "reason": None,
    }
    await hub.stop()


async def test_engine_pass_notifies_with_ids_only(env, dup, monkeypatch):  # noqa: F811
    job, order = await _paid_direct(env, dup)
    seen: list[bytes] = []
    pubsub = env.s.redis.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(PROC_PROGRESS_CHANNEL)
    await _sql(
        env, "UPDATE procurement_jobs SET progress = progress - 'pub' WHERE id = :j", j=job.id
    )
    await engine._publish_progress(dup.ctx, job.tenant_id, job.id)
    msg = None
    for _ in range(30):
        msg = await pubsub.get_message(timeout=0.1)
        if msg:
            seen.append(msg["data"])
            break
    await pubsub.aclose()
    assert seen and json.loads(seen[0]) == {
        "tenant_id": str(job.tenant_id),
        "order_id": str(job.order_id),
    }
    assert (await _job(dup, order)).progress["pub"] == "completed"


async def test_failed_publish_is_retried_on_the_next_pass(env, dup, monkeypatch):  # noqa: F811
    job, order = await _paid_direct(env, dup)
    await _sql(
        env, "UPDATE procurement_jobs SET progress = progress - 'pub' WHERE id = :j", j=job.id
    )

    class Down:
        async def publish(self, *a):
            raise ConnectionError("redis down")

    ctx = dup.ctx
    real = ctx.redis
    ctx.redis = Down()
    try:
        await engine._publish_progress(ctx, job.tenant_id, job.id)
    finally:
        ctx.redis = real
    assert "pub" not in ((await _job(dup, order)).progress or {})  # not marked: not lost
    await engine._publish_progress(ctx, job.tenant_id, job.id)
    assert (await _job(dup, order)).progress["pub"] == "completed"


async def test_malformed_and_cross_tenant_messages_are_ignored(env, dup):  # noqa: F811
    job, order = await _paid_direct(env, dup)
    buyer_id = await _buyer_id(env, order)
    maker, redis = env.res.maker, env.s.redis
    hub = _hub(env)
    sub = hub.subscribe([user_channel(buyer_id)])
    await hub.wait_ready()
    good = {"tenant_id": str(job.tenant_id), "order_id": str(job.order_id)}
    bad = [
        b"not json",
        b"[]",
        b"{}",
        json.dumps({**good, "order_id": "nope"}).encode(),
        json.dumps({**good, "tenant_id": 5}).encode(),
        json.dumps({"tenant_id": str(env.other_tenant_id), "order_id": good["order_id"]}).encode(),
        json.dumps({**good, "order_id": str(uuid.uuid4())}).encode(),
        b"x" * 5000,
        # a buyer id in the message is never trusted: nothing else of it is read
        json.dumps({**good, "buyer_id": str(uuid.uuid4()), "step": "completed"}).encode()[:0],
    ]
    for raw in bad:
        assert await handle_message(maker, redis, raw) is False
    for raw in bad:
        await redis.publish(PROC_PROGRESS_CHANNEL, raw)
    await asyncio.sleep(0.4)
    assert sub.queue.empty()
    # a forged extra field changes nothing: the event still goes to the REAL buyer
    forged = json.dumps({**good, "buyer_id": str(uuid.uuid4()), "step": "paid"})
    assert await handle_message(maker, redis, forged) is True
    item = await asyncio.wait_for(sub.queue.get(), 3)
    assert item["data"]["step"] == "completed"
    await hub.stop()


# ---- the Redis ACL of the procurement role ----------------------------------------------------
def _bootstrap_rule() -> str:
    text = (Path(__file__).resolve().parents[2] / "deploy" / "bootstrap.sh").read_text()
    m = re.search(r'^user procurement on #\$\(sha "[^"]*"\) (.*)$', text, re.M)
    assert m, "procurement ACL line missing"
    return m.group(1)


def test_bootstrap_procurement_acl_grants_exactly_one_channel():
    rule = _bootstrap_rule().split()
    channels = [t for t in rule if t.startswith("&")]
    assert channels == ["&rt:procprogress"]
    assert rule.index("resetchannels") < rule.index("&rt:procprogress")
    assert "+publish" in rule and "&*" not in rule and "allchannels" not in rule
    assert not any(t in rule for t in ("+@all", "+@pubsub", "+subscribe", "+psubscribe"))


async def test_acl_user_with_the_bootstrap_rule_can_publish_only_there(env):
    r = env.s.redis
    name = f"t_proc_{uuid.uuid4().hex[:8]}"
    try:
        await r.execute_command("ACL", "SETUSER", name, "on", ">pw", *_bootstrap_rule().split())
    except ResponseError as exc:
        pytest.skip(f"test Redis refuses ACL SETUSER: {exc}")
    kw = r.connection_pool.connection_kwargs
    c = aioredis.Redis(
        host=kw.get("host", "127.0.0.1"), port=kw.get("port", 6379), username=name, password="pw"
    )
    try:
        await c.publish(PROC_PROGRESS_CHANNEL, "{}")  # allowed
        with pytest.raises(ResponseError, match="No permissions"):
            await c.publish("rt:user:x", "{}")
        with pytest.raises(ResponseError, match="No permissions"):
            await c.publish("rt:staff:x", "{}")
    finally:
        await c.aclose()
        await r.execute_command("ACL", "DELUSER", name)


# ---- regex work is bounded ---------------------------------------------------------------------
ADVERSARIAL = {
    "dots": "a." * 32000,
    "run": "a" * 65536,
    "ats": "@" * 32000 + "a" * 32000,
    "mail": "a@" * 32000,
    "digits": "1 " * 32000,
    "dashes": "1" + "-" * 65000,
    "http": "http://" * 9000,
    "tld": "a-" * 32000 + ".com",
    "www": "www." * 16000,
}


@pytest.mark.parametrize("name", list(ADVERSARIAL))
def test_sanitiser_is_linear_on_adversarial_input(name):
    text = ADVERSARIAL[name]
    for fn in (dp.clean_message, dp.clean_code):
        t0 = time.perf_counter()
        fn(text)
        assert time.perf_counter() - t0 < 0.2, (fn.__name__, name)
    t0 = time.perf_counter()
    normalise(
        {"event_id": "e1", "type": "gpt_direct.updated", "public_message": text,
         "public_code": text, "step": text, "category": text, "payment_fact": text,
         "timeline_event_id": text},
        "s",
    )  # fmt: skip
    assert time.perf_counter() - t0 < 0.2


def test_webhook_progress_fields_are_restricted_and_capped():
    ev = normalise(
        {"event_id": "e1", "type": "gpt_direct.updated", "public_code": "a b<>" * 100,
         "step": {"x": 1}, "category": True, "payment_fact": "ok" * 100,
         "timeline_event_id": 7, "payment_state": "x" * 500},
        "s",
    )  # fmt: skip
    p = ev.payload
    assert len(p["public_code"]) <= 48 and re.fullmatch(r"[A-Za-z0-9_.:-]+", p["public_code"])
    assert p["step"] is None and p["category"] is None and len(p["payment_fact"]) <= 32
    assert p["timeline_event_id"] == "7" and len(p["payment_state"]) <= 32


# ---- recording is isolated; the buyer step never regresses -------------------------------------
async def test_corrupt_progress_never_aborts_a_transition(env, dup):  # noqa: F811
    await _zovo(dup, _vendor(), CLAUDE)
    buyer, order = await dup.paid_direct(claude_session(new_marker()))
    await _sql(
        env,
        "UPDATE procurement_jobs SET progress = CAST(:p AS jsonb) WHERE order_id = :o",
        p='{"events": 5, "seen": 7, "v": "junk", "raw": [1]}',
        o=uuid.UUID(order["id"]),
    )
    await _drive(env, dup, order, buyer)  # the transition to fulfilled still commits
    assert (await _job(dup, order)).state == "fulfilled"


def test_buyer_step_never_regresses():
    doc, _ = dp.record(None, {"status": "running", "stage": "dispatch", "events": []}, "poll")
    doc, _ = dp.record(doc, {"status": "queued", "events": []}, "webhook")  # late, lower
    doc, _ = dp.record(doc, {"status": "failed", "events": []}, "webhook")  # vendor failed
    assert doc["v"]["status"] == "running"
    assert [e.get("status") for e in doc["events"]] == ["running", "queued", "failed"]  # staff
    assert doc["raw"]["status"] == "failed"

    class J:
        state, error_code, upstream_ref, upstream_status = "accepted_pending", None, "r", "failed"
        first_sent_at, progress = None, doc

    assert dp.from_job(J()).step == "processing"
    J.state = "needs_manual"  # once OUR job ends the failure is shown with its reason
    p = dp.from_job(J())
    assert p.outcome == "failed" and p.reason == "not_completed"
    # a higher rank still advances
    doc, _ = dp.record(doc, {"status": "running", "stage": "paid", "events": []}, "poll")
    assert doc["v"]["stage"] == "paid"


def test_upstream_status_fallback_splits_the_fee_part():
    class J:
        state, error_code, upstream_ref, first_sent_at = "accepted_pending", None, "r", None
        upstream_status, progress = "running:charged", None

    assert dp.from_job(J()).step == "processing"


def test_newest_fifty_events_either_vendor_order():
    evs = [{"timeline_event_id": i, "occurred_at": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z",
            "public_code": f"c{i}"} for i in range(120)]  # fmt: skip
    for seq in (evs, evs[::-1]):
        out = dp.vendor_view({}, seq)["events"]
        assert [e["tid"] for e in out] == [str(i) for i in range(70, 120)]
    no_time = [{"id": i} for i in range(120)]
    for seq in (no_time, no_time[::-1]):
        out = dp.vendor_view({}, seq)["events"]
        assert [e["tid"] for e in out] == [str(i) for i in range(70, 120)]
