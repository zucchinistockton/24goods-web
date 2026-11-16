# ruff: noqa: E501
"""Configurable email templates (``tenants.settings.email_templates``).

Four templates (verify_code, password_reset, order_paid, order_delivered), each
``{subject_i18n, body_i18n}`` per locale. Bodies are plain text or simple markdown: rendered
to a plain-text part (placeholders substituted verbatim) and an HTML part (markdown -> nh3
allow-list, substituted values markdown-escaped so a product title cannot inject links).
Placeholders: ``{site_name} {code} {minutes} {order_no} {order_url} {title} {amount}``;
unknown placeholders stay literal. Callers never pass card secrets (only these values).

The HTML part is a complete, self-contained document (``_layout``): table layout with inline
styles, light/dark through ``prefers-color-scheme``, no remote asset of any kind. Around the
admin-editable body it adds a header with the site name, one focal block (the code for code
mails; an order summary and a "View order" button for order mails) and a footer. Every value
placed in the layout is HTML-escaped; the order link must be http(s).
"""

from __future__ import annotations

import copy
import html as _html
import re
from typing import Any

from app.core.markdown import render

KINDS = ("verify_code", "password_reset", "order_paid", "order_delivered")
PLACEHOLDERS = ("site_name", "code", "minutes", "order_no", "order_url", "title", "amount")
LOCALES = ("en", "zh-CN")
MAX_SUBJECT = 150
MAX_BODY = 5000

DEFAULTS: dict[str, dict[str, dict[str, str]]] = {
    "verify_code": {
        "subject_i18n": {
            "en": "Your {site_name} verification code",
            "zh-CN": "您的 {site_name} 验证码",
        },
        "body_i18n": {
            "en": "Enter this code to continue:\n\n**{code}**\n\n"
            "It expires in {minutes} minutes. If you did not request it, you can ignore this email.",
            "zh-CN": "请输入以下验证码以继续：\n\n**{code}**\n\n"
            "验证码 {minutes} 分钟内有效。如非本人操作，请忽略此邮件。",
        },
    },
    "password_reset": {
        "subject_i18n": {
            "en": "Reset your {site_name} password",
            "zh-CN": "重置您的 {site_name} 密码",
        },
        "body_i18n": {
            "en": "Enter this code to reset your password:\n\n**{code}**\n\n"
            "It expires in {minutes} minutes. If you did not request a reset, ignore this email; "
            "your password is unchanged.",
            "zh-CN": "请输入以下验证码以重置密码：\n\n**{code}**\n\n"
            "验证码 {minutes} 分钟内有效。如非本人操作，请忽略此邮件，您的密码不会改变。",
        },
    },
    "order_paid": {
        "subject_i18n": {
            "en": "Payment received · Order {order_no} · {site_name}",
            "zh-CN": "付款成功 · 订单 {order_no} · {site_name}",
        },
        "body_i18n": {
            "en": "Thank you. We received your payment for **{title}**.\n\n"
            "We are preparing your order and will email you again when it is delivered.",
            "zh-CN": "感谢您的购买，我们已收到 **{title}** 的付款。\n\n订单正在处理中，交付后会再次邮件通知您。",
        },
    },
    "order_delivered": {
        "subject_i18n": {
            "en": "Your order is ready · Order {order_no} · {site_name}",
            "zh-CN": "订单已交付 · 订单 {order_no} · {site_name}",
        },
        "body_i18n": {
            "en": "Your order for **{title}** has been delivered.\n\n"
            "For your security the delivery details are not sent by email. Open the order in your "
            "account to see them.",
            "zh-CN": "您购买的 **{title}** 已交付。\n\n为了您的安全，交付内容不会通过邮件发送，请登录账户在订单中查看。",
        },
    },
}

