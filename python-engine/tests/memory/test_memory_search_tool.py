"""Task 29: 单元测试 - memory_search 工具。

覆盖：
1. 输入校验（空 query、超长 query、limit 规范化）
2. MemoryService 调用（top_k 透传、异常降级）
3. Token/字符预算控制（单条截断、总量截断、truncated 标记）
4. 用户上下文校验（缺 user_id / 缺 service）
5. 空结果场景
"""

from __future__ import annotations

import pytest

from app.memory.layers import RecalledItem, RecallResult
from app.tools import context as tool_context

# ── 测试基建 ────────────────────────────────────────────────────────


class FakeMemoryService:
    """MemoryService 的可观测假实现。

    签名与 ``MemoryService.recall(tenant_id, user_id, query, top_k)`` 保持一致 ——
    假件与真件的签名一旦分叉，测试通过与否就取决于「谁被替换成了谁」而不是行为。
    """

    def __init__(self, recall_result=None, should_fail=False):
        self._recall_result = recall_result or RecallResult(
            profile_block="", summary_items=[]
        )
        self._should_fail = should_fail
        self.recall_called = False
        self.last_tenant_id = None
        self.last_user_id = None
        self.last_query = None
        self.last_top_k = None

    async def recall(self, tenant_id, user_id, query="", top_k=5):
        self.recall_called = True
        self.last_tenant_id = tenant_id
        self.last_user_id = user_id
        self.last_query = query
        self.last_top_k = top_k
        if self._should_fail:
            raise RuntimeError("recall failed")
        return self._recall_result


def _make_item(item_id: str, content: str, score: float = 0.9,
               session_id: str = "sess-001", topics=None) -> RecalledItem:
    return RecalledItem(
        id=item_id,
        content=content,
        topics=topics or [],
        entities={},
        turn_range=(0, 5),
        session_id=session_id,
        access_count=0,
        last_accessed_at=0.0,
        created_at=0.0,
        score=score,
    )


@pytest.fixture
def fake_svc():
    return FakeMemoryService()


# ── 1. 输入校验 ──────────────────────────────────────────────────────


class TestInputValidation:
    @pytest.mark.asyncio
    async def test_empty_query_returns_error(self, monkeypatch):
        from app.tools.memory import memory_search
        svc = FakeMemoryService()
        monkeypatch.setattr("app.memory.service.get_memory_service", lambda: svc)
        tool_context.set_tool_context(user_id="u1", tenant_id="t1")
        try:
            result = await memory_search("")
            assert "query is required" in result["error"]
            assert result["results"] == []
        finally:
            tool_context.restore_context({})

    @pytest.mark.asyncio
    async def test_query_exceeds_max_length_rejected(self, monkeypatch):
        from app.tools.memory import _MEMORY_SEARCH_MAX_QUERY, memory_search
        svc = FakeMemoryService()
        monkeypatch.setattr("app.memory.service.get_memory_service", lambda: svc)
        tool_context.set_tool_context(user_id="u1", tenant_id="t1")
        try:
            long_query = "a" * (_MEMORY_SEARCH_MAX_QUERY + 1)
            result = await memory_search(long_query)
            assert "exceeds" in result["error"]
            assert result["results"] == []
            # 不应调用 service
            assert svc.recall_called is False
        finally:
            tool_context.restore_context({})

    @pytest.mark.asyncio
    async def test_limit_below_1_normalized_to_1(self, monkeypatch):
        from app.tools.memory import memory_search
        items = [_make_item("m1", "content", 0.9)]
        svc = FakeMemoryService(RecallResult(profile_block="", summary_items=items))
        monkeypatch.setattr("app.memory.service.get_memory_service", lambda: svc)
        tool_context.set_tool_context(user_id="u1", tenant_id="t1")
        try:
            await memory_search("test", limit=0)
            assert svc.last_top_k == 1
        finally:
            tool_context.restore_context({})

    @pytest.mark.asyncio
    async def test_limit_above_20_clamped_to_20(self, monkeypatch):
        from app.tools.memory import memory_search
        svc = FakeMemoryService()
        monkeypatch.setattr("app.memory.service.get_memory_service", lambda: svc)
        tool_context.set_tool_context(user_id="u1", tenant_id="t1")
        try:
            await memory_search("test", limit=1000)
            assert svc.last_top_k == 20
        finally:
            tool_context.restore_context({})

    @pytest.mark.asyncio
    async def test_limit_invalid_type_falls_back_to_default(self, monkeypatch):
        from app.tools.memory import memory_search
        svc = FakeMemoryService()
        monkeypatch.setattr("app.memory.service.get_memory_service", lambda: svc)
        tool_context.set_tool_context(user_id="u1", tenant_id="t1")
        try:
            await memory_search("test", limit="not-a-number")
            assert svc.last_top_k == 10
        finally:
            tool_context.restore_context({})


