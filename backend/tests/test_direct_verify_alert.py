"""Telegram + staff alert when a ZovoCard direct order waits for the Stripe verification."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core import notify_tg
from app.models import ProcurementJob, SupplierAccount
from app.modules.webhooks import needs_verification, verify_deadline
from app.suppliers import direct_progress as dp
from app.suppliers.zovocard.webhook import normalise
from tests.helpers import buy
from tests.test_telegram_v52 import TOKEN, Telegram, _enable

SECRET = "whsec_" + "a" * 32
HOOK = "/api/v1/webhooks/zovocard"
MESSAGE = "支付需完成人机验证，请在订单页点「完成验证」按钮操作"


def _signed(body: dict | list) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Signature": sig, "Content-Type": "application/json"}


def _event(n: int = 1, **over) -> dict:
    exp = (datetime.now(timezone.utc) + timedelta(seconds=170)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev = {
        "event_id": f"gpt_direct.progress:{n}",
        "type": "gpt_direct.progress",
        "order_id": 4242,
        "cdk_id": 4242,
        "status": "pending",
        "stage": "native_captcha",
        "public_code": "awaiting_native_captcha",
        "public_message": MESSAGE,
        "captcha_kind": "stripe_3ds",
        "captcha_state": "pending",
        "captcha_expires_at": exp,
    }
    ev.update(over)
    return ev


@pytest.fixture
async def hook(env, monkeypatch):
    env.res.settings = env.res.settings.model_copy(
        update={"zovocard_sandbox_webhook_secret": SECRET}
    )
    async with env.owner_session() as db:
        db.add(
            SupplierAccount(
                id=uuid.uuid4(),
                tenant_id=env.tenant_id,
                code="zovocard",
                adapter="zovocard",
                label="z",
                env="sandbox",
                settings={},
            )
        )
        await db.commit()
    t = Telegram(env.res.http.post)
    monkeypatch.setattr(env.res.http, "post", t.post)
    admin, _ = await env.login("admin")
    await _enable(env, admin)
    yield t
    await notify_tg.drain()


async def _send(env, body) -> int:
    raw, headers = _signed(body)
    r = await env.client(csrf=False).post(HOOK, content=raw, headers=headers)
    await notify_tg.drain()
    return r.status_code


async def _our_job(env, order, tenant_id=None, upstream_ref="4242"):
    async with env.owner_session(tenant_id) as db:
        db.add(
            ProcurementJob(
                tenant_id=tenant_id or env.tenant_id,
                order_id=uuid.UUID(order["id"]),
                unit_no=1,
                kind="direct",
                idempotency_key=f"pj:{uuid.uuid4().hex}:submit",
                upstream_ref=upstream_ref,
            )
        )
        await db.commit()


# ------------------------------------------------------------------ normalisation / pure helpers
def test_payload_keeps_the_captcha_fields_as_capped_tokens():
    ev = normalise(_event(captcha_kind="stripe_3ds" + "x" * 80, captcha_state="pending"), SECRET)
    assert ev.payload["captcha_state"] == "pending"
    assert len(ev.payload["captcha_kind"]) == 32
    assert isinstance(ev.payload["captcha_expires_at"], str)


def test_needs_verification_rules():
    base = {"stage": "native_captcha", "captcha_state": "pending", "status": "pending"}
    assert needs_verification("gpt_direct.progress", base)
    assert needs_verification(
        "gpt_direct.progress", {"captcha_kind": "stripe_3ds", "status": "running"}
    )
    assert not needs_verification("gpt_direct.progress", {**base, "captcha_state": "solving"})
    assert not needs_verification("cdk.consumed", base)
    assert not needs_verification("gpt_direct.progress", {"stage": "running"})
    for status in ("completed", "failed", "failed_precharge", "cancelled", "declined", "expired"):
        assert not needs_verification("gpt_direct.progress", {**base, "status": status})


def test_deadline_from_expiry_with_fallback_and_never_negative():
    now = datetime(2026, 11, 26, 13, 12, 0, tzinfo=timezone.utc)
    secs, at = verify_deadline("2026-11-26T13:14:30Z", now)
    assert secs == 150 and at.strftime("%H:%M:%S") == "13:14:30"
    assert verify_deadline("2026-11-26T13:00:00Z", now)[0] == 0
    assert verify_deadline(None, now)[0] == 180
    assert verify_deadline("garbage", now)[0] == 180


# ------------------------------------------------------------------------------- the alert
async def test_alert_names_our_order_once_and_ignores_repeats(env, hook):
    buyer, _ = await env.login("buyer")
    order = await buy(buyer, await env.product(mode="manual"))
    await _our_job(env, order)
    ev = _event(1)
    assert await _send(env, ev) == 200
    assert await _send(env, ev) == 200  # duplicate delivery (same event id)
    # a second, different event for the same verification window: throttled by the once key
    assert await _send(env, {**ev, "event_id": "gpt_direct.progress:2"}) == 200
    assert len(hook.calls) == 1
    text = hook.texts[0]
    assert "URGENT: Stripe verification needed" in text
    assert order["order_no"] in text
    assert "Complete within" in text and "UTC" in text
    assert "zovocard.com/direct" in text and "/admin/orders" in text
    assert TOKEN not in text and MESSAGE not in text  # vendor free text is never forwarded


async def test_unknown_vendor_order_is_named_by_its_vendor_id(env, hook):
    assert await _send(env, _event(1, order_id=9001, cdk_id=9001)) == 200
    assert len(hook.calls) == 1
    assert "ZovoCard order #9001" in hook.texts[0]


async def test_solving_and_terminal_states_send_nothing(env, hook):
    assert await _send(env, _event(1, captcha_state="solving")) == 200
    for i, status in enumerate(("completed", "failed", "declined", "cancelled"), start=2):
        assert await _send(env, _event(i, status=status)) == 200
    assert await _send(env, _event(9, stage="running", captcha_kind="", captcha_state="")) == 200
    assert hook.calls == []


async def test_other_tenants_job_is_never_used(env, hook):
    buyer, _ = await env.login("buyer")
    order = await buy(buyer, await env.product(mode="manual"))
    await _our_job(env, order, tenant_id=env.other_tenant_id)  # same upstream_ref, other tenant
    assert await _send(env, _event(1)) == 200
    assert len(hook.calls) == 1
    assert order["order_no"] not in hook.texts[0]
    assert "ZovoCard order #4242" in hook.texts[0]


async def test_event_toggle_off_sends_nothing(env, hook):
    from tests.test_v5_core import put_site

    admin, _ = await env.login("admin")
    await put_site(env, admin, {"notifications": {"event_direct_verify": False}})
    assert await _send(env, _event(1)) == 200
    assert hook.calls == []


# ------------------------------------------------------------------------------- buyer view
def test_buyer_progress_never_shows_the_vendor_sentence():
    job = SimpleNamespace(
        state="accepted_pending",
        error_code=None,
        upstream_ref="4242",
        upstream_status="pending",
        first_sent_at=datetime.now(timezone.utc),
        progress={
            "v": {"status": "pending", "stage": "native_captcha"},
            "events": [
                {"tid": "1", "cat": "progress", "code": "awaiting_native_captcha", "msg": MESSAGE}
            ],
        },
    )
    p = dp.from_job(job)
    view = json.dumps(dp.buyer_view(p), ensure_ascii=False)
    assert p.step == "processing" and p.outcome == "active"
    assert "完成验证" not in view and "captcha" not in view.lower()
