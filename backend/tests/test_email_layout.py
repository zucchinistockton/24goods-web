"""Email layout: escaping, focal blocks, plain-text part, legacy defaults (no services needed)."""

from __future__ import annotations

from app.core.email_templates import _LEGACY, DEFAULTS, SAMPLE, normalize, render_template

VALUES = {**SAMPLE, "site_name": "Kesiv & <Co>"}


def test_code_mail_shows_the_code_once_in_a_box() -> None:
    subject, text, html = render_template(None, "verify_code", "en", VALUES)
    assert "123456" not in subject  # codes never appear in subjects (lock-screen previews)
    assert html.startswith("<!doctype html>") and html.count("123456") == 1
    assert 'class="bx"' in html and "letter-spacing" in html
    assert "\n123456\n" in text and "**" not in text
    assert "Kesiv &amp; &lt;Co&gt;" in html and "<Co>" not in html
    assert "Never share this code" in html and "Never share this code" in text


def test_custom_body_with_inline_code_is_kept_as_written() -> None:
    ts = {"email_templates": {"verify_code": {"body_i18n": {"en": "Code: **{code}** now"}}}}
    _, text, html = render_template(ts, "verify_code", "en", VALUES)
    assert "<strong>123456</strong>" in html and 'class="bx"' not in html
    assert text.startswith("Code: 123456 now")


def test_order_mail_has_summary_button_and_escapes_values() -> None:
    values = {**VALUES, "title": '<script>alert(1)</script> "Plus" [x](http://evil.test)'}
    subject, text, html = render_template(None, "order_delivered", "en", values)
    assert subject == "Your order is ready · Order GABCDEFGHIJKL · Kesiv & <Co>"
    assert "<script" not in html and 'href="http://evil.test"' not in html
    assert 'href="https://example.com/orders/GABCDEFGHIJKL"' in html and "View order" in html
    assert "Status: Delivered" in text
    assert "View order: https://example.com/orders/GABCDEFGHIJKL" in text
    assert "not sent by email" in text


def test_order_link_must_be_http() -> None:
    for bad in ("javascript:alert(1)", "data:text/html,x", ""):
        _, _, html = render_template(None, "order_paid", "en", {**VALUES, "order_url": bad})
        assert "<a " not in html and "javascript:" not in html


def test_no_remote_assets_and_zh_layout() -> None:
    for kind in DEFAULTS:
        _, text, html = render_template(None, kind, "zh-CN", VALUES)
        assert "<img" not in html and "<link" not in html and "url(" not in html
        assert 'lang="zh-CN"' in html and "请勿直接回复" in text
    _, _, html = render_template(None, "order_paid", "zh-CN", VALUES)
    assert "查看订单" in html and "已支付" in html


def test_legacy_stored_defaults_follow_the_new_text() -> None:
    old = _LEGACY["order_paid"]
    ts = {
        "email_templates": {
            "order_paid": {
                "subject_i18n": {"en": old["subject_i18n"]["en"], "zh-CN": "自定义主题 {order_no}"},
                "body_i18n": dict(old["body_i18n"]),
            }
        }
    }
    got = normalize(ts)["order_paid"]
    assert got["subject_i18n"]["en"] == DEFAULTS["order_paid"]["subject_i18n"]["en"]
    assert got["subject_i18n"]["zh-CN"] == "自定义主题 {order_no}"
    assert got["body_i18n"] == DEFAULTS["order_paid"]["body_i18n"]
