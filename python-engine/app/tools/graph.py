"""Graph / Workflow tools 注册到本地工具注册表。

实现对标 Go `internal/graph/executor.go` 注册的四个工具：
- graph_create：创建并编译 DAG（不执行）
- graph_run：创建并执行 DAG（LangGraph 引擎）
- graph_templates：列出/查询内置模板
- workflow_run：自然语言快捷工作流（简化三节点线性图）
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from app.gateway.router import GatewayRouter
from app.tools.registry import registry
from app.workflow.engine import WorkflowInstance, run_workflow

_gateway: GatewayRouter | None = None


def bind_gateway(gw: GatewayRouter) -> None:
    global _gateway
    _gateway = gw


# ── 内置模板（与 Go 侧一致）───────────────────────────────────
_TEMPLATES: list[dict[str, Any]] = [
    {
        "name": "Web Research Automation",
        "description": "搜索网页并生成摘要",
        "nodes": [
            {"id": "input_1", "label": "Query", "node_type": "input"},
            {
                "id": "web_search",
                "label": "Search Web",
                "node_type": "tool",
                "config": {
                    "tool_name": "web_fetch",
                    "params": {"url": "https://example.com"},
                },
            },
            {
                "id": "llm_summarize",
                "label": "Summarize",
                "node_type": "llm",
                "config": {"prompt": "Summarize the following content:"},
            },
            {"id": "output_1", "label": "Result", "node_type": "output"},
        ],
        "edges": [
            {"source_id": "input_1", "target_id": "web_search"},
            {"source_id": "web_search", "target_id": "llm_summarize"},
            {"source_id": "llm_summarize", "target_id": "output_1"},
        ],
    },
    {
        "name": "Content Generation Pipeline",
        "description": "根据输入生成内容",
        "nodes": [
            {"id": "input_1", "label": "Prompt", "node_type": "input"},
            {
                "id": "llm_generate",
                "label": "Generate",
                "node_type": "llm",
                "config": {"prompt": "Generate content based on input:"},
            },
            {"id": "output_1", "label": "Result", "node_type": "output"},
        ],
        "edges": [
            {"source_id": "input_1", "target_id": "llm_generate"},
            {"source_id": "llm_generate", "target_id": "output_1"},
        ],
    },
    {
        "name": "Multi-step Decision Flow",
        "description": "根据条件分支执行不同路径",
        "nodes": [
            {"id": "input_1", "label": "Input", "node_type": "input"},
            {
                "id": "llm_analyze",
                "label": "Analyze",
                "node_type": "llm",
                "config": {"prompt": "Analyze input and decide:"},
            },
            {
                "id": "condition_1",
                "label": "Decision",
                "node_type": "condition",
                "config": {"expression": "success", "input": "$llm_analyze"},
            },
            {
                "id": "llm_success",
                "label": "Success Path",
                "node_type": "llm",
                "config": {"prompt": "Handle success case:"},
            },
            {
                "id": "llm_failure",
                "label": "Failure Path",
                "node_type": "llm",
                "config": {"prompt": "Handle failure case:"},
            },
            {"id": "output_1", "label": "Result", "node_type": "output"},
        ],
        "edges": [
            {"source_id": "input_1", "target_id": "llm_analyze"},
            {"source_id": "llm_analyze", "target_id": "condition_1"},
            {"source_id": "condition_1", "target_id": "llm_success"},
            {"source_id": "condition_1", "target_id": "llm_failure"},
            {"source_id": "llm_success", "target_id": "output_1"},
            {"source_id": "llm_failure", "target_id": "output_1"},
        ],
    },
]


# ── 图编译（轻量校验）───────────────────────────────────────
def _compile_graph(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]], entry_point: str = ""
) -> dict[str, Any]:
    if not nodes:
        raise ValueError("graph has no nodes")

    node_map = {n["id"]: n for n in nodes}

    # 边端点校验
    for e in edges:
        if e["source_id"] not in node_map:
            raise ValueError(f"edge source '{e['source_id']}' not found")
        if e["target_id"] not in node_map:
            raise ValueError(f"edge target '{e['target_id']}' not found")

    if not entry_point:
        entry_point = nodes[0]["id"]
    if entry_point not in node_map:
        raise ValueError(f"entry point '{entry_point}' not found")

    # 拓扑排序（Kahn）
    in_deg: dict[str, int] = {n["id"]: 0 for n in nodes}
    adj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        adj[e["source_id"]].append(e["target_id"])
        in_deg[e["target_id"]] += 1

    queue = deque([nid for nid, d in in_deg.items() if d == 0])
    topo: list[str] = []
    while queue:
        nid = queue.popleft()
        topo.append(nid)
        for nei in adj[nid]:
            in_deg[nei] -= 1
            if in_deg[nei] == 0:
                queue.append(nei)

    if len(topo) != len(nodes):
        raise ValueError("graph contains a cycle")

    return {
        "entry_point": entry_point,
        "topological_order": topo,
        "node_count": len(nodes),
        "edge_count": len(edges),
    }


# ── 工具实现 ─────────────────────────────────────────────────
async def graph_create(
    name: str, nodes: list[dict[str, Any]], edges: list[dict[str, Any]], entry_point: str = ""
) -> dict[str, Any]:
    if not name:
        return {"error": "name is required"}
    try:
        result = _compile_graph(nodes, edges, entry_point)
    except Exception as e:
        return {"error": str(e)}
    topo_str = " → ".join(result["topological_order"])
    return {
        "output": f"Graph '{name}' created ({result['node_count']} nodes, {result['edge_count']} edges)\nTopo: {topo_str}",
        "node_count": result["node_count"],
        "edge_count": result["edge_count"],
        "topological_order": result["topological_order"],
    }


async def graph_run(
    name: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    initial_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not name:
        return {"error": "name is required"}
    if _gateway is None:
        return {"error": "gateway not bound"}
    try:
        _compile_graph(nodes, edges)
    except Exception as e:
        return {"error": str(e)}

    graph_json = {
        "name": name,
        "nodes": nodes,
        "edges": edges,
        "entry_point": nodes[0]["id"],
    }
    instance: WorkflowInstance = await run_workflow(graph_json, _gateway, initial_state)

    state = {k: v for k, v in instance.state.items() if not k.startswith("__")}
    results = {
        k: {
            "node_id": v.node_id,
            "status": v.status,
            "output": v.output,
            "error": v.error,
            "duration_ms": v.duration_ms,
        }
        for k, v in instance.results.items()
    }
    events = []
    for node in nodes:
        nid = node["id"]
        events.append({"type": "node_started", "node_id": nid})
        if nid in results:
            if results[nid]["status"] == "completed":
                events.append(
                    {
                        "type": "node_completed",
                        "node_id": nid,
                        "output": results[nid]["output"],
                    }
                )
            else:
                events.append(
                    {
                        "type": "node_error",
                        "node_id": nid,
                        "error": results[nid]["error"],
                    }
                )
    events.append({"type": "done", "progress": 1.0})

    return {
        "output": f"Graph '{name}' executed ({len(nodes)} nodes)\nStatus: {instance.status}",
        "state": state,
        "events": events,
        "results": results,
    }


async def graph_templates(name: str = "") -> dict[str, Any]:
    if not name:
        lines = [f"  • {t['name']}\n    {t['description']}" for t in _TEMPLATES]
        return {
            "output": "Available workflow templates:\n\n" + "\n\n".join(lines),
            "templates": _TEMPLATES,
            "count": len(_TEMPLATES),
        }

    for t in _TEMPLATES:
        if t["name"].lower() == name.lower():
            nodes_str = "\n".join(
                [
                    f"  - {n['id']} ({n['node_type']}): {n.get('label', '')}"
                    for n in t["nodes"]
                ]
            )
            edges_str = "\n".join(
                [f"  - {e['source_id']} → {e['target_id']}" for e in t["edges"]]
            )
            return {
                "output": f"📋 Template: {t['name']}\nDescription: {t['description']}\nNodes:\n{nodes_str}\nEdges:\n{edges_str}",
                "template": t,
                "node_count": len(t["nodes"]),
                "edge_count": len(t["edges"]),
            }
    names = ", ".join(t["name"] for t in _TEMPLATES)
    return {"error": f"template '{name}' not found (available: {names})"}


async def workflow_run(
    task: str, input_data: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not task:
        return {"error": "task is required"}
    if _gateway is None:
        return {"error": "gateway not bound"}

    graph_json = {
        "name": f"workflow_{task[:24]}",
        "nodes": [
            {"id": "input_1", "label": "Input", "node_type": "input"},
            {
                "id": "llm_1",
                "label": "Process Task",
                "node_type": "llm",
                "config": {"prompt": task},
            },
            {"id": "output_1", "label": "Output Result", "node_type": "output"},
        ],
        "edges": [
            {"source_id": "input_1", "target_id": "llm_1"},
            {"source_id": "llm_1", "target_id": "output_1"},
        ],
        "entry_point": "input_1",
    }

    init = dict(input_data or {})
    init.setdefault("task", task)

    instance = await run_workflow(graph_json, _gateway, init)
    state = {k: v for k, v in instance.state.items() if not k.startswith("__")}
    return {"result": state, "events": len(instance.results)}


# ── 注册 ─────────────────────────────────────────────────────
registry.register(
    name="graph_create",
    description="Create a workflow graph (compile only, no execution).",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "nodes": {"type": "array", "items": {"type": "object"}},
            "edges": {"type": "array", "items": {"type": "object"}},
            "entry_point": {"type": "string", "default": ""},
        },
        "required": ["name", "nodes", "edges"],
    },
    handler=graph_create,
)

registry.register(
    name="graph_run",
    description="Create and execute a workflow graph (LangGraph-based).",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "nodes": {"type": "array", "items": {"type": "object"}},
            "edges": {"type": "array", "items": {"type": "object"}},
            "initial_state": {"type": "object", "default": {}},
        },
        "required": ["name", "nodes", "edges"],
    },
    handler=graph_run,
)

registry.register(
    name="graph_templates",
    description="List or query built-in workflow templates.",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "default": ""},
        },
    },
    handler=graph_templates,
)

registry.register(
    name="workflow_run",
    description="Run a natural language task as a simplified workflow.",
    parameters={
        "type": "object",
        "properties": {
            "task": {"type": "string"},
            "input_data": {"type": "object", "default": {}},
        },
        "required": ["task"],
    },
    handler=workflow_run,
)
