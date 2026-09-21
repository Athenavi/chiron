"""OpenCode 的 `x-opencode-session` 头必须被注入（且只对 OpenCode 注入）。

回归背景（真实故障）：OpenCode Go 强制要求该头，缺失时直接

    400 MissingSessionID: Request is missing x-opencode-session and cannot be routed
    efficiently. Please see https://opencode.ai/docs/go/#where-can-i-use-it

而 Chiron 此前完全不发这个头 —— 用户把 provider 配好、模型也选对了，请求照样失败。
该头只影响对方的路由与 prompt 缓存，所以用「同一会话的稳定 ID」注入最合适。
"""

from app.providers.named import NamedOpenAIProvider
from app.providers.session_context import set_llm_session_id


def _provider(name: str) -> NamedOpenAIProvider:
    return NamedOpenAIProvider(name, api_key="sk-fake-for-test")


def test_opencode_go_injects_session_header():
    set_llm_session_id("sess-abc-123")
    kwargs = _provider("opencode-go")._build_kwargs([], "glm-5", 1024, 0.5, None)
    assert kwargs["extra_headers"] == {"x-opencode-session": "sess-abc-123"}


def test_opencode_zen_variants_also_inject():
    """四个 OpenCode 变体（Zen/Go × openai/anthropic）名字都以 opencode 开头。"""
    set_llm_session_id("sess-x")
    for name in ("opencode", "opencode-anthropic", "opencode-go", "opencode-go-anthropic"):
        kwargs = _provider(name)._build_kwargs([], "m", 100, 0.0, None)
        assert kwargs.get("extra_headers") == {"x-opencode-session": "sess-x"}, name


def test_other_providers_get_no_session_header():
    """其他 provider 不应被塞这个头（避免把内部标识泄露给无关第三方）。"""
    set_llm_session_id("sess-abc")
    kwargs = _provider("deepseek")._build_kwargs([], "deepseek-chat", 1024, 0.5, None)
    assert "extra_headers" not in kwargs


def test_no_session_id_means_no_header():
    """会话未知时不注入（行为与旧版本一致，由对方自行决定是否拒绝）。"""
    set_llm_session_id("")
    kwargs = _provider("opencode-go")._build_kwargs([], "glm-5", 100, 0.0, None)
    assert "extra_headers" not in kwargs
