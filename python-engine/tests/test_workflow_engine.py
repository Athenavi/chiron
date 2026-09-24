"""app/workflow 核心执行逻辑测试 (engine.py + tracing_engine 编辑会话)。

test_interop.py 已覆盖节点互通冒烟;本文件补齐缺失的核心执行语义:
- 拓扑序: 依赖节点必须先于依赖者执行 (下游才能拿到上游输出)
- 条件/LLM 节点的求值语义
- 工具节点重试
- 失败必须落到实例状态 (fail loud,不得静默吞掉)
- 编辑会话的 TTL 过期与租户隔离
"""
from __future__ import annotations

import pytest

import app.main  # noqa: F401 — 初始化 app 包，避免循环导入
from app.tools.registry import registry
from app.workflow import tracing_engine
from app.workflow.engine import _topological_sort, run_workflow

# ── 测试替身 ────────────────────────────────────────────────────────


class _Chunk:
    def __init__(self, content: str = ""):
        self.content = content


class _FakeGateway:
    def __init__(self, content: str = "llm-ok"):
        self._content = content

    async def chat_stream(self, messages=None, model=""):
        yield _Chunk(self._content)


class _BrokenGateway:
    async def chat_stream(self, messages=None, model=""):
        raise RuntimeError("llm exploded")
        yield  # pragma: no cover — 保证这是生成器函数


# ── 拓扑排序: 依赖必须先执行 ────────────────────────────────────────


def test_topological_sort_dependency_before_dependent():
    """意图: 下游节点执行时上游输出必须已就绪,故依赖必须排在前面。"""
    nodes = [{"id": "b"}, {"id": "a"}, {"id": "c"}]
    edges = [
        {"source_id": "a", "target_id": "b"},
        {"source_id": "b", "target_id": "c"},
    ]
    order = [n["id"] for n in _topological_sort(nodes, edges)]
    assert order.index("a") < order.index("b") < order.index("c")


def test_topological_sort_includes_orphan_nodes():
    """孤立节点 (无边) 也不能被丢掉,否则工作流会静默少执行节点。"""
    nodes = [{"id": "solo"}, {"id": "a"}, {"id": "b"}]
    edges = [{"source_id": "a", "target_id": "b"}]
    order = [n["id"] for n in _topological_sort(nodes, edges)]
    assert set(order) == {"solo", "a", "b"}


# ── 节点执行语义 ────────────────────────────────────────────────────


async def test_condition_node_evaluates_predecessor_output():
    """条件节点必须基于前驱输出做包含判断,输出 true/false 字符串。"""
    graph = {
        "name": "wf-cond",
        "nodes": [
            {"id": "in", "node_type": "input"},
            {"id": "cond", "node_type": "condition", "config": {"condition": "hello"}},
        ],
        "edges": [{"source_id": "in", "target_id": "cond"}],
    }
    hit = await run_workflow(graph, _FakeGateway(), {"input": "hello world"})
    assert hit.status == "completed"
    assert hit.results["cond"].output == "true"

    miss = await run_workflow(graph, _FakeGateway(), {"input": "goodbye"})
    assert miss.results["cond"].output == "false"


async def test_llm_node_returns_gateway_content():
    """LLM 节点输出必须来自 gateway 流式内容 (而非占位文本)。"""
    graph = {
        "name": "wf-llm",
        "nodes": [
            {"id": "llm", "node_type": "llm", "config": {"user_message": "hi", "model": "m"}},
        ],
        "edges": [],
    }
    inst = await run_workflow(graph, _FakeGateway(content="unique-llm-answer"), {})
    assert inst.status == "completed"
    assert inst.results["llm"].output == "unique-llm-answer"


async def test_workflow_failure_sets_error_status():
    """节点抛错必须落到实例状态 (status=error + 错误信息),不得静默成功。"""
    graph = {
        "name": "wf-fail",
        "nodes": [
            {"id": "llm", "node_type": "llm", "config": {"user_message": "hi"}},
        ],
        "edges": [],
    }
    inst = await run_workflow(graph, _BrokenGateway(), {})
    assert inst.status == "error"
    assert "llm exploded" in inst.error
    assert inst.finished_at is not None


# ── 工具节点重试 ────────────────────────────────────────────────────


async def test_tool_node_retries_until_success():
    """配置 retries=2 时,前两次失败后第三次必须成功 (重试真正生效)。"""
    calls = {"n": 0}

    async def _flaky_tool():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError(f"transient failure {calls['n']}")
        return {"ok": True}

    registry.register("wf_flaky_tool", "flaky", {"type": "object"}, _flaky_tool)
    try:
        graph = {
            "name": "wf-retry",
            "nodes": [
                {"id": "tk", "node_type": "tool",
                 "config": {"tool_name": "wf_flaky_tool", "retries": 2}},
            ],
            "edges": [],
        }
        inst = await run_workflow(graph, _FakeGateway(), {})
        assert inst.status == "completed"
        assert calls["n"] == 3, "必须恰好重试到成功为止"
        assert "ok" in inst.results["tk"].output
    finally:
        registry.unregister("wf_flaky_tool")


async def test_tool_node_without_retries_surfaces_error():
    """不配置重试时失败必须体现在输出里 (fail loud,不得吞错)。"""
    async def _always_fail():
        raise RuntimeError("permanent failure")

    registry.register("wf_fail_tool", "fail", {"type": "object"}, _always_fail)
    try:
        graph = {
            "name": "wf-no-retry",
            "nodes": [
                {"id": "tk", "node_type": "tool", "config": {"tool_name": "wf_fail_tool"}},
            ],
            "edges": [],
        }
        inst = await run_workflow(graph, _FakeGateway(), {})
        assert "error" in inst.results["tk"].output
        assert "permanent failure" in inst.results["tk"].output
    finally:
        registry.unregister("wf_fail_tool")


# ── 编辑会话 (tracing_engine, Pause-Mode 前置能力) ──────────────────


def test_edit_session_create_get_and_tenant_isolation():
    """编辑会话必须按 workflow 实例 + 租户精确匹配,跨租户不可见。"""
    engine = tracing_engine.TracingWorkflowEngine(gateway_router=None)
    session_id = tracing_engine.create_edit_session("wf_iso", tenant_id="tenantA")
    try:
        assert tracing_engine.get_edit_session(session_id) is not None
        assert engine._check_edit_commands("wf_iso", "tenantA") is not None
        # 其他租户不得看到/消费该会话 (SaaS 隔离)
        assert engine._check_edit_commands("wf_iso", "tenantB") is None
        assert engine._check_edit_commands("wf_other", "tenantA") is None
    finally:
        tracing_engine._edit_sessions.pop(session_id, None)


def test_edit_session_expires_after_ttl():
    """过期会话必须被视为不存在 (不得返回僵尸会话)。"""
    session_id = tracing_engine.create_edit_session("wf_ttl", tenant_id="tenantA", ttl=-1)
    assert tracing_engine.get_edit_session(session_id) is None
    assert session_id not in tracing_engine._edit_sessions  # 过期即清理


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
