"""长期记忆检索的过滤维度必须同时含 tenant 与 user。

回归背景：写入侧明明存了 ``user_id``（见 ``app/memory/manager.py`` 的写入 payload），
但 ``_query_long_term`` 的 filter 只写了 ``tenant_id`` —— 于是**同租户**的其他用户
检索时会命中本用户的长期记忆原文，并把它注入自己的对话上下文。

这是一处"字段有、查询没用"的漏写，而不是设计取舍，所以断言要直接盯住 filter 表达式。

既有的 ``tests/integration/test_memory_isolation.py`` 覆盖了「跨租户」与
「同用户跨租户」，恰好缺「同租户跨用户」这一格 —— 缺口正是在这里。
"""

from __future__ import annotations

import pytest

from app.memory.manager import MemoryManager


class _FakeResp:
    embedding = [0.1] * 8


class _FakeGateway:
    async def embed(self, text: str, model: str) -> _FakeResp:  # noqa: D102
        return _FakeResp()


class _CapturingStore:
    """记录传给向量库的 filter_expr，不真的查。"""

    def __init__(self) -> None:
        self.filter_expr: str | None = None
        self.called = False

    async def search(self, *, collection, query_vector, top_k, threshold, filter_expr):  # noqa: D102
        self.called = True
        self.filter_expr = filter_expr
        return []


def _manager_with_store(store) -> MemoryManager:
    mgr = MemoryManager.__new__(MemoryManager)
    mgr._vector_store = store
    mgr._gateway = _FakeGateway()
    return mgr


@pytest.mark.asyncio
async def test_long_term_filter_includes_user_dimension():
    """filter 必须同时约束 tenant 与 user，缺任一个都会造成泄漏。"""
    store = _CapturingStore()
    await _manager_with_store(store)._query_long_term("tenant-a", "user-a", "q", 5)

    assert store.called, "应当真的向向量库发起检索"
    expr = store.filter_expr
    assert 'tenant_id == "tenant-a"' in expr
    assert 'user_id == "user-a"' in expr, (
        "filter 缺少 user 维度：同租户的其他用户会检索到本用户的记忆"
    )


@pytest.mark.asyncio
async def test_long_term_refuses_unscoped_search_without_user_id():
    """拿不到 user_id 时宁可返回空，也不能退化成"整个租户可见"。"""
    store = _CapturingStore()
    result = await _manager_with_store(store)._query_long_term("tenant-a", "", "q", 5)

    assert result == []
    assert not store.called, "无 user_id 时不应发起未限定用户范围的检索"


@pytest.mark.asyncio
async def test_long_term_filter_escapes_quotes():
    """tenant/user 里的引号不能破坏 Milvus 表达式（否则会退化成宽匹配）。"""
    store = _CapturingStore()
    await _manager_with_store(store)._query_long_term('t"x', 'u"y', "q", 5)

    expr = store.filter_expr
    assert expr.count('\\"') == 2, f"两处引号都应被转义: {expr}"
    assert 'tenant_id == "t\\"x"' in expr
    assert 'user_id == "u\\"y"' in expr