# ── 2. MemoryService 调用 ────────────────────────────────────────────


class TestServiceInvocation:
    @pytest.mark.asyncio
    async def test_passes_top_k_to_recall(self, monkeypatch):
        from app.tools.memory import memory_search
        svc = FakeMemoryService(RecallResult(profile_block="", summary_items=[]))
        monkeypatch.setattr("app.memory.service.get_memory_service", lambda: svc)
        tool_context.set_tool_context(user_id="u1", tenant_id="t1", session_id="sess-001")
        try:
            await memory_search("hello", limit=5)
            assert svc.recall_called is True
            assert svc.last_query == "hello"
            assert svc.last_top_k == 5
            assert svc.last_user_id == "u1"
            assert svc.last_tenant_id == "t1"
        finally:
            tool_context.restore_context({})

    @pytest.mark.asyncio
    async def test_recall_exception_returns_error(self, monkeypatch):
        from app.tools.memory import memory_search
        svc = FakeMemoryService(should_fail=True)
        monkeypatch.setattr("app.memory.service.get_memory_service", lambda: svc)
        tool_context.set_tool_context(user_id="u1", tenant_id="t1")
        try:
            result = await memory_search("test")
            assert "error" in result
            assert "results" in result
            assert result["results"] == []
        finally:
            tool_context.restore_context({})

    @pytest.mark.asyncio
    async def test_no_user_context_returns_error(self, monkeypatch):
        from app.tools.memory import memory_search
        svc = FakeMemoryService()
        monkeypatch.setattr("app.memory.service.get_memory_service", lambda: svc)
        tool_context.restore_context({})
        result = await memory_search("test")
        assert "user context" in result["error"]
        assert result["results"] == []
        assert svc.recall_called is False

    @pytest.mark.asyncio
    async def test_no_service_returns_not_available(self, monkeypatch):
        from app.tools.memory import memory_search
        monkeypatch.setattr("app.memory.service.get_memory_service", lambda: None)
        tool_context.set_tool_context(user_id="u1", tenant_id="t1")
        try:
            result = await memory_search("test")
            assert "not available" in result["output"]
            assert result["results"] == []
        finally:
            tool_context.restore_context({})


# ── 3. Token/字符预算控制 ────────────────────────────────────────────


class TestCharacterBudget:
    @pytest.mark.asyncio
    async def test_single_item_truncated_at_600_chars(self, monkeypatch):
        from app.tools.memory import memory_search
        long_content = "x" * 1500
        items = [_make_item("m1", long_content, 0.9)]
        svc = FakeMemoryService(RecallResult(profile_block="", summary_items=items))
        monkeypatch.setattr("app.memory.service.get_memory_service", lambda: svc)
        tool_context.set_tool_context(user_id="u1", tenant_id="t1")
        try:
            result = await memory_search("test", limit=1)
            assert len(result["results"]) == 1
            assert len(result["results"][0]["content"]) <= 600
            assert result["results"][0]["content"].endswith("…")
        finally:
            tool_context.restore_context({})

    @pytest.mark.asyncio
    async def test_total_budget_triggers_early_termination(self, monkeypatch):
        from app.tools.memory import _MEMORY_SEARCH_MAX_CHARS, memory_search
        # 每条 400 字符（< 597 阈值，不触发单条截断），共 20 条
        # 预算 6000 字符 → 最多 15 条（400*15=6000）
        items = [_make_item(f"m{i}", "a" * 400, 0.9 - i * 0.01) for i in range(20)]
        svc = FakeMemoryService(RecallResult(profile_block="", summary_items=items))
        monkeypatch.setattr("app.memory.service.get_memory_service", lambda: svc)
        tool_context.set_tool_context(user_id="u1", tenant_id="t1")
        try:
            result = await memory_search("test", limit=20)
            # 因字符预算限制，不能返回全部 20 条
            assert len(result["results"]) < 20
            # 总字符数不应超预算
            total = sum(len(r["content"]) for r in result["results"])
            assert total <= _MEMORY_SEARCH_MAX_CHARS
        finally:
            tool_context.restore_context({})

    @pytest.mark.asyncio
    async def test_total_budget_not_triggered_for_small_items(self, monkeypatch):
        from app.tools.memory import memory_search
        items = [_make_item(f"m{i}", f"short content {i}", 0.9) for i in range(3)]
        svc = FakeMemoryService(RecallResult(profile_block="", summary_items=items))
        monkeypatch.setattr("app.memory.service.get_memory_service", lambda: svc)
        tool_context.set_tool_context(user_id="u1", tenant_id="t1")
        try:
            result = await memory_search("test", limit=3)
            assert len(result["results"]) == 3
            assert result["truncated"] is False
        finally:
            tool_context.restore_context({})


