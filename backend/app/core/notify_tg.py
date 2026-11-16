"""Telegram staff notifications (review-v5.2 §10).

Best effort, off the request path: callers use :func:`spawn` (a tracked asyncio task) and
:func:`notify` never raises. The bot token is the ``telegram_bot_token`` tenant secret; it is
read only here, sent only to api.telegram.org and never logged (failures log the exception
class, not the message, because httpx messages can carry the URL). Messages carry an event
label, order no, product title, amount and an admin link: never card secrets or buyer
contact data. Dedupe/throttle keys are set only once a message is actually going out.

Language: the tenant setting ``notifications.telegram_language`` (``en`` by default, ``zh-CN``
selectable in the console). Callers never write prose: they pass :class:`T` keys from
``TEXTS`` (rendered when the message goes out, in the tenant's language) and plain strings only
for data (titles, order numbers, amounts).
"""

from __future__ import annotations

import asyncio
import html
import logging
import uuid
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core import secretbox
from app.core.db import tenant_session
from app.core.money import fmt
from app.core.resources import Resources
from app.core.settings_v5 import section
from app.modules.content.site import tenant_settings

log = logging.getLogger("app.notify_tg")

EVENTS = (
    "order_paid",
    "chat_message",
    "after_sales",
    "payment_review",
    "reseller_application",
    "low_stock",
)
API = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT_S = 5.0
LOW_STOCK_BELOW = 3
LABELS = {
    "order_paid": "Order paid",
    "chat_message": "New buyer message",
    "after_sales": "After-sales request",
    "payment_review": "Payment needs review",
    "reseller_application": "Reseller application",
    "low_stock": "Low card stock",
}
DEFAULT_LANG = "en"
# every staff-facing sentence, per language; ``en`` is the source and must hold every key
TEXTS: dict[str, dict[str, str]] = {
    "en": {
        **{f"label.{k}": v for k, v in LABELS.items()},
        "label.urgent_manual_paid": "URGENT: manual order paid",
        "label.urgent_reminder": "URGENT: buyer reminder",
        "label.test": "Test",
        "test.body": "Test message from your store settings.",
        "order.no": "Order {no}",
        "order.manual_ship_now": "manual delivery: ship now",
        "order.manual": "manual delivery",
        "order.auto": "auto delivery",
        "order.second_payment": "SECOND payment, refund it: trade {trade}",
        "order.expedite": "Buyer asks to ship this manual order now",
        "stock.after_order": "Available after this order: {n}",
        "aftersales.reason": "Reason: {reason}",
        "chat.from": "From {who}",
        "chat.type": "Type: {type}",
        "reseller.store": "Store: {slug}",
        "reseller.applicant": "Applicant: {who}",
        "job.dead": "Delivery job {topic} #{id} failed {n} times and was dropped",
        "job.maybe_undelivered": "Order {order} may be undelivered",
        "pay.stale": "Payment attempt for {no} has been unresolved for 24 h",
        "pay.check_gateway": "Check the gateway console",
        "proc.job_stuck": "Procurement job {job} is {state} ({code})",
        "proc.order_waits": "Order {order} waits for a supplier code",
        "proc.supplier_paused": "Supplier {code} ({env}) is paused for new orders",
        "proc.pause_reason": "Reason: {reason} ({kind})",
        "reminder.title": "Service expiry reminder",
        "reminder.id": "Notification",
        "reminder.service": "Service",
        "reminder.buyer": "Buyer reference",
        "reminder.due": "Due",
        "reminder.unknown": "unknown",
    },
    "zh-CN": {
        "label.order_paid": "订单已付款",
        "label.chat_message": "买家新消息",
        "label.after_sales": "售后申请",
        "label.payment_review": "付款需人工核对",
        "label.reseller_application": "分销商申请",
        "label.low_stock": "卡密库存不足",
        "label.urgent_manual_paid": "紧急：人工发货订单已付款",
        "label.urgent_reminder": "紧急：买家催单",
        "label.test": "测试",
        "test.body": "这是一条来自店铺设置的测试消息。",
        "order.no": "订单 {no}",
        "order.manual_ship_now": "人工发货：请立即发货",
        "order.manual": "人工发货",
        "order.auto": "自动发货",
        "order.second_payment": "重复付款，请退款：交易号 {trade}",
        "order.expedite": "买家请求立即发货（人工发货订单）",
        "stock.after_order": "本单之后可用库存：{n}",
        "aftersales.reason": "原因：{reason}",
        "chat.from": "来自 {who}",
        "chat.type": "类型：{type}",
        "reseller.store": "店铺：{slug}",
        "reseller.applicant": "申请人：{who}",
        "job.dead": "发货任务 {topic} #{id} 失败 {n} 次，已被丢弃",
        "job.maybe_undelivered": "订单 {order} 可能未发货",
        "pay.stale": "订单 {no} 的付款已超过 24 小时未确认",
        "pay.check_gateway": "请在支付网关后台核对",
        "proc.job_stuck": "采购任务 {job} 状态为 {state}（{code}）",
        "proc.order_waits": "订单 {order} 正在等待供应商发码",
        "proc.supplier_paused": "供应商 {code}（{env}）已暂停接收新订单",
        "proc.pause_reason": "原因：{reason}（{kind}）",
        "reminder.title": "服务到期提醒",
        "reminder.id": "通知编号",
        "reminder.service": "服务",
        "reminder.buyer": "买家编号",
        "reminder.due": "到期时间",
        "reminder.unknown": "未知",
    },
}


