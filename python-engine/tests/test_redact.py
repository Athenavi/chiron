"""脱敏回归：**JSON 形态**的密钥字段必须被识别。

背景（本轮取证）：三条审计流水（exec / approval / hooks）都是先把对象 `json.dumps`
再交给 `redact_text`，而键名在 JSON 里是带引号的（`"password": "…"`）。原先
`password_assignment` 只匹配 `password=` / `password: `，于是
`{"password": "hunter2secret"}` **原样落盘** —— 与 `approval_audit` 模块 docstring
「参数文本必须脱敏 + 截断」的承诺相反。

同时守住"不误伤正文"这条既定策略（模块 docstring：宁可漏报，不做正则大扫除）。
"""
from __future__ import annotations

import json

import pytest

from app.subagent.redact import redact_text


@pytest.mark.parametrize(
    ("payload", "secret"),
    [
        ({"password": "hunter2secret"}, "hunter2secret"),
        ({"passwd": "hunter2secret"}, "hunter2secret"),
        ({"token": "abcdefghijklmnop123456"}, "abcdefghijklmnop123456"),
        ({"access_token": "ya29abcdefghijklmnop"}, "ya29abcdefghijklmnop"),
        ({"client_secret": "s3cr3t-value-1234"}, "s3cr3t-value-1234"),
        ({"api_key": "sk-1234567890abcdef"}, "sk-1234567890abcdef"),
    ],
)
def test_json_serialized_secret_fields_are_redacted(payload, secret):
    text = json.dumps(payload, ensure_ascii=False)
    out, hits = redact_text(text)
    assert secret not in out, f"密钥以明文留存：{out}"
    assert hits >= 1
    assert "[REDACTED" in out


def test_shell_style_assignment_still_redacted():
    out, hits = redact_text("export password=hunter2secret")
    assert "hunter2secret" not in out
    assert hits == 1


def test_quoted_key_form_is_redacted():
    out, _ = redact_text("password: 'hunter2secret'")
    assert "hunter2secret" not in out


def test_prose_is_not_over_redacted():
    """只匹配明确形态：正文里提到字段名不该被替换。"""
    prose = "the password field is documented in the README section above"
    out, hits = redact_text(prose)
    assert out == prose
    assert hits == 0


def test_short_values_are_not_treated_as_secrets():
    """值太短（<8）不当密钥 —— 避免把 `{"token": "true"}` 这类普通字段打掉。"""
    text = json.dumps({"token": "true"})
    out, hits = redact_text(text)
    assert out == text
    assert hits == 0