# Built-in texts shipped before the 2026-11 redesign. A stored value equal to one of these was never
# edited by an admin (saving any template used to store all of them), so it follows the new default.
_LEGACY: dict[str, dict[str, dict[str, str]]] = {
    "verify_code": {
        "subject_i18n": {
            "en": "{site_name} verification code",
            "zh-CN": "{site_name} 验证码",
        },
        "body_i18n": {
            "en": "Your verification code is **{code}**. It is valid for {minutes} minutes.\n\n"
            "If you did not request it, ignore this email.",
            "zh-CN": "您的验证码是 **{code}**，{minutes} 分钟内有效。\n\n如非本人操作请忽略此邮件。",
        },
    },
    "password_reset": {
        "subject_i18n": {
            "en": "{site_name} password reset code",
            "zh-CN": "{site_name} 重置密码验证码",
        },
        "body_i18n": {
            "en": "Use code **{code}** to reset your password. It is valid for {minutes} minutes.\n\n"
            "If you did not request this, ignore this email; your password is unchanged.",
            "zh-CN": "请使用验证码 **{code}** 重置密码，{minutes} 分钟内有效。\n\n"
            "如非本人操作请忽略此邮件，您的密码不会改变。",
        },
    },
    "order_paid": {
        "subject_i18n": {
            "en": "{site_name}: payment received for order {order_no}",
            "zh-CN": "{site_name}：订单 {order_no} 已支付",
        },
        "body_i18n": {
            "en": "We received your payment of {amount} for **{title}** (order {order_no}).\n\n"
            "View your order: {order_url}",
            "zh-CN": "我们已收到订单 {order_no}（**{title}**）的付款 {amount}。\n\n查看订单：{order_url}",
        },
    },
    "order_delivered": {
        "subject_i18n": {
            "en": "{site_name}: order {order_no} delivered",
            "zh-CN": "{site_name}：订单 {order_no} 已发货",
        },
        "body_i18n": {
            "en": "Your order {order_no} (**{title}**) has been delivered.\n\n"
            "View it in your account: {order_url}",
            "zh-CN": "您的订单 {order_no}（**{title}**）已发货。\n\n请登录账户查看：{order_url}",
        },
    },
}


# Fixed layout texts (not admin-editable): labels around the focal block and the footer.
_L: dict[str, dict[str, str]] = {
    "en": {
        "code_note": "Never share this code. {site_name} staff will never ask for it.",
        "order": "Order",
        "item": "Item",
        "amount": "Amount",
        "status": "Status",
        "paid": "Paid",
        "delivered": "Delivered",
        "view": "View order",
        "link_hint": "If the button does not work, open this link:",
        "footer": "This is an automated message from {site_name}. Please do not reply to it.",
    },
    "zh-CN": {
        "code_note": "请勿向任何人透露验证码，{site_name} 工作人员不会向您索取。",
        "order": "订单号",
        "item": "商品",
        "amount": "金额",
        "status": "状态",
        "paid": "已支付",
        "delivered": "已交付",
        "view": "查看订单",
        "link_hint": "如果按钮无法打开，请复制以下链接：",
        "footer": "此邮件由 {site_name} 系统自动发送，请勿直接回复。",
    },
}
_CODE_KINDS = ("verify_code", "password_reset")
_ORDER_STATUS = {"order_paid": "paid", "order_delivered": "delivered"}
_FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif"
)
_MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
_BOLD = re.compile(r"\*\*(.+?)\*\*")

_PH = re.compile(r"\{(\w+)\}")
_MD_SPECIAL = re.compile(r"([\\`*_\[\]()<>#!|~&])")


def _clean_map(
    raw: Any, default: dict[str, str], max_len: int, legacy: dict[str, str] | None = None
) -> dict[str, str]:
    out = copy.deepcopy(default)
    if isinstance(raw, dict):
        for k in LOCALES:
            v = raw.get(k)
            if isinstance(v, str) and v.strip() and v.strip() != (legacy or {}).get(k):
                out[k] = v.strip()[:max_len]
    return out


def normalize(ts: dict[str, Any] | None) -> dict[str, dict[str, dict[str, str]]]:
    """Complete templates: stored values where set, built-in defaults elsewhere."""
    raw = (ts or {}).get("email_templates")
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, dict[str, dict[str, str]]] = {}
    for kind in KINDS:
        r = raw.get(kind) if isinstance(raw.get(kind), dict) else {}
        out[kind] = {
            fld: _clean_map(r.get(fld), DEFAULTS[kind][fld], limit, _LEGACY[kind][fld])
            for fld, limit in (("subject_i18n", MAX_SUBJECT), ("body_i18n", MAX_BODY))
        }
    return out


