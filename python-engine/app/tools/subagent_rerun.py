"""rerun_subagent 工具 —— 重跑一个**已结束**的子 Agent run（中断/失败后的补救）。

为什么需要它：后台子 Agent 的生命周期与父回合解耦之后（见
``docs/turn-lifecycle-and-approval-audit.md`` §六 第一步），"父回合结束"不再等于
"子任务作废"，但反过来也会出现新的常态：

* 实例重启/被驱逐 → run 被判 ``lost``（不会有人再收尾它）；
* 用户显式停止 → ``cancelled``；
* 上游 provider 报错 → ``failed``。

这些 run 的**任务描述与 Profile 都还在 DB 里**（``subagent_runs.task`` /
``profile_name`` / ``read_only``），重跑只是"按原样再派一次"——不该让模型重新拼一遍
任务文本（那几乎必然与原来不一致，也就不是"重跑"了）。

设计约束：

* 只允许**终态** run（running 的不需要、也不该被重跑 —— 会得到两份并发作业）；
* 重新派发走**同一个** ``subagent`` 工具路径（同一套工具面/预算/栅栏/落库），
  并用 ``rerun_of`` 记录血缘（新列，见迁移 ``f3a91c2d5e08``）；
* 原 run 的写权限**照旧**：``read_only=true`` 的 run 重跑后仍是只读
  （收紧默认工具面之后，这个映射尤其重要 —— 不能让重跑变成提权通道）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.tools.context import get_session_id, get_tenant_id, get_user_id
from app.tools.registry import registry

logger = logging.getLogger(__name__)

#: 允许重跑的状态：都是终态。running 不在列 —— 它不需要重跑，重跑会得到两份并发作业。
RERUNNABLE = ("completed", "failed", "cancelled", "lost")

RUN_SQL = """
SELECT id, task, profile_name, read_only, status, depth
  FROM subagent_runs
 WHERE id = $1
   AND tenant_id = $2
   AND (user_id = $3 OR user_id IS NULL)
   -- 按对话会话隔离：与 read_subagent_result 同一套判定，绝不允许跨会话重跑
   AND root_session_id = $4
"""


async def rerun_subagent(run_id: str, task_override: str = "") -> dict[str, Any]:
    """按原任务与 Profile 重新派发一个已结束的 run。

    Args:
        run_id: 要重跑的 run（来自 ``subagent`` 的返回值或 ``list_subagent_runs``）。
        task_override: 可选——用新的任务描述重跑（其余保持原样）。
    """
    run_id = (run_id or "").strip()
    if not run_id:
        return {"error": "run_id is required"}

    try:
        from app.db import get_pool

        pool = get_pool()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"database unavailable: {str(exc)[:120]}"}
    if pool is None:
        return {"error": "database unavailable"}

    root_session_id = get_session_id()
    if not root_session_id:
        return {"error": "no active session context"}
    try:
        row = await pool.fetchrow(
            RUN_SQL, run_id, get_tenant_id(), get_user_id(), root_session_id
        )
    except Exception as exc:  # noqa: BLE001 - 表缺失 / 老库没有 rerun_of 列等
        logger.warning("rerun_subagent 查询失败: %s", str(exc)[:200])
        return {"error": "subagent run store unavailable (check migrations)"}
    if not row:
        return {"error": f"subagent run not found: {run_id}"}

    status = str(row["status"] or "")
    if status not in RERUNNABLE:
        return {
            "error": f"run {run_id} is {status or 'unknown'}; only finished runs can be rerun "
                     f"({', '.join(RERUNNABLE)}) — a running one already has an owner",
            "status": status,
        }

    task = (task_override or "").strip() or (row["task"] or "").strip()
    if not task:
        return {"error": f"run {run_id} has no stored task to rerun"}

    # 局部导入：工具模块之间不互相 import，避免注册期的循环依赖
    from app.tools.subagent import subagent as delegate

    try:
        result = await delegate(
            task=task,
            profile=str(row["profile_name"] or ""),
            run_in_background=True,
            # 写权限照旧：read_only 的 run 重跑后仍只读（重跑不是提权通道）
            allow_write=not bool(row["read_only"]),
            rerun_of=run_id,
        )
    except Exception as exc:  # noqa: BLE001 - 派发失败要如实返回
        logger.warning("rerun_subagent 派发失败: %s", str(exc)[:200])
        return {"error": f"rerun failed: {str(exc)[:160]}"}

    if isinstance(result, dict) and result.get("error"):
        return result
    if isinstance(result, dict):
        result["rerun_of"] = run_id
        result.pop("note", None)
    logger.info("subagent rerun launched: from=%s to=%s", run_id,
                result.get("run_id") if isinstance(result, dict) else "?")
    return result if isinstance(result, dict) else {"error": "unexpected dispatch result"}


registry.register(
    name="rerun_subagent",
    description=(
        "Re-run a **finished** subagent run by its run_id, reusing the task and profile it was "
        "originally dispatched with (optionally with a new task_override). Use it after a run "
        "ended as failed / cancelled / lost — e.g. the engine instance restarted, the user "
        "stopped it, or the upstream provider errored. Running runs cannot be rerun (they "
        "already have an owner); the new run records rerun_of=<run_id> for lineage."
    ),
    parameters={
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "The finished run to rerun (from subagent or list_subagent_runs)",
            },
            "task_override": {
                "type": "string",
                "default": "",
                "description": "Optional replacement task; empty = reuse the stored task verbatim",
            },
        },
        "required": ["run_id"],
    },
    handler=rerun_subagent,
)
