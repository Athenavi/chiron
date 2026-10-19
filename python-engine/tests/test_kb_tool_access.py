"""工具层知识库访问控制 —— kb_list / kb_search 与 API 层同一套可见性规则。

规则本体在 ``app.kb_access``（``kb_access_allowed``），工具层只做"校验 + 取数"。
**provider 解析用 KB 所有者**（Cloud API key 是用户级的，只有属主配过），调用者身份
只用于权限判定 —— 两件事混用会得到"能看见库却查不了"。

旧版这里还覆盖 ``rag_query``（不核验 kb_id、只按调用方自称的租户过滤，提示注入即可
换一个别人的库 ID）：该工具连同向量检索一起退场，用例随之移除。
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.config import DEFAULT_TENANT_ID
from app.knowledge.providers.base import AnswerResult, Citation
from app.tools.context import get_all, restore_context, set_tool_context


@pytest.fixture
def tool_ctx():
    before = get_all()
    yield
    restore_context(before)


def _kb_row(**overrides):
    row = {
        "id": "kb-1",
        "provider": "cloud",
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
    async def test_denies_cross_tenant_before_resolving_provider(self, tool_ctx):
        from app.tools.kb import kb_search

        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=_kb_row(tenant_id="t-real"))
        set_tool_context(user_id="u-stranger", tenant_id="t-other")
        with (
            patch("app.tools.kb._pool", AsyncMock(return_value=pool)),
            patch("app.tools.kb.answer_question", AsyncMock()) as ask,
        ):
            result = await kb_search("kb-1", "怎么部署")
        assert result == {"error": "not authorized to query this knowledge base"}
        ask.assert_not_awaited()

    async def test_uses_kb_owner_for_provider_resolution(self, tool_ctx):
        from app.tools.kb import kb_search

        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=_kb_row(tenant_id="t-real"))
        ask = AsyncMock(
            return_value=(
                AnswerResult(answer="答案", citations=[Citation(doc_name="a.pdf", page=2)]),
                "cloud",
            )
        )
        set_tool_context(user_id="u-stranger", tenant_id="t-real")
        with (
            patch("app.tools.kb._pool", AsyncMock(return_value=pool)),
            patch("app.tools.kb.answer_question", ask),
        ):
            result = await kb_search("kb-1", "怎么部署")

        assert result["answer"] == "答案"
        assert result["count"] == 1
        assert result["citations"][0]["page"] == 2
        # provider 解析（即 Cloud key 的来源）必须是 KB 属主，而不是调用方
        assert ask.await_args.kwargs["user_id"] == "u-owner"
        assert ask.await_args.kwargs["kb_id"] == "kb-1"


class TestKbSearchErrorSurfacing:
    async def test_provider_failure_is_returned_not_raised(self, tool_ctx):
        from app.knowledge.errors import ProviderUnavailableError
        from app.tools.kb import kb_search

        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=_kb_row())
        set_tool_context(user_id="u-owner", tenant_id="t-real")
        with (
            patch("app.tools.kb._pool", AsyncMock(return_value=pool)),
            patch(
                "app.tools.kb.answer_question",
                AsyncMock(side_effect=ProviderUnavailableError("provider 未配置")),
            ),
        ):
            result = await kb_search("kb-1", "q")
        assert result["error"] == "provider 未配置"
        assert result["provider"] == "cloud"
