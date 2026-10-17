"""六大互通冒烟测试：工作流 gateway 绑定、kb 工具注册、工作流内 skill/MCP 工具。"""
from __future__ import annotations

import pytest

import app.main  # noqa: F401 — 初始化 app 包，避免循环导入
from app.tools.context import set_tool_context
from app.tools.registry import registry
from app.workflow.engine import run_workflow


class _FakeGateway:
    async def chat_stream(self, messages=None, model=""):
        class _C:
            content = "llm-ok"
        yield _C()


# ── 工作流可被对话/Agent 调用（bind_gateway 接通后） ─────────────────────

@pytest.mark.asyncio
async def test_workflow_run_tool_after_bind():
    from app.workflow.tools import bind_gateway, workflow_run

    bind_gateway(_FakeGateway())
    result = await workflow_run({"nodes": [
        {"id": "in", "node_type": "input"},
        {"id": "out", "node_type": "output"},
    ], "edges": [{"source_id": "in", "target_id": "out"}]}, {"input": "hello"})
    # 修复前：返回 {"error": "gateway not bound"}
    assert "error" not in result or "gateway not bound" not in result.get("error", "")
    assert result.get("status") == "completed"


@pytest.mark.asyncio
async def test_graph_run_tool_after_bind():
    from app.tools.graph import bind_gateway, graph_run

    bind_gateway(_FakeGateway())
    result = await graph_run("demo", [
        {"id": "input_1", "label": "I", "node_type": "input"},
        {"id": "output_1", "label": "O", "node_type": "output"},
    ], [{"source_id": "input_1", "target_id": "output_1"}], {"input": "hi"})
    assert "error" not in result or "gateway not bound" not in result.get("error", "")


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
async def test_kb_search_rag_scopes_to_kb_and_tenant():
    """RAG 类型库走新栈：kb_id + 该库真实租户双收窄，不再串读同租户其它库。"""
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.tools import kb as kb_mod

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={
        "id": "kb-9", "type": "rag", "user_id": "u1",
        "visibility": "private", "tenant_id": "t-9",
    })
    builder = MagicMock()
    builder.query = AsyncMock(return_value=[
        {"id": "v", "doc_id": "d1", "chunk_index": 2, "content": "hello", "score": 0.7},
    ])

    set_tool_context(session_id="s", user_id="u1", tenant_id="t-9")
    with (
        patch("app.db.get_pool", return_value=pool),
        patch("app.main.get_gateway", new=AsyncMock(return_value=MagicMock())),
        patch("app.rag.builder.RAGBuilder", return_value=builder),
    ):
        r = await kb_mod.kb_search(kb_id="kb-9", query="q", top_k=5)

    kwargs = builder.query.await_args.kwargs
    assert kwargs["kb_id"] == "kb-9"
    assert kwargs["tenant_id"] == "t-9"
    assert r["results"][0]["document_id"] == "d1"
    assert r["results"][0]["chunk_index"] == 2


# ── 工作流内 skill 节点 ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_workflow_skill_node(tmp_path, monkeypatch):
    from app.skill.store import SkillDef, SkillStore
    from app.tools import skill as skill_mod

    store = SkillStore(str(tmp_path / "skills"))
    store.save(SkillDef(name="wf-echo", description="echo", exec_type="shell", source='python -c "print(\'wf-{who}\')"'))
    skill_mod._store = store  # noqa: SLF001 — 测试注入临时 store

    graph = {
        "name": "wf-skill",
        "nodes": [
            {"id": "in", "node_type": "input"},
            {"id": "sk", "node_type": "skill", "config": {"skill_name": "wf-echo", "params": {"who": "ok"}}},
            {"id": "out", "node_type": "output"},
        ],
        "edges": [
            {"source_id": "in", "target_id": "sk"},
            {"source_id": "sk", "target_id": "out"},
        ],
    }
    inst = await run_workflow(graph, _FakeGateway(), {"input": "x"})
    assert inst.status == "completed"
    assert "wf-ok" in inst.results["sk"].output


# ── 工作流内知识库节点（配置缺失时明确报错而非崩溃） ──────────────────────

@pytest.mark.asyncio
async def test_workflow_knowledge_node_missing_kb():
    graph = {
        "name": "wf-kb",
        "nodes": [
            {"id": "in", "node_type": "input"},
            {"id": "kb", "node_type": "knowledge", "config": {}},
            {"id": "out", "node_type": "output"},
        ],
        "edges": [
            {"source_id": "in", "target_id": "kb"},
            {"source_id": "kb", "target_id": "out"},
        ],
    }
    inst = await run_workflow(graph, _FakeGateway(), {"input": "x"})
    assert inst.status == "completed"
    assert "kb_id is required" in inst.results["kb"].output


# ── 工作流内 MCP 工具（工具上下文生效后 owner 工具可执行） ────────────────

async def _owned_tool(**kw):
    return {"ok": "mcp-result"}


@pytest.mark.asyncio
async def test_workflow_tool_node_with_user_context():
    registry.register("wf_mcp_demo", "demo", {"type": "object"}, _owned_tool, owner="u-mcp")
    try:
        set_tool_context(session_id="s", user_id="u-mcp", tenant_id="u-mcp")
        graph = {
            "name": "wf-mcp",
            "nodes": [
                {"id": "in", "node_type": "input"},
                {"id": "tk", "node_type": "tool", "config": {"tool_name": "wf_mcp_demo"}},
                {"id": "out", "node_type": "output"},
            ],
            "edges": [
                {"source_id": "in", "target_id": "tk"},
                {"source_id": "tk", "target_id": "out"},
            ],
        }
        inst = await run_workflow(graph, _FakeGateway(), {"input": "x"})
        assert inst.status == "completed"
        assert "mcp-result" in inst.results["tk"].output
    finally:
        registry.unregister("wf_mcp_demo")