def stored_after_patch(
    ts: dict[str, Any] | None, patch: dict[str, dict[str, dict[str, str]]]
) -> dict[str, Any]:
    """New ``email_templates`` value after a partial patch (kind -> field -> locale -> text).
    An empty string for a locale resets it to the built-in default."""
    cur = {k: {f: dict(m) for f, m in v.items()} for k, v in normalize(ts).items()}
    for kind, fields in patch.items():
        if kind not in KINDS:
            continue
        for fld in ("subject_i18n", "body_i18n"):
            for loc, text in (fields.get(fld) or {}).items():
                if loc not in LOCALES:
                    continue
                text = (text or "").strip()
                limit = MAX_SUBJECT if fld == "subject_i18n" else MAX_BODY
                cur[kind][fld][loc] = text[:limit] if text else DEFAULTS[kind][fld][loc]
    return cur


def _substitute(template: str, values: dict[str, str], *, escape: bool) -> str:
    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in PLACEHOLDERS or name not in values:
            return m.group(0)  # unknown placeholder: left literal
        v = values[name]
        return _MD_SPECIAL.sub(r"\\\1", v) if escape and name != "order_url" else v

    return _PH.sub(repl, template)


def _row(label: str, value: str, *, mono: bool = False) -> str:
    font = f"font-family:{_MONO};" if mono else ""
    return (
        f'<tr><td class="mu" style="padding:6px 0;color:#6b7280;font-size:13px;white-space:nowrap;'
        f'vertical-align:top">{_html.escape(label)}</td>'
        f'<td style="padding:6px 0 6px 16px;font-size:14px;text-align:right;word-break:break-word;'
        f'{font}">{_html.escape(value)}</td></tr>'
    )


def _layout(kind: str, loc: str, v: dict[str, str], subject: str, body_html: str) -> str:
    """Complete HTML document around the rendered body. ``v`` holds raw (unescaped) values."""
    t = _L[loc]
    site = v.get("site_name", "")
    focal = ""
    code = v.get("code", "")
    if kind in _CODE_KINDS and code:
        box = (
            f'<p class="bx" style="margin:20px 0;padding:16px 8px;background:#f4f5f7;border-radius:8px;'
            f"text-align:center;font-family:{_MONO};font-size:30px;font-weight:700;"
            f'letter-spacing:6px;color:#111827">{_html.escape(code)}</p>'
        )
        # the default body carries the code alone in a paragraph: show that paragraph as the box
        alone = f"<p><strong>{_html.escape(code)}</strong></p>"
        if alone in body_html:
            body_html = body_html.replace(alone, box, 1)
        focal = (
            f'<p class="mu" style="margin:16px 0 0;color:#6b7280;font-size:13px;line-height:1.5">'
            f"{_html.escape(t['code_note'].format(site_name=site))}</p>"
        )
    elif kind in _ORDER_STATUS:
        rows = _row(t["order"], v.get("order_no", ""), mono=True)
        rows += _row(t["item"], v.get("title", "")) + _row(t["amount"], v.get("amount", ""))
        rows += _row(t["status"], t[_ORDER_STATUS[kind]])
        focal = (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="ln" '
            'style="margin:20px 0 0;border-top:1px solid #e5e7eb;border-bottom:1px solid #e5e7eb">'
            f"{rows}</table>"
        )
        url = v.get("order_url", "")
        if url.startswith(("https://", "http://")):
            u = _html.escape(url, quote=True)
            focal += (
                f'<p style="margin:24px 0 0"><a class="bt" href="{u}" style="display:inline-block;'
                "padding:12px 28px;background:#111827;color:#ffffff;border-radius:8px;font-size:15px;"
                f'font-weight:600;text-decoration:none">{_html.escape(t["view"])}</a></p>'
                f'<p class="mu" style="margin:16px 0 0;color:#6b7280;font-size:12px;line-height:1.5;'
                f'word-break:break-all">{_html.escape(t["link_hint"])}<br>{u}</p>'
            )
    lang = "zh-CN" if loc == "zh-CN" else "en"
    return (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="supported-color-schemes" content="light dark">'
        f"<title>{_html.escape(subject)}</title>"
        "<style>@media (prefers-color-scheme:dark){"
        ".bg{background:#0f1115!important}.cd{background:#191c22!important;color:#e6e8eb!important}"
        ".mu{color:#9aa3af!important}.bx{background:#262a33!important;color:#ffffff!important}"
        ".ln{border-color:#30353f!important}.bt{background:#ffffff!important;color:#111827!important}"
        ".cd a:not(.bt){color:#9ecbff!important}}"
        "</style></head>"
        f'<body class="bg" style="margin:0;padding:0;background:#f4f5f7;font-family:{_FONT}">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="bg" '
        'style="background:#f4f5f7"><tr><td align="center" style="padding:24px 12px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:560px"><tr><td class="mu" style="padding:0 4px 12px;font-size:15px;'
        f'font-weight:700;color:#111827;letter-spacing:.2px">{_html.escape(site)}</td></tr>'
        '<tr><td class="cd" style="background:#ffffff;border-radius:12px;padding:28px 24px;'
        'color:#1f2328;font-size:15px;line-height:1.6">'
        f"{body_html}{focal}</td></tr>"
        '<tr><td class="mu" style="padding:16px 4px 0;color:#6b7280;font-size:12px;line-height:1.5">'
        f"{_html.escape(t['footer'].format(site_name=site))}</td></tr>"
        "</table></td></tr></table></body></html>"
    )


