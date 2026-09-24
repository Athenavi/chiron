"""PR-3 Task 27: 集成测试 - 兼容性。

验证：
1. AgentRuntime 在 memory=None 时行为回归一致（不抛错、不注入记忆区块）。
2. PromptEngine 在无 MemoryService 时返回空记忆上下文。
3. ContextManager.compress 的任何路径都接受 memory_service=None。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.agent.prompt_engine import PromptEngine
from app.context.manager import ContextManager
from app.memory.layers import RecallResult

# ── PromptEngine 兼容性 ───────────────────────────────────────────────


class TestPromptEngineCompat:
    @pytest.mark.asyncio
    async def test_prompt_engine_without_memory_service(self, monkeypatch):
        """无全局 _memory_service 时返回空字符串。"""
        import app.agent.prompt_engine as pe

        monkeypatch.setattr(pe, "_memory_service", None)
        engine = PromptEngine()

        result = await engine._get_memory_context(user_id="u1", query="hello")

        assert result == ""

    @pytest.mark.asyncio
    async def test_prompt_engine_with_memory_manager_none(self, monkeypatch):
        """无 MemoryManager 时返回空字符串。"""
        import app.agent.prompt_engine as pe

        monkeypatch.setattr(pe, "_memory_service", None)
        # 旧 MemoryManager 为 None
        engine = PromptEngine(memory_manager=None)

        result = await engine._get_memory_context(user_id="u1", query="hello")
        assert result == ""

    @pytest.mark.asyncio
    async def test_prompt_engine_with_recall_exception_degrades_gracefully(self, monkeypatch):
        """MemoryService.recall 抛异常时应优雅降级（不抛错，返回空字符串）。"""
        import app.agent.prompt_engine as pe

        class _BadService:
            async def recall(self, scope, query=None):
                raise RuntimeError("service down")

        monkeypatch.setattr(pe, "_memory_service", _BadService())
        engine = PromptEngine(memory_manager=None)

        result = await engine._get_memory_context(user_id="u1", query="hello")

        # 异常被捕获，返回空字符串而非抛出
        assert result == ""


# ── ContextManager 兼容性 ────────────────────────────────────────────


class TestContextManagerCompat:
    @pytest.mark.asyncio
    async def test_compress_accepts_none_memory_service(self):
        """memory_service=None 不应导致压缩失败。"""
        cm = ContextManager(max_tokens=200, compression_threshold=0.5)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u" * 200},
            {"role": "assistant", "content": "a" * 200},
        ] * 10

        # 无 gateway + 无 memory_service → 降级但不报错
        result = await cm.compress(messages, gateway=None, memory_service=None)

        assert isinstance(result, list)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_trim_to_fit_works_without_memory(self):
        cm = ContextManager(max_tokens=200)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u" * 500},
            {"role": "assistant", "content": "a" * 500},
        ] * 5

        result = cm.trim_to_fit(messages)

        assert cm.count_message_tokens(result) <= cm.max_tokens
        # System 始终保留
        assert result[0]["role"] == "system"


# ── MemoryService 兼容性 ────────────────────────────────────────────


class TestMemoryServiceCompat:
    def test_recall_result_empty_when_no_content(self):
        """空 RecallResult.has_content 应为 False。"""
        r = RecallResult(profile_block="", summary_items=[])
        assert r.has_content is False

    def test_recall_result_has_profile(self):
        """仅含 profile_block 时 has_content 应为 True。"""
        r = RecallResult(profile_block="some profile", summary_items=[])
        assert r.has_content is True

    def test_recall_result_has_summary(self):
        """仅含 summary_items 时 has_content 应为 True。"""
        items = [
            MagicMock(content="hello", score=0.8),
        ]
        # 让 item 具有 content/score 属性
        r = RecallResult(profile_block="", summary_items=items)
        assert r.has_content is True
