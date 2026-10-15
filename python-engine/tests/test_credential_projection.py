"""D4：MCP 凭据投影单测。

钉住四件事：找得出凭据、替换得掉、日志标签不含凭据、投影不污染引擎进程环境。
"""

from __future__ import annotations

from app.mcp.credential_projection import (
    PLACEHOLDER,
    collect_secrets,
    project_stdio_env,
    redact,
    register_secrets,
    registered_secrets,
    reset_registered_secrets,
    safe_label,
)


class _Server:
    """最小 ServerDef 形状（避免 import 重量级模块）。"""

    def __init__(self, name="s", url="", env=None, args=None):
        self.name = name
        self.url = url
        self.env = env or {}
        self.args = args or []


def test_collects_sensitive_env_values() -> None:
    s = _Server(env={"API_KEY": "abcdef123456", "PATH": "/usr/bin"})
    secrets = collect_secrets(s)
    assert "abcdef123456" in secrets
    assert "/usr/bin" not in secrets  # 非敏感键不收集


def test_collects_url_userinfo_and_query() -> None:
    s = _Server(url="https://user:supersecret@mcp.example.com/sse?token=querysecret123")
    secrets = collect_secrets(s)
    assert "supersecret" in secrets
    assert "querysecret123" in secrets


def test_collects_flag_values_from_args() -> None:
    a = _Server(args=["--token", "argsecret1234"])
    b = _Server(args=["--api-key=eqsecret1234"])
    assert "argsecret1234" in collect_secrets(a)
    assert "eqsecret1234" in collect_secrets(b)


def test_ignores_short_values() -> None:
    # 太短的值（"-" / "1"）会在正常文本里到处误命中 ⇒ 不收集。
    s = _Server(env={"TOKEN": "1"})
    assert collect_secrets(s) == []


def test_redact_replaces_every_occurrence() -> None:
    text = "connect to https://h/?token=abc123456 failed; retry with abc123456"
    out = redact(text, ["abc123456"])
    assert "abc123456" not in out
    assert out.count(PLACEHOLDER) == 2


def test_redact_without_secrets_is_identity() -> None:
    assert redact("nothing to do", []) == "nothing to do"
    assert redact("", ["x"]) == ""


def test_safe_label_drops_userinfo_and_query() -> None:
    s = _Server(name="mcp1", url="https://user:pw@mcp.example.com:8443/sse?token=zzz")
    label = safe_label(s)
    assert "pw" not in label and "zzz" not in label and "user" not in label
    assert "mcp.example.com:8443" in label
    assert label.startswith("mcp1 (")


def test_safe_label_without_url_is_just_name() -> None:
    assert safe_label(_Server(name="stdio-srv")) == "stdio-srv"


def test_project_stdio_env_does_not_mutate_base() -> None:
    base = {"PATH": "/usr/bin"}
    s = _Server(env={"API_KEY": "k1234567890"})
    env = project_stdio_env(s, base)
    assert env["API_KEY"] == "k1234567890"
    assert base == {"PATH": "/usr/bin"}  # 入参未被改动
    assert env is not base


def test_project_stdio_env_overrides_base_keys() -> None:
    env = project_stdio_env(_Server(env={"PATH": "/custom"}), {"PATH": "/usr/bin"})
    assert env["PATH"] == "/custom"


def test_longer_secret_replaced_first() -> None:
    # 否则短值先命中，长的会被切成半截残留。
    out = redact("value=abcdefghij", ["abcdefghij", "abcd"])
    assert "abcdefghij" not in out
    assert "abcd" not in out


# ── 收尾3：进程级登记表 → 出站脱敏收口 ─────────────────────────────────────


def test_register_filters_short_values() -> None:
    reset_registered_secrets()
    assert register_secrets(["abc", "longenough1234"]) == 1
    assert registered_secrets() == ["longenough1234"]


def test_register_is_idempotent_and_sorted_by_length() -> None:
    reset_registered_secrets()
    assert register_secrets(["aaaaaaaa", "bbbbbbbbbbbb"]) == 2
    assert register_secrets(["aaaaaaaa"]) == 0  # 已登记 ⇒ 不重复计数
    assert registered_secrets()[0] == "bbbbbbbbbbbb"  # 长的在前


def test_register_accepts_scalar_and_ignores_junk() -> None:
    reset_registered_secrets()
    assert register_secrets("singlelongvalue") == 1
    assert register_secrets(None) == 0
    assert register_secrets(12345) == 0  # 非字符串集合 ⇒ 不登记
    assert register_secrets(["ok-value-1234"]) == 1


def test_redact_text_covers_registered_secrets() -> None:
    """既有出站脱敏收口（`redact_text`）必须能覆盖登记的凭据 —— 这是收尾3 的全部意义。"""
    from app.subagent.redact import redact_text

    reset_registered_secrets()
    register_secrets(["custom-token-abcdef"])
    out, hits = redact_text("log line: custom-token-abcdef used")
    assert "custom-token-abcdef" not in out
    assert hits >= 1


def test_redact_text_without_registry_is_unchanged() -> None:
    from app.subagent.redact import redact_text

    reset_registered_secrets()
    out, _ = redact_text("nothing sensitive here")
    assert out == "nothing sensitive here"


def test_reset_clears_registry() -> None:
    register_secrets(["some-long-secret"])
    reset_registered_secrets()
    assert registered_secrets() == []