def _text(kind: str, loc: str, v: dict[str, str], body: str, body_tpl: str) -> str:
    """Plain-text part: the body without markdown bold, the order summary, the footer."""
    t = _L[loc]
    parts = [_BOLD.sub(r"\1", body)]
    if kind in _CODE_KINDS:
        parts.append(t["code_note"].format(site_name=v.get("site_name", "")))
    elif kind in _ORDER_STATUS:
        lines = [
            f"{t['order']}: {v.get('order_no', '')}",
            f"{t['item']}: {v.get('title', '')}",
            f"{t['amount']}: {v.get('amount', '')}",
            f"{t['status']}: {t[_ORDER_STATUS[kind]]}",
        ]
        if v.get("order_url") and "{order_url}" not in body_tpl:
            lines.append(f"{t['view']}: {v['order_url']}")
        parts.append("\n".join(lines))
    parts.append("-- \n" + t["footer"].format(site_name=v.get("site_name", "")))
    return "\n\n".join(parts)


def render_template(
    ts: dict[str, Any] | None, kind: str, lang: str, values: dict[str, str]
) -> tuple[str, str, str]:
    """(subject, text body, html document) for ``kind`` in ``lang`` (falls back to en)."""
    tpl = normalize(ts)[kind]
    loc = lang if lang in LOCALES else "en"
    clean = {k: " ".join(str(v).split()) for k, v in values.items() if k != "order_url"}
    if "order_url" in values:
        clean["order_url"] = str(values["order_url"]).strip()
    body_tpl = tpl["body_i18n"][loc]
    # a code never goes into a subject (lock-screen previews), also when an edited template asks
    no_code = {k: v for k, v in clean.items() if k != "code"}
    subject = _substitute(tpl["subject_i18n"][loc].replace("{code}", ""), no_code, escape=False)
    subject = " ".join(subject.split())
    text = _text(kind, loc, clean, _substitute(body_tpl, clean, escape=False), body_tpl)
    html = _layout(kind, loc, clean, subject, render(_substitute(body_tpl, clean, escape=True)))
    return subject, text, html


def site_name(ts: dict[str, Any] | None, lang: str, brand: str = "Kesiv") -> str:
    from app.core.settings_v5 import section

    names = section(ts, "general")["site_name_i18n"]
    default = "客思维" if lang == "zh-CN" else brand
    return names.get(lang) or names.get("en") or default


SAMPLE = {
    "code": "123456",
    "minutes": "10",
    "order_no": "GABCDEFGHIJKL",
    "order_url": "https://example.com/orders/GABCDEFGHIJKL",
    "title": "Sample product",
    "amount": "¥19.90",
}
