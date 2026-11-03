"""互通冒烟测试：kb 工具注册与工具链可见性。"""
from __future__ import annotations

import pytest

import app.main  # noqa: F401 — 初始化 app 包，避免循环导入
from app.tools.context import set_tool_context
from app.tools.registry import registry

# ── 知识库工具注册（kb_list / kb_search 对工具链可见） ────────────────────

def test_kb_tools_registered():
    assert registry.get("kb_search") is not None
    assert registry.get("kb_list") is not None


@pytest.mark.asyncio
async def test_kb_search_requires_auth_context():
    # 无用户上下文时拒绝（而非未注册）；带上下文时走 DB（无 DB 返回错误而非未注册）
    set_tool_context(session_id="s", user_id="", tenant_id="")
    r = await registry.execute("kb_search", {"kb_id": "x", "query": "q"})
    assert "error" in r  # authentication context missing

    set_tool_context(session_id="s", user_id="u1", tenant_id="u1")
    r2 = await registry.execute("kb_search", {"kb_id": "x", "query": "q"})
    # 有上下文：应尝试查询 DB（无 DB 时返回 database 错误），证明工具链已接通
    assert "error" in r2 and "database" in r2["error"].lower()


@pytest.mark.asyncio
async def test_kb_search_answers_via_provider():
    """知识库检索走 provider：kb_search 返回答案 + 引用，且用 KB 属主解析 provider。"""
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.knowledge.providers.base import AnswerResult, Citation
    from app.tools import kb as kb_mod

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={
        "id": "kb-9", "provider": "cloud", "user_id": "u1",
        "visibility": "private", "tenant_id": "t-9",
    })
    answer = AnswerResult(answer="部署方式是…", citations=[Citation(doc_name="a.pdf", page=3)])

    set_tool_context(session_id="s", user_id="u1", tenant_id="t-9")
    with (
        patch("app.tools.kb._pool", AsyncMock(return_value=pool)),
        patch("app.tools.kb.answer_question", AsyncMock(return_value=(answer, "cloud"))),
    ):
        r = await kb_mod.kb_search(kb_id="kb-9", query="q", top_k=5)

    assert r["answer"] == "部署方式是…"
    assert r["provider"] == "cloud"
    assert r["citations"][0]["page"] == 3
