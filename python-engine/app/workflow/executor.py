"""Workflow 队列执行器 —— 幂等执行 + 断点续跑（多引擎实例/崩溃恢复）。

职责：
1. 读 workflow_instances.checkpoint（{state, done_nodes}）恢复执行游标；
2. 调 run_workflow(graph, gateway, resume_state=…, resume_done=…, on_node_done=…)
   每完成一个节点把 checkpoint 写回 DB（updated_at 即最后心跳）；
3. 终态（completed/error + results）写回 workflow_instances。

消费语义：
- 同一 instance 的消息被重投/另一实例接管时，读到 checkpoint 自动跳过已完成节点继续
  （节点为纯函数、可重放，幂等安全）；
- 实例已终态（completed/error）则直接返回（防重复执行副作用）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 终态写回最大耗时内的瞬时错误仅告警（不影响实例主流程判定）
_TERMINAL_STATUSES = ("completed", "error")


async def load_checkpoint(pool, instance_id: str) -> tuple[str | None, dict, set[str]]:
    """返回 (status, resume_state, resume_done)。行不存在时 status=None。"""
    row = await pool.fetchrow(
        "SELECT status, checkpoint FROM workflow_instances WHERE id = $1",
        instance_id,
    )
    if row is None:
        return None, {}, set()
    cp = row["checkpoint"] or {}
    resume_state = cp.get("state") if isinstance(cp, dict) else None
    resume_done = set(cp.get("done_nodes") or []) if isinstance(cp, dict) else set()
    return row["status"], resume_state or {}, resume_done


async def execute_with_checkpoint(
    instance_id: str,
    graph_json: dict,
    initial_state: dict[str, Any],
    user_id: str = "",
    gateway: Any = None,
) -> None:
    """执行（或续跑）一个 workflow instance 并写回终态。worker/fallback 均调用。"""
    from app.db import get_pool
    from app.workflow.engine import run_workflow

    pool = get_pool()
    status, resume_state, resume_done = await load_checkpoint(pool, instance_id)
    if status is None:
        logger.info("workflow instance not found, skip: %s", instance_id)
        return
    if status in _TERMINAL_STATUSES:
        logger.info("workflow already terminal (%s), idempotent skip: %s", status, instance_id)
        return

    # 工具沙箱上下文：队列 worker 的 contextvars 不含请求上下文，必须显式设置
    from app.tools.context import set_tool_context

    set_tool_context(session_id=instance_id, user_id=user_id or "", tenant_id=user_id or "")

    async def persist_checkpoint(state: dict, done: list[str]) -> None:
        try:
            await pool.execute(
                """UPDATE workflow_instances
                   SET checkpoint = $1, updated_at = NOW()
                   WHERE id = $2""",
                json.dumps({"state": state, "done_nodes": done}),
                instance_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "workflow checkpoint persist failed (recovery granularity only): %s", exc
            )

    final_status = "error"
    results_json = "{}"
    error_text = ""
    try:
        instance = await run_workflow(
            graph_json,
            gateway,
            initial_state=initial_state,
            instance_id=instance_id,
            resume_state=resume_state or None,
            resume_done=resume_done or None,
            on_node_done=persist_checkpoint,
        )
        if instance.status == "completed":
            final_status = "completed"
        else:
            error_text = instance.error or "workflow execution failed"
        results_json = json.dumps(
            {
                nid: {"status": nr.status, "output": nr.output}
                for nid, nr in instance.results.items()
            },
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("workflow execution failed: %s", exc)
        error_text = str(exc)

    try:
        await pool.execute(
            """UPDATE workflow_instances
               SET status = $1, results = $2, error = $3, checkpoint = NULL, updated_at = NOW()
               WHERE id = $4""",
            final_status,
            results_json,
            error_text or None,
            instance_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("workflow instance final update failed: %s", exc)

    # Webhook 事件（workflow.complete/error）：统一出口在队列 executor（跨实例一致）
    try:
        from app.event_bus import emit_event

        wf_payload = {
            "instance_id": instance_id,
            "user_id": user_id,
            "status": final_status,
            "graph_name": (graph_json or {}).get("name", ""),
            "results": json.loads(results_json) if results_json and results_json != "{}" else {},
            "error": error_text or "",
        }
        event_type = "workflow.complete" if final_status == "completed" else "workflow.error"
        await emit_event(event_type, wf_payload, tenant_id=user_id or "default")
    except Exception as exc:  # noqa: BLE001
        logger.warning("workflow webhook emit failed: %s", exc)
