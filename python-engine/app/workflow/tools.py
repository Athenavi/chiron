"""工作流工具：将 LangGraph 工作流引擎接入本地工具注册表。"""

from __future__ import annotations

from typing import Any

from app.gateway.router import GatewayRouter
from app.tools.registry import registry
from app.workflow.engine import get_instance, run_workflow

_gateway: GatewayRouter | None = None


def bind_gateway(gateway: GatewayRouter) -> None:
    global _gateway
    _gateway = gateway


async def workflow_run(
    graph_json: dict[str, Any], initial_state: dict[str, Any] | None = None, name: str = ""
) -> dict[str, Any]:
    if _gateway is None:
        return {"error": "gateway not bound"}
    if not graph_json:
        return {"error": "graph_json is required"}
    if name:
        graph_json.setdefault("name", name)
    instance = await run_workflow(graph_json, _gateway, initial_state)
    return {
        "workflow": instance.graph_name,
        "instance_id": instance.instance_id,
        "status": instance.status,
        "output": {k: v.output for k, v in instance.results.items()},
    }


async def workflow_status(instance_id: str) -> dict[str, Any]:
    inst = get_instance(instance_id)
    if not inst:
        return {"error": f"workflow instance '{instance_id}' not found"}
    return {
        "workflow": inst.graph_name,
        "instance_id": inst.instance_id,
        "status": inst.status,
        "results": {
            k: {"status": v.status, "output": v.output} for k, v in inst.results.items()
        },
    }


registry.register(
    name="workflow_run",
    description="Execute a workflow graph (LangGraph-based)",
    parameters={
        "type": "object",
        "properties": {
            "graph_json": {
                "type": "object",
                "description": "StateGraph JSON with nodes/edges",
            },
            "initial_state": {"type": "object", "default": {}},
            "name": {"type": "string", "default": ""},
        },
        "required": ["graph_json"],
    },
    handler=workflow_run,
)

registry.register(
    name="workflow_status",
    description="Check workflow instance status",
    parameters={
        "type": "object",
        "properties": {
            "instance_id": {"type": "string"},
        },
        "required": ["instance_id"],
    },
    handler=workflow_status,
)