# ── 4. 结果格式与空结果 ────────────────────────────────────────────


class TestResultFormat:
    @pytest.mark.asyncio
    async def test_empty_summary_items_returns_no_results(self, monkeypatch):
        from app.tools.memory import memory_search
        svc = FakeMemoryService(RecallResult(profile_block="", summary_items=[]))
        monkeypatch.setattr("app.memory.service.get_memory_service", lambda: svc)
        tool_context.set_tool_context(user_id="u1", tenant_id="t1")
        try:
            result = await memory_search("test")
            assert result["count"] == 0
            assert result["results"] == []
            assert "No semantic memories" in result["output"]
        finally:
            tool_context.restore_context({})

    @pytest.mark.asyncio
    async def test_result_shape_contains_expected_fields(self, monkeypatch):
        from app.tools.memory import memory_search
        items = [_make_item("m1", "Python asyncio", 0.92, topics=["Python", "asyncio"])]
        svc = FakeMemoryService(RecallResult(profile_block="", summary_items=items))
        monkeypatch.setattr("app.memory.service.get_memory_service", lambda: svc)
        tool_context.set_tool_context(user_id="u1", tenant_id="t1")
        try:
            result = await memory_search("Python")
            assert result["count"] == 1
            r0 = result["results"][0]
            assert "id" in r0
            assert "content" in r0
            assert "topics" in r0
            assert "score" in r0
            assert "session_id" in r0
            assert "created_at" in r0
            assert r0["score"] == 0.92
            assert "Python" in r0["topics"]
        finally:
            tool_context.restore_context({})

    @pytest.mark.asyncio
    async def test_result_count_reflects_returned_not_total(self, monkeypatch):
        from app.tools.memory import memory_search
        # 10 条结果，limit=3，应只返回 3 条，count=3
        items = [_make_item(f"m{i}", f"c{i}", 0.9 - i * 0.01) for i in range(10)]
        svc = FakeMemoryService(RecallResult(profile_block="", summary_items=items))
        monkeypatch.setattr("app.memory.service.get_memory_service", lambda: svc)
        tool_context.set_tool_context(user_id="u1", tenant_id="t1")
        try:
            result = await memory_search("test", limit=3)
            assert result["count"] == 3
            assert len(result["results"]) == 3
        finally:
            tool_context.restore_context({})

    @pytest.mark.asyncio
    async def test_missing_summary_items_attribute_handled(self, monkeypatch):
        """summary_items 属性缺失时应安全降级为空列表。"""
        from app.tools.memory import memory_search

        class WeirdResult:
            profile_block = "profile only"

        svc = FakeMemoryService(WeirdResult())
        monkeypatch.setattr("app.memory.service.get_memory_service", lambda: svc)
        tool_context.set_tool_context(user_id="u1", tenant_id="t1")
        try:
            result = await memory_search("test")
            assert result["count"] == 0
            assert result["results"] == []
        finally:
            tool_context.restore_context({})
