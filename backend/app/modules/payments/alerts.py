"""Staff Telegram alerts about payment gateways (owner request 2026-11-26).

Best effort and off the payment path: every function only schedules a background send and never
raises. Gateway text (the gateway's refusal message, the admin-chosen label) is data: control
characters are stripped and the length capped here; the Telegram builder HTML-escapes it.
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import select

from app.core import notify_tg
from app.core.db import tenant_session
from app.core.money import fmt
from app.core.resources import Resources
from app.models import PaymentGateway
from app.modules.payments import deposit

log = logging.getLogger("app.payments.alerts")

REFUSED_TTL_S = 6 * 3600
UNREACHABLE_TTL_S = 3600
NO_GATEWAY_TTL_S = 15 * 60
LARGE_TTL_S = 7 * 86400
MESSAGE_CAP = 120
LABEL_CAP = 80
_CTRL = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]")


def clean_text(value: object, cap: int) -> str:
    """Control and bidi characters removed, whitespace collapsed, length-capped."""
    return " ".join(_CTRL.sub(" ", str(value or "")).split())[:cap]


def gateway_failed(
    res: Resources, tid: uuid.UUID, pid: uuid.UUID, label: str, reason: str, message: str = ""
) -> None:
    """One create attempt at a gateway failed cleanly (refused / unreachable)."""
    try:
        name = clean_text(label, LABEL_CAP) or str(pid)[:8]
        if reason == "gateway_refused":
            lines: list[notify_tg.Line] = [
                notify_tg.T(
                    "gw.refused", label=name, message=clean_text(message, MESSAGE_CAP) or "-"
                ),
                notify_tg.T("gw.refused_hint"),
            ]
            once = (f"gw_refused:{pid}", REFUSED_TTL_S)
        else:
            lines = [notify_tg.T("gw.unreachable", label=name), notify_tg.T("gw.unreachable_hint")]
            once = (f"gw_unreachable:{pid}", UNREACHABLE_TTL_S)
        notify_tg.notify_bg(
            res,
            tid,
            "gateway_down",
            lines,
            link=notify_tg.admin_link(res, "settings/payment"),
            once=once,
        )
    except Exception as exc:
        log.warning("gateway alert skipped (%s)", type(exc).__name__)


def no_gateway_left(res: Resources, tid: uuid.UUID) -> None:
    """Every gateway failed for one order: buyers cannot pay right now (urgent)."""
    try:
        notify_tg.notify_bg(
            res,
            tid,
            "gateway_down",
            [notify_tg.T("gw.none"), notify_tg.T("gw.none_hint")],
            link=notify_tg.admin_link(res, "settings/payment"),
            once=("gw_none", NO_GATEWAY_TTL_S),
            label=notify_tg.T("label.urgent_no_gateway"),
        )
    except Exception as exc:
        log.warning("gateway alert skipped (%s)", type(exc).__name__)


def _yuan(minor: int) -> str:
    return "¥" + fmt(minor)


def payment_settled(
    res: Resources, tid: uuid.UUID, pid: uuid.UUID, amount_minor: int, ref: str
) -> None:
    """A payment through gateway ``pid`` just settled (call AFTER the commit): schedule the
    large-payment check. Never raises, never touches the settlement."""
    try:
        notify_tg.spawn(_large_payment(res, tid, pid, int(amount_minor), ref[:64]))
    except Exception as exc:
        log.warning("large payment check skipped (%s)", type(exc).__name__)


async def _large_payment(
    res: Resources, tid: uuid.UUID, pid: uuid.UUID, amount_minor: int, ref: str
) -> None:
    """Alert when ``amount_minor`` is above the gateway's previous 30-day largest payment: the
    gateway's deposit due rises with it (see :mod:`app.modules.payments.deposit`)."""
    try:
        async with tenant_session(res.maker, tid) as db:
            gw = await db.scalar(
                select(PaymentGateway).where(
                    PaymentGateway.id == pid, PaymentGateway.tenant_id == tid
                )
            )
            if gw is None:
                return
            prev = await deposit.max30_minor(db, tid, pid, exclude_ref=ref)
            if amount_minor <= prev:
                return
            name = clean_text(gw.label, LABEL_CAP) or str(pid)[:8]
            lines: list[notify_tg.Line] = [
                notify_tg.T("large.line", amount=_yuan(amount_minor), label=name, prev=_yuan(prev))
            ]
            if deposit.guarded(gw):
                required = deposit.required_minor(
                    amount_minor, gw.deposit_pct or 100, gw.deposit_floor_minor or 0
                )
                paid = gw.deposit_minor or 0
                if required > paid:
                    lines.append(
                        notify_tg.T(
                            "large.deposit",
                            required=_yuan(required),
                            deposit=_yuan(paid),
                            short=_yuan(required - paid),
                        )
                    )
                else:
                    lines.append(
                        notify_tg.T(
                            "large.deposit_ok", required=_yuan(required), deposit=_yuan(paid)
                        )
                    )
        notify_tg.notify_bg(
            res,
            tid,
            "large_payment",
            lines,
            link=notify_tg.admin_link(res, "settings/payment"),
            once=(f"large_payment:{ref}", LARGE_TTL_S),
        )
    except Exception as exc:
        log.warning("large payment check failed: %s", type(exc).__name__)