@dataclass(frozen=True)
class T:
    """A catalog sentence with its values; rendered in the tenant's language when sent."""

    key: str
    params: dict[str, Any] = field(default_factory=dict)

    def __init__(self, key: str, **params: Any) -> None:
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "params", params)


Line = str | T


def tr(lang: str, key: str, **params: Any) -> str:
    """Catalog text in ``lang`` (English when the language or the key is missing)."""
    table = TEXTS.get(lang, TEXTS[DEFAULT_LANG])
    template = table.get(key) or TEXTS[DEFAULT_LANG].get(key, key)
    try:
        return template.format(**params)
    except (KeyError, IndexError):
        return template


def render(lang: str, line: Line) -> str:
    return tr(lang, line.key, **line.params) if isinstance(line, T) else line


def language(cfg: dict[str, Any]) -> str:
    lang = cfg.get("telegram_language", DEFAULT_LANG)
    return lang if lang in TEXTS else DEFAULT_LANG


_tasks: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> None:
    """Run ``coro`` in the background; the task is held so it is not garbage collected."""
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:  # no loop (should not happen in the API): drop quietly
        coro.close()
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def drain() -> None:
    """Wait for in-flight notifications (tests, shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


def mask_email(email: str) -> str:
    """``alice@example.com`` -> ``a***@example.com`` (never the full address)."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def admin_link(res: Resources, path: str) -> str:
    return f"{res.settings.public_base_url.rstrip('/')}/admin/{path.lstrip('/')}"


def build_text(
    event: str,
    lines: Sequence[Line],
    link: str | None = None,
    label: Line | None = None,
    lang: str = DEFAULT_LANG,
) -> str:
    """HTML message: bold label, escaped body lines, optional link. Catalog entries are rendered
    in ``lang`` first and escaped afterwards, values included."""
    head = render(lang, label) if label is not None else tr(lang, f"label.{event}")
    parts = [
        f"<b>{html.escape(head)}</b>",
        *(html.escape(render(lang, x)) for x in lines),
    ]
    if link:
        parts.append(html.escape(link))
    return "\n".join(parts)


async def _post(res: Resources, token: str, chat_id: str, text: str) -> bool:
    try:
        r = await res.http.post(
            API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=TIMEOUT_S,
        )
        if r.status_code != 200:
            log.warning("telegram send failed: http %s", r.status_code)
            return False
        return True
    except Exception as exc:  # network, timeout, TLS ...  never log str(exc) (may hold the URL)
        log.warning("telegram send failed: %s", type(exc).__name__)
        return False


async def notify(
    res: Resources,
    tenant_id: uuid.UUID,
    event: str,
    text: str | Callable[[str], str],
    *,
    once: tuple[str, int] | None = None,
) -> bool:
    """Send ``text`` to every configured chat when enabled for ``event``. Never raises.
    A callable ``text`` receives the tenant's notification language and returns the message.

    ``once=(key, ttl_s)``: Redis SET NX EX dedupe/throttle (only claimed when a send follows).
    """
    try:
        async with tenant_session(res.maker, tenant_id) as db:
            cfg = section(await tenant_settings(db, tenant_id), "notifications")
            if not cfg["telegram_enabled"] or not cfg.get(f"event_{event}", False):
                return False
            chats = cfg["telegram_chat_ids"]
            lang = language(cfg)
            token = await secretbox.get_secret(db, res.settings, tenant_id, "telegram_bot_token")
        if not token or not chats:
            return False
        if callable(text):
            text = text(lang)
        if once is not None:
            key, ttl = once
            if not await res.redis.set(f"tg:{tenant_id}:{key}", "1", nx=True, ex=ttl):
                return False
        results = [await _post(res, token, c, text) for c in chats]
        return any(results)
    except Exception as exc:
        log.warning("telegram notify failed: %s", type(exc).__name__)
        return False


def notify_bg(
    res: Resources,
    tenant_id: uuid.UUID,
    event: str,
    lines: Sequence[Line],
    *,
    link: str | None = None,
    once: tuple[str, int] | None = None,
    label: Line | None = None,
) -> None:
    body = list(lines)
    spawn(
        notify(
            res,
            tenant_id,
            event,
            lambda lang: build_text(event, body, link, label, lang),
            once=once,
        )
    )


