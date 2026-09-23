"""list_subagent_runs 工具 —— 主 Agent 的「主动感知」入口。

为什么需要它：子 Agent 的结论此前只有两条**被动**通道 ——

1. ``followup``：多跳投递（队列 → 网关 → 新一轮），每一跳都能静默丢，且受速率上限、
   级联保护与 deadline 限制；
2. ``read_subagent_result``：**必须知道 run_id**，且一次只取一个。

两者都预设"主 Agent 已经知道有哪个 run 存在"。一旦它在长对话里忘了 run_id
（很常见：派发发生在几十轮以前），就只能等用户去刷新侧边栏 —— 观测于是退化成"推送模型"。

本工具把观测补成**可查询**模型（设计稿 §二 原则 4：回传不依赖投递链路）：
"我派过哪些子任务、它们现在怎么样了" —— 一次查询回答，数据来自 **DB（唯一权威）**。

隔离：与 ``read_subagent_result`` 同一套三重校验（tenant + user + **root_session_id**），
主 Agent 永远读不到别的会话的子 Agent 结果。
"""

from __future__ import annotations

import logging
from typing import Any

from app.tools.context import get_session_id, get_tenant_id, get_user_id
from app.tools.registry import registry

logger = logging.getLogger(__name__)

#: 单条 summary 在列表里的上限：列表是"总览"，细节用 read_subagent_result 取
SUMMARY_MAX_CHARS = 400
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
#: 允许的过滤值（与状态机一致；空串 = 不过滤）
STATUS_VALUES = ("running", "completed", "failed", "cancelled", "lost")

LIST_SQL = """
SELECT id, parent_run_id, status, profile_name, summary, steps, read_only,
       input_tokens, output_tokens, error, created_at, finished_at
  FROM subagent_runs
 WHERE tenant_id = $1
   AND (user_id = $2 OR user_id IS NULL)
   AND root_session_id = $3
   AND ($4 = '' OR status = $4)
 ORDER BY created_at DESC
 LIMIT $5
"""


async def list_subagent_runs(status: str = "", limit: int = 0) -> dict[str, Any]:
    """列出**本会话**派发的子 Agent 运行（最新在前）。

    Args:
        status: 可选过滤：running / completed / failed / cancelled / lost；留空看全部。
        limit: 返回条数上限（默认 20，最大 50）。
    """
    wanted = (status or "").strip().lower()
    if wanted and wanted not in STATUS_VALUES:
        return {"error": f"unknown status filter: {status}（可选：{', '.join(STATUS_VALUES)}）"}

    try:
        from app.db import get_pool

        pool = get_pool()
    except Exception as exc:  # noqa: BLE001 - 池未初始化
        return {"error": f"database unavailable: {str(exc)[:120]}"}
    if pool is None:
        return {"error": "database unavailable"}

    tenant_id = get_tenant_id()
    user_id = get_user_id()
    root_session_id = get_session_id()
    if not root_session_id:
        return {"error": "no active session context"}

    try:
        cap = int(limit) if limit else DEFAULT_LIMIT
    except (TypeError, ValueError):
        cap = DEFAULT_LIMIT
    cap = max(1, min(cap, MAX_LIMIT))

    try:
        rows = await pool.fetch(LIST_SQL, tenant_id, user_id, root_session_id, wanted, cap)
    except Exception as exc:  # noqa: BLE001 - 表缺失等
        logger.warning("list_subagent_runs 查询失败: %s", str(exc)[:200])
        return {"error": "subagent run store unavailable (check migration 5e244b718fd1)"}

    runs: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {
            "run_id": row["id"],
            "status": row["status"],
            "profile": row["profile_name"] or "",
            "summary": (row["summary"] or "")[:SUMMARY_MAX_CHARS],
            "steps": int(row["steps"] or 0),
            "usage": {
                "input_tokens": int(row["input_tokens"] or 0),
                "output_tokens": int(row["output_tokens"] or 0),
            },
            "read_only": bool(row["read_only"]),
        }
        if row["parent_run_id"]:
            item["parent_run_id"] = row["parent_run_id"]
        if row["error"]:
            item["error"] = row["error"]
        if row["created_at"]:
            item["created_at"] = row["created_at"].isoformat()
        if row["finished_at"]:
            item["finished_at"] = row["finished_at"].isoformat()
        runs.append(item)

    return {
        "runs": runs,
        "count": len(runs),
        "hint": "用 read_subagent_result(run_id) 取某个 run 的完整过程；"
                "rerun_subagent(run_id) 可重跑一个已结束的 run。",
    }


registry.register(
    name="list_subagent_runs",
    description=(
        "List the subagent runs **you** delegated in this conversation (newest first), with "
        "status / profile / summary / step count / usage, read straight from the authoritative "
        "store. Use it when you need to know what background work you handed off and how it "
        "ended — especially before assuming a task is still running or that you never delegated "
        "it. Filter with status='completed' to see only finished ones."
    ),
    parameters={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["", "running", "completed", "failed", "cancelled", "lost"],
                "default": "",
                "description": "Optional status filter; empty = all runs.",
            },
            "limit": {
                "type": "integer",
                "default": DEFAULT_LIMIT,
                "description": f"Max runs to return (1-{MAX_LIMIT}).",
            },
        },
        "required": [],
    },
    handler=list_subagent_runs,
)
