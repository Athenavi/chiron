"""L3 巩固正文的生成式产出（`app/memory/summariser.py`）。

这一层是 §5.1 里"A3"的那一半：正文过去只由 `_extract_summary` 拼每条消息的前 200 字符，
那是一段被截断的原文而不是摘要，于是近重复判定比的是互相重叠的两段原文。这里只管三件事：
失败时**不挡写入**、模型说"没什么值得记"时**别拿降级正文顶上**、以及预算要的租户**要传到**。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import settings
from app.memory.summariser import (
    NothingWorthRememberingError,
    build_summariser,
    render_conversation,
    summarise_messages,
)

MSGS: list[dict[str, Any]] = [
    {"role": "system", "content": "你是助手"},
    {"role": "user", "content": "我们把网关切成 Go 了"},
    {"role": "assistant", "content": "好的，记到 docs/gateway.md"},
]


class _Gateway:
    """只记录调用、按脚本回话的网关替身。

    失败形状用的是 `ChatResponse(finish_reason="error")` 而不是异常 —— `app/gateway/router.py`
    的 `chat()` 就是这么回错的，把它当异常处理会永远走不到那条分支。
    """

    def __init__(self, *, content: Any = "", finish_reason: str = "", raises: bool = False):
        self.calls: list[dict[str, Any]] = []
        self._content = content
        self._finish_reason = finish_reason
        self._raises = raises

    async def chat(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._raises:
            raise RuntimeError("gateway unreachable")
        return SimpleResponse(self._content, self._finish_reason)


class SimpleResponse:
    def __init__(self, content: Any, finish_reason: str = ""):
        self.content = content
        self.finish_reason = finish_reason


class TestRenderConversation:
    def test_system_and_tool_lines_are_dropped(self):
        text = render_conversation(MSGS)
        assert "[user] 我们把网关切成 Go 了" in text
        assert "[assistant]" in text
        assert "你是助手" not in text, "系统提示不是会话内容，进模型只会挤占预算"

    def test_head_is_kept_not_tail(self):
        """取头部：巩固的是滑出窗口的整段对话，决定与标识符通常在开头。

        （`distill.render_transcript` 相反，它看的是刚刚这一回合。）
        """
        msgs = [{"role": "user", "content": f"m{i} " + "x" * 40} for i in range(10)]
        text = render_conversation(msgs, max_chars=120)
        assert text.startswith("[user] m0")
        assert "m9" not in text

    def test_blank_content_is_skipped(self):
        assert render_conversation([{"role": "user", "content": "   "}]) == ""


class TestSummariseMessages:
    async def test_no_gateway_returns_empty(self):
        assert await summarise_messages(MSGS, gateway=None, model="m") == ""

    async def test_empty_transcript_does_not_call_the_model(self):
        gw = _Gateway(content="不该被调用")
        out = await summarise_messages([{"role": "system", "content": "只有系统"}], gateway=gw, model="m")
        assert out == ""
        assert gw.calls == []

    async def test_tenant_id_reaches_the_gateway(self):
        """预算预检与扣减只在 `tenant_id` 非空时发生 —— 不传等于让后台任务绕过租户预算。"""
        gw = _Gateway(content="决定：网关改用 Go。")
        await summarise_messages(MSGS, gateway=gw, model="gpt-x", tenant_id="t7")
        assert gw.calls[0]["tenant_id"] == "t7"
        assert gw.calls[0]["model"] == "gpt-x"

    async def test_raising_gateway_returns_empty(self):
        gw = _Gateway(raises=True)
        assert await summarise_messages(MSGS, gateway=gw, model="m") == ""

    async def test_error_finish_reason_returns_empty(self):
        gw = _Gateway(content="半截摘要", finish_reason="error")
        assert await summarise_messages(MSGS, gateway=gw, model="m") == ""

    async def test_blank_content_returns_empty(self):
        gw = _Gateway(content="   ")
        assert await summarise_messages(MSGS, gateway=gw, model="m") == ""

    async def test_returned_text_is_the_model_answer(self):
        gw = _Gateway(content="  决定：网关改用 Go。  ")
        assert await summarise_messages(MSGS, gateway=gw, model="m") == "决定：网关改用 Go。"

    @pytest.mark.parametrize("answer", ["NONE", "none.", "  NONE ", "N/A。", "(no content)"])
    async def test_none_answer_is_a_judgement_not_a_summary(self, answer):
        """命中约定回话就抛：模型说"没值得记的"，把一段截断原文存进长期记忆正是噪声场。"""
        gw = _Gateway(content=answer)
        with pytest.raises(NothingWorthRememberingError):
            await summarise_messages(MSGS, gateway=gw, model="m")


class TestBuildSummariser:
    async def test_disabled_switch_never_calls_the_gateway(self, monkeypatch):
        monkeypatch.setattr(settings, "memory_summariser_enabled", False)
        gw = _Gateway(content="摘要")
        assert await build_summariser(gw)(MSGS, "t1") == ""
        assert gw.calls == []

    async def test_model_defaults_to_default_model(self, monkeypatch):
        monkeypatch.setattr(settings, "default_model", "default-chat-model")
        gw = _Gateway(content="摘要")
        await build_summariser(gw)(MSGS, "t1")
        assert gw.calls[0]["model"] == "default-chat-model"

    async def test_configured_model_wins_over_the_fallback(self, monkeypatch):
        monkeypatch.setattr(settings, "memory_summariser_model", "cheap-model")
        gw = _Gateway(content="摘要")
        await build_summariser(gw, model="")(MSGS, "t1")
        assert gw.calls[0]["model"] == "cheap-model"

    async def test_explicit_model_beats_the_config(self, monkeypatch):
        monkeypatch.setattr(settings, "memory_summariser_model", "cheap-model")
        gw = _Gateway(content="摘要")
        await build_summariser(gw, model="hand-picked")(MSGS, "t1")
        assert gw.calls[0]["model"] == "hand-picked"

    async def test_per_call_tenant_overrides_the_assembly_default(self):
        """装配时的 tenant_id 只是兜底：扣预算要按发生那次巩固所属的租户记。"""
        gw = _Gateway(content="摘要")
        await build_summariser(gw, tenant_id="assembly-tenant")(MSGS, "runtime-tenant")
        assert gw.calls[0]["tenant_id"] == "runtime-tenant"

    async def test_limits_come_from_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "memory_summariser_max_tokens", 111)
        monkeypatch.setattr(settings, "memory_summariser_max_input_chars", 30)
        gw = _Gateway(content="摘要")
        long_msgs = [{"role": "user", "content": "y" * 500}]
        await build_summariser(gw)(long_msgs, "t1")
        assert gw.calls[0]["max_tokens"] == 111
        assert len(gw.calls[0]["messages"][1]["content"]) == 30