async def send_test(res: Resources, tenant_id: uuid.UUID) -> tuple[bool, str | None]:
    """Admin test: ignores the enable/event toggles, reports a short error code. Never raises."""
    try:
        async with tenant_session(res.maker, tenant_id) as db:
            cfg = section(await tenant_settings(db, tenant_id), "notifications")
            token = await secretbox.get_secret(db, res.settings, tenant_id, "telegram_bot_token")
    except Exception as exc:  # e.g. unseal failure after key rotation
        log.warning("telegram test: config unavailable (%s)", type(exc).__name__)
        return False, "config_error"
    if not token:
        return False, "no_token"
    if not cfg["telegram_chat_ids"]:
        return False, "no_chat"
    text = build_text("test", [T("test.body")], lang=language(cfg))
    ok = [await _post(res, token, c, text) for c in cfg["telegram_chat_ids"]]
    return (True, None) if all(ok) else (False, "send_failed")


# ------------------------------------------------------------------ event helpers
def order_lines(order: Any) -> list[Line]:
    return [
        T("order.no", no=order.order_no),
        f"{order.title}",
        f"{fmt(order.amount_minor)} {order.currency}",
    ]


def order_event(
    res: Resources,
    order: Any,
    event: str,
    extra: Sequence[Line] | None = None,
    once_suffix: str = "",
    label: Line | None = None,
) -> None:
    """Notify about one order (plain attributes only: safe to call after commit)."""
    notify_bg(
        res,
        order.tenant_id,
        event,
        [*order_lines(order), *(extra or [])],
        link=admin_link(res, "orders"),
        once=(f"{event}:{order.id}{once_suffix}", 7 * 86400),
        label=label,
    )


def outcome_events(
    res: Resources, orders: list[Any], dup_trade_no: str | Sequence[str] | None = None
) -> None:
    """After a settled payment: paid -> order_paid (+ low stock); manual_review -> review.
    Never raises: runs inline on the payment-notify path after commit.

    ``dup_trade_no`` may be several trades (a merged reconciliation outcome): each is a separate
    refund owed, so each gets its own alert rather than being folded into one."""
    try:
        _outcome_events(res, orders)
        dups = [dup_trade_no] if isinstance(dup_trade_no, str) else list(dup_trade_no or ())
        for dup in dict.fromkeys(d for d in dups if d):
            # a second paid trade: never throttled behind the first review alert
            for o in orders:
                order_event(
                    res,
                    o,
                    "payment_review",
                    [T("order.second_payment", trade=dup[:32])],
                    once_suffix=f":{dup[:64]}",
                )
    except Exception as exc:  # a notification bug must not fail a notify reply
        log.warning("telegram outcome events skipped (%s)", type(exc).__name__)


def _outcome_events(res: Resources, orders: list[Any]) -> None:
    for o in orders:
        if o.status == "paid":
            if o.delivery_mode == "manual" and o.kind == "goods":
                # one notice, marked urgent: staff must ship by hand (no second alert)
                order_event(
                    res,
                    o,
                    "order_paid",
                    [T("order.manual_ship_now")],
                    label=T("label.urgent_manual_paid"),
                )
            else:
                mode = T("order.manual" if o.delivery_mode == "manual" else "order.auto")
                order_event(res, o, "order_paid", [mode] if o.kind == "goods" else [o.kind])
            if o.delivery_mode == "auto_card" and o.product_id is not None:  # pool only
                spawn(low_stock_check(res, o.tenant_id, o.product_id, o.qty, o.title))
        elif o.status == "manual_review":
            order_event(res, o, "payment_review")


async def low_stock_check(
    res: Resources, tenant_id: uuid.UUID, product_id: uuid.UUID, qty: int, title: str
) -> None:
    """Throttled per product (1 h) once available cards, minus this order's, drop below 3."""
    try:
        from sqlalchemy import func, select

        from app.models import CardStock

        async with tenant_session(res.maker, tenant_id) as db:
            avail = await db.scalar(
                select(func.count())
                .select_from(CardStock)
                .where(
                    CardStock.tenant_id == tenant_id,
                    CardStock.product_id == product_id,
                    CardStock.status == "available",
                )
            )
        left = int(avail or 0) - qty
        if left >= LOW_STOCK_BELOW:
            return
        await notify(
            res,
            tenant_id,
            "low_stock",
            lambda lang: build_text(
                "low_stock",
                [title, T("stock.after_order", n=max(left, 0))],
                admin_link(res, "inventory"),
                lang=lang,
            ),
            once=(f"low_stock:{product_id}", 3600),
        )
    except Exception as exc:
        log.warning("low stock check failed: %s", type(exc).__name__)
