# -*- coding: utf-8 -*-
"""分支压缩的测试（纯逻辑 + 假 gateway，不碰真实模型/DB）。

盯住三件事（都是"错了也看不出来、但用户会踩到"的地方）：
1. 压缩区与原文保留区的切分边界；
2. **永远有摘要**：模型不可用要降级成提取式，而不是让分支拿不到任何上下文；
3. 提示词预算：超长历史要保首尾（目标在最前、进展在最后）。
"""

from __future__ import annotations

import types

import pytest

from app.context.branch_condense import (
    MIN_COMPRESSIBLE_MESSAGES,
    condense_messages,
    extractive_summary,
    render_messages,
    split_head_tail,
)


def _msgs(n: int) -> list[dict]:
    return [{"role": "user" if i % 2 else "assistant", "content": "m%d" % i} for i in range(n)]


class _FakeGateway:
    """记录调用参数、可控返回/抛错的假 gateway。"""

    def __init__(self, content: str = "## 目标\n- 继续", fail: bool = False) -> None:
        self.content = content
        self.fail = fail
        self.calls: list[list[dict]] = []

    async def chat(self, *, messages, model, max_tokens):  # noqa: ANN001, ARG002
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("provider down")
        return types.SimpleNamespace(
            content=self.content,
            usage=types.SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
        )


class TestSplitHeadTail:
    def test_keep_tail_zero_means_compress_everything(self):
        head, tail = split_head_tail(_msgs(5), 0)
        assert len(head) == 5
        assert tail == []

    def test_keep_tail_splits_from_the_end(self):
        head, tail = split_head_tail([{"content": "a"}, {"content": "b"}, {"content": "c"}], 2)
        assert [m["content"] for m in head] == ["a"]
        assert [m["content"] for m in tail] == ["b", "c"]

    def test_keep_tail_not_smaller_than_messages(self):
        head, tail = split_head_tail(_msgs(3), 10)
        assert head == []
        assert len(tail) == 3

    def test_negative_keep_tail_is_treated_as_zero(self):
        head, tail = split_head_tail(_msgs(4), -1)
        assert len(head) == 4
        assert tail == []


class TestRenderMessages:
    def test_renders_role_and_skips_empty_content(self):
        text = render_messages([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "  "}])
        assert text == "[user] hi"

    def test_over_budget_keeps_head_and_tail(self):
        messages = [{"role": "user", "content": "A" * 500}, {"role": "user", "content": "B" * 500}]
        text = render_messages(messages, limit_chars=100)
        assert "中段省略" in text
        assert text.startswith("[user] A")
        assert text.endswith("B" * 10)


class TestExtractiveSummary:
    def test_keeps_first_line_of_each_message(self):
        summary = extractive_summary(
            [{"role": "user", "content": "第一行\n第二行"}, {"role": "assistant", "content": "结论"}]
        )
        assert "第一行" in summary
        assert "第二行" not in summary
        assert "结论" in summary

    def test_empty_messages_still_produce_a_summary(self):
        assert "无可提取内容" in extractive_summary([])


class TestCondenseMessages:
    @pytest.mark.asyncio
    async def test_too_small_compressible_means_no_condense(self):
        """压缩区不足：不调模型、condensed=False（Go 侧据此直接置 ready）。"""
        gateway = _FakeGateway()
        result = await condense_messages(
            messages=_msgs(MIN_COMPRESSIBLE_MESSAGES - 1 + 4), gateway=gateway, keep_tail=4
        )
        assert result.condensed is False
        assert result.reason == "compressible_too_small"
        assert gateway.calls == []

    @pytest.mark.asyncio
    async def test_uses_llm_summary_and_usage(self):
        gateway = _FakeGateway(content="## 目标\n- 把分支做出来")
        result = await condense_messages(messages=_msgs(10), gateway=gateway, keep_tail=4)
        assert result.condensed is True
        assert result.degraded is False
        assert result.summary.startswith("## 目标")
        assert result.usage["total_tokens"] == 18
        assert result.source_messages == 6

    @pytest.mark.asyncio
    async def test_gateway_failure_degrades_instead_of_failing(self):
        """模型挂了也必须给出一份摘要 —— 分支不能因此变成空会话。"""
        gateway = _FakeGateway(fail=True)
        result = await condense_messages(messages=_msgs(10), gateway=gateway, keep_tail=4)
        assert result.condensed is True
        assert result.degraded is True
        assert "提取式降级摘要" in result.summary

    @pytest.mark.asyncio
    async def test_no_gateway_goes_straight_to_extractive(self):
        result = await condense_messages(messages=_msgs(10), gateway=None, keep_tail=4)
        assert result.degraded is True
        assert result.summary != ""

    @pytest.mark.asyncio
    async def test_include_future_appends_future_block(self):
        gateway = _FakeGateway()
        await condense_messages(
            messages=_msgs(10),
            gateway=gateway,
            keep_tail=4,
            include_future=True,
            future_messages=[{"role": "user", "content": "后来我们改了方案"}],
        )
        user_prompt = gateway.calls[0][1]["content"]
        assert "<后续>" in user_prompt
        assert "后来我们改了方案" in user_prompt
        assert "后续走向" in gateway.calls[0][0]["content"]

    @pytest.mark.asyncio
    async def test_future_block_absent_by_default(self):
        gateway = _FakeGateway()
        await condense_messages(
            messages=_msgs(10),
            gateway=gateway,
            keep_tail=4,
            future_messages=[{"role": "user", "content": "不该出现"}],
        )
        user_prompt = gateway.calls[0][1]["content"]
        assert "<后续>" not in user_prompt
        assert "不该出现" not in user_prompt
