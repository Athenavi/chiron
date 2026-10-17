"""工具层知识库访问控制 —— kb_list / kb_search / rag_query 与 API 层同一套可见性规则。

这三处各自内联过一份判定（或干脆没判），后果分两类：
- 判得比 API 严（"owner 或 public"）：同租户共享的库在 agent 里检索不到，用户看到的是
  "UI 里能选、agent 说没权限"；
- 判得比 API 松（rag_query 完全不核验 kb_id）：kb_id 由模型给出，提示注入即可换一个
  别人的库 ID 读出其中的文档片段。
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import DEFAULT_TENANT_ID
from app.tools.context import get_all, restore_context, set_tool_context


@pytest.fixture
def tool_ctx():
    before = get_all()
    yield
    restore_context(before)


def _kb_row(**overrides):
    row = {
        "id": "kb-1",
        "type": "rag",
        "user_id": "u-owner",
        "visibility": "tenant",
        "tenant_id": "t-real",
    }
    row.update(overrides)
    return row


class TestKbListToolScope:
    async def test_sql_includes_tenant_shared_kb(self, tool_ctx):
        from app.tools.kb import kb_list

        pool = AsyncMock()
        pool.fetch = AsyncMock(return_value=[])
        set_tool_context(user_id="u-stranger", tenant_id="t-2")
        with patch("app.tools.kb._pool", AsyncMock(return_value=pool)):
            result = await kb_list(query="ops")

        assert result == {"count": 0, "knowledge_bases": []}
        sql, args = pool.fetch.call_args.args[0], pool.fetch.call_args.args[1:]
        assert "visibility = 'tenant' AND tenant_id = $2" in sql
        assert args[0] == "u-stranger"
        assert args[1] == "t-2"
        # 动态拼 name ILIKE 后，limit 仍是最后一个占位符
        assert "name ILIKE $3" in sql
        assert args[-1] == 20

    async def test_blank_caller_tenant_falls_back_to_default(self, tool_ctx):
        from app.tools.kb import kb_list

        pool = AsyncMock()
        pool.fetch = AsyncMock(return_value=[])
        set_tool_context(user_id="u-stranger", tenant_id="")
        with patch("app.tools.kb._pool", AsyncMock(return_value=pool)):
            await kb_list()
        assert pool.fetch.call_args.args[2] == DEFAULT_TENANT_ID


class TestKbSearchToolGate:
    async def test_denies_cross_tenant_before_touching_vectors(self, tool_ctx):
        from app.tools.kb import kb_search

        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=_kb_row(tenant_id="t-real"))
        set_tool_context(user_id="u-stranger", tenant_id="t-other")
        with patch("app.tools.kb._pool", AsyncMock(return_value=pool)), \
             patch("app.main.get_gateway", AsyncMock()) as gw, \
             patch("app.rag.builder.RAGBuilder") as builder_cls:
            result = await kb_search("kb-1", "怎么部署")
        assert result == {"error": "not authorized to query this knowledge base"}
        gw.assert_not_awaited()
        builder_cls.assert_not_called()

    async def test_allows_same_tenant_and_filters_by_kb_tenant(self, tool_ctx):
        from app.tools.kb import kb_search

        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=_kb_row(tenant_id="t-real"))
        fake_builder = MagicMock()
        fake_builder.query = AsyncMock(return_value=[])
        set_tool_context(user_id="u-stranger", tenant_id="t-real")
        with patch("app.tools.kb._pool", AsyncMock(return_value=pool)), \
             patch("app.main.get_gateway", AsyncMock(return_value=MagicMock())), \
             patch("app.rag.builder.RAGBuilder", return_value=fake_builder):
            result = await kb_search("kb-1", "怎么部署")

        assert result == {"type": "rag", "results": []}
        # 过滤值来自 KB 行，而不是调用方自称的租户
        assert fake_builder.query.call_args.kwargs["tenant_id"] == "t-real"

    async def test_null_kb_tenant_is_normalized_for_storage(self, tool_ctx):
        """owner 读自己的存量库（tenant_id 为 NULL）→ 按默认租户过滤，与写入侧同值"""
        from app.tools.kb import kb_search

        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=_kb_row(tenant_id=None))
        fake_builder = MagicMock()
        fake_builder.query = AsyncMock(return_value=[])
        set_tool_context(user_id="u-owner", tenant_id="")
        with patch("app.tools.kb._pool", AsyncMock(return_value=pool)), \
             patch("app.main.get_gateway", AsyncMock(return_value=MagicMock())), \
             patch("app.rag.builder.RAGBuilder", return_value=fake_builder):
            await kb_search("kb-1", "怎么部署")
        assert fake_builder.query.call_args.kwargs["tenant_id"] == DEFAULT_TENANT_ID


class TestRagQueryToolGate:
    @staticmethod
    def _retriever(pool):
        retriever = MagicMock()
        retriever._pg_pool = pool
        retriever.hybrid_search = AsyncMock(return_value=[])
        return retriever

    async def test_denies_foreign_kb_without_searching(self, tool_ctx):
        from app.tools.rag_query import rag_query

        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=_kb_row(visibility="private"))
        retriever = self._retriever(pool)
        set_tool_context(user_id="u-stranger", tenant_id="t-real")
        with patch("app.tools.rag_query._get_hybrid_retriever", AsyncMock(return_value=retriever)):
            result = await rag_query(query="机密", kb_id="kb-1")

        assert result == {"error": "not authorized to query this knowledge base"}
        retriever.hybrid_search.assert_not_awaited()
        # 只查了归属，没有跑默认库那条 SELECT
        assert pool.fetchrow.await_args.args[1] == "kb-1"

    async def test_unknown_kb_reports_not_found(self, tool_ctx):
        from app.tools.rag_query import rag_query

        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=None)
        retriever = self._retriever(pool)
        set_tool_context(user_id="u-owner", tenant_id="t-real")
        with patch("app.tools.rag_query._get_hybrid_retriever", AsyncMock(return_value=retriever)):
            result = await rag_query(query="x", kb_id="kb-missing")
        assert "not found" in result["error"]
        retriever.hybrid_search.assert_not_awaited()

    async def test_no_pool_fails_closed(self, tool_ctx):
        """PG 不可用时不能核验归属 → 拒绝检索（此前的代码照旧按 caller 租户搜向量）"""
        from app.tools.rag_query import rag_query

        retriever = self._retriever(None)
        set_tool_context(user_id="u-owner", tenant_id="t-real")
        with patch("app.tools.rag_query._get_hybrid_retriever", AsyncMock(return_value=retriever)):
            result = await rag_query(query="x", kb_id="kb-1")
        assert "cannot be verified" in result["error"]
        retriever.hybrid_search.assert_not_awaited()

    async def test_owner_search_uses_kb_tenant_not_context(self, tool_ctx):
        from app.tools.rag_query import rag_query

        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=_kb_row(tenant_id="t-real"))
        retriever = self._retriever(pool)
        set_tool_context(user_id="u-owner", tenant_id="t-stale")
        with patch("app.tools.rag_query._get_hybrid_retriever", AsyncMock(return_value=retriever)):
            result = await rag_query(query="x", kb_id="kb-1")
        assert result["count"] == 0
        assert retriever.hybrid_search.await_args.kwargs["tenant_id"] == "t-real"
