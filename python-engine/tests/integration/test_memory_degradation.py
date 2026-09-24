"""PR-3 Task 26: 集成测试 - 降级链。

验证 ContextManager.compress 的降级流程：
1. LLM 摘要可用时走正常路径。
2. LLM 摘要失败时重试 1 次。
3. 重试仍失败时降级到提取式摘要（degraded=True）。
4. 硬截断路径（trim_to_fit）在仍超预算时被触发。
5. 降级模式下后台补交被压缩内容到 L3。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.context.manager import ContextManager

# ── Helpers ──────────────────────────────────────────────────────────


def _make_messages(n_user_turns: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": "You are helpful."}]
    for i in range(n_user_turns):
        messages.append({"role": "user", "content": f"user-turn-{i}: " + ("u" * 200)})
        messages.append({"role": "assistant", "content": f"assistant-turn-{i}: " + ("a" * 200)})
    return messages


class _SuccessGateway:
    """正常返回摘要的 gateway。"""

    async def chat(self, messages, model=None, max_tokens=None):
        return MagicMock(content="This is a good summary.")


class _FailingGateway:
    """始终抛异常的 gateway。"""

    async def chat(self, messages, model=None, max_tokens=None):
        raise RuntimeError("LLM unavailable")


class _FailThenSucceedGateway:
    """首次失败，二次重试成功。"""

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, model=None, max_tokens=None):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary failure")
        return MagicMock(content="Retry succeeded summary.")


class _BudgetBlowGateway:
    """返回超长摘要导致仍超预算。"""

    async def chat(self, messages, model=None, max_tokens=None):
        return MagicMock(content="x" * 4000)  # 超长摘要


# ── Tests ─────────────────────────────────────────────────────────────


class TestDegradationChain:
    @pytest.mark.asyncio
    async def test_llm_summary_normal_path(self):
        cm = ContextManager(max_tokens=4000, compression_threshold=0.5)
        original = _make_messages(20)

        compressed = await cm.compress(original, gateway=_SuccessGateway())

        assert len(compressed) < len(original)
        # 正常路径下摘要应为 LLM 产出
        assert "This is a good summary." in compressed[1]["content"]
        # 不应标记 degraded
        assert "[DEGRADED MODE]" not in compressed[1]["content"]

    @pytest.mark.asyncio
    async def test_llm_failure_retries_once(self):
        cm = ContextManager(max_tokens=4000, compression_threshold=0.5)
        original = _make_messages(20)
        gw = _FailThenSucceedGateway()

        compressed = await cm.compress(original, gateway=gw)

        # 发生了两次调用（首次失败 + 重试成功）
        assert gw.calls == 2
        # 摘要应使用第二次成功结果
        assert "Retry succeeded summary." in compressed[1]["content"]
        # 未降级
        assert "[DEGRADED MODE]" not in compressed[1]["content"]

    @pytest.mark.asyncio
    async def test_llm_both_fail_degrades_to_extractive(self):
        cm = ContextManager(max_tokens=4000, compression_threshold=0.5)
        original = _make_messages(20)

        compressed = await cm.compress(original, gateway=_FailingGateway())

        # 摘要块标记为降级模式
        assert "[DEGRADED MODE" in compressed[1]["content"]
        # 仍包含 Context Summary 头部
        assert "Context Summary" in compressed[1]["content"]
        # 摘要内容应来自提取式（不含 LLM 输出）
        assert "This is a good summary." not in compressed[1]["content"]

    @pytest.mark.asyncio
    async def test_no_gateway_uses_extractive(self):
        cm = ContextManager(max_tokens=4000, compression_threshold=0.5)
        original = _make_messages(20)

        compressed = await cm.compress(original, gateway=None)

        assert "[DEGRADED MODE" in compressed[1]["content"]

    @pytest.mark.asyncio
    async def test_hard_trim_when_still_over_budget(self):
        """摘要后仍超预算，应触发 trim_to_fit。"""
        cm = ContextManager(max_tokens=500, compression_threshold=0.5)
        # 构造大量消息，确保即便摘要后仍超预算
        original = _make_messages(30)

        compressed = await cm.compress(original, gateway=_BudgetBlowGateway())

        # 最终 token 数必须 ≤ max_tokens
        assert cm.count_message_tokens(compressed) <= cm.max_tokens
        # System 始终保留
        assert compressed[0]["role"] == "system"

    @pytest.mark.asyncio
    async def test_degraded_flag_set_when_no_gateway(self):
        """提取式摘要路径应标记 degraded。"""
        cm = ContextManager(max_tokens=4000, compression_threshold=0.5)
        original = _make_messages(20)

        compressed = await cm.compress(original, gateway=None)
        assert "[DEGRADED MODE" in compressed[1]["content"]

    @pytest.mark.asyncio
    async def test_degraded_content_submitted_to_l3(self):
        """降级模式下被压缩的消息应提交到 L3 巩固。"""
        from app.memory.layers import Scope

        class _FakeMemoryService:
            def __init__(self):
                self.saved: list[dict[str, Any]] = []

            async def save_summary(self, scope, content, topics=None):
                self.saved.append({
                    "scope": scope,
                    "content": content,
                    "topics": topics,
                })
                return None

        svc = _FakeMemoryService()
        # 给消息加上 tenant_id/user_id 以辅助 scope 推断
        original = _make_messages(20)
        for m in original:
            m["tenant_id"] = "t1"
            m["user_id"] = "u1"

        ContextManager(max_tokens=500, compression_threshold=0.5)
        # 为了让同步断言 save_summary 被调用，直接调用 _submit_degraded_content
        # （compression 中走 asyncio.create_task，单元级验证直接调用更稳定）
        # 取中间段（去掉 system + tail）模拟被压缩段
        middle = original[1:-8]
        await ContextManager._submit_degraded_content(middle, svc)

        assert len(svc.saved) == 1
        saved = svc.saved[0]
        assert saved["topics"] == ["degraded_compaction"]
        assert isinstance(saved["scope"], Scope)
        assert saved["scope"].tenant_id == "t1"
        assert saved["scope"].user_id == "u1"

    @pytest.mark.asyncio
    async def test_submit_degraded_handles_missing_scope(self):
        """消息无 tenant_id/user_id 时，应回退到 default。"""
        class _FakeMemoryService:
            def __init__(self):
                self.saved: list[dict[str, Any]] = []

            async def save_summary(self, scope, content, topics=None):
                self.saved.append({"scope": scope})
                return None

        svc = _FakeMemoryService()
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        await ContextManager._submit_degraded_content(messages, svc)

        assert len(svc.saved) == 1
        scope = svc.saved[0]["scope"]
        assert scope.tenant_id == "default"
        assert scope.user_id == "unknown"

    @pytest.mark.asyncio
    async def test_submit_degraded_no_convo_messages(self):
        """无 user/assistant 内容时应跳过保存。"""
        class _FakeMemoryService:
            def __init__(self):
                self.called = False

            async def save_summary(self, *args, **kwargs):
                self.called = True

        svc = _FakeMemoryService()
        messages = [{"role": "system", "content": "only system"}]
        await ContextManager._submit_degraded_content(messages, svc)
        assert svc.called is False
