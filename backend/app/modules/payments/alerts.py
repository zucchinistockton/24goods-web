"""Staff Telegram alerts about payment gateways (owner request 2026-11-26).

Best effort and off the payment path: every function only schedules a background send and never
raises. Gateway text (the gateway's refusal message, the admin-chosen label) is data: control
characters are stripped and the length capped here; the Telegram builder HTML-escapes it.
"""

from __future__ import annotations

import logging
import re
import uuid

from app.core import notify_tg
from app.core.resources import Resources

log = logging.getLogger("app.payments.alerts")

REFUSED_TTL_S = 6 * 3600
UNREACHABLE_TTL_S = 3600
NO_GATEWAY_TTL_S = 15 * 60
MESSAGE_CAP = 120
LABEL_CAP = 80
_CTRL = re.compile("[\x00-\x1f\x7f-\x9f​-‏ -‮⁦-⁩﻿]")


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
