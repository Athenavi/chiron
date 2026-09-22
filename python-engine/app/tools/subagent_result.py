"""read_subagent_result 工具 —— 按 run_id 取子 Agent 的 L1 摘要与 L0 完整过程。

为什么需要它：L2 回传给父上下文的是**限长摘要**（防止子 Agent 的长输出淹没父上下文），
细节必须"按需取用"。本工具就是这个按需入口，让"大产物走外部存储 + 轻量引用"的模式闭环。

安全：只允许读**同租户**的 run；内容在入库前已脱敏（``app.subagent.redact``），
因此读取时不重复脱敏，但仍会限制单次返回体积。
"""

from __future__ import annotations

import logging
from typing import Any

from app.tools.context import get_session_id, get_tenant_id, get_user_id
from app.tools.registry import registry

logger = logging.getLogger(__name__)

STEP_READ_MAX_CHARS = 8000  # 单步返回上限（防止一次拉回整份过程）
RUN_SQL = """
SELECT id, status, profile_name, summary, artifacts, input_tokens, output_tokens,
       steps, redacted_count, error, created_at
  FROM subagent_runs
 WHERE id = $1 AND tenant_id = $2 AND (user_id = $3 OR user_id IS NULL)
   -- 按对话会话隔离：主 Agent 不该读到**别的会话**的子 Agent 结果，
   -- 否则它会拿别人的 run 当作自己的上下文（跨会话影响判断）。
   AND root_session_id = $4
"""
STEPS_SQL = """
SELECT seq, kind, role, tool_name, tool_call_id, content, truncated
  FROM subagent_run_steps
 WHERE run_id = $1
 ORDER BY seq
 LIMIT $2
"""


async def read_subagent_result(
    run_id: str, max_steps: int = 50, include_steps: bool = True
) -> dict[str, Any]:
    """读取指定子 Agent run 的摘要与（可选的）逐 step 过程。

    run_id 来自 ``subagent``/``fleet`` 返回的 ``result_ref``。
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

    tenant_id = get_tenant_id()
    user_id = get_user_id()
    root_session_id = get_session_id()
    try:
        row = await pool.fetchrow(RUN_SQL, run_id, tenant_id, user_id, root_session_id)
    except Exception as exc:  # noqa: BLE001 - 表缺失等
        logger.warning("read_subagent_result 查询失败: %s", str(exc)[:200])
        return {"error": "subagent run store unavailable (check migration 5e244b718fd1)"}
    if not row:
        return {"error": f"subagent run not found: {run_id}"}

    payload: dict[str, Any] = {
        "run_id": row["id"],
        "status": row["status"],
        "profile": row["profile_name"] or "",
        "summary": row["summary"] or "",
        "usage": {
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "steps": int(row["steps"] or 0),
        },
        "redacted_count": int(row["redacted_count"] or 0),
    }
    if row["error"]:
        payload["error"] = row["error"]
    if row["created_at"]:
        payload["created_at"] = row["created_at"].isoformat()

    if include_steps:
        limit = max(1, min(int(max_steps or 50), 200))
        try:
            steps = await pool.fetch(STEPS_SQL, run_id, limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("read_subagent_result 读取 steps 失败: %s", str(exc)[:160])
            steps = []
        payload["steps"] = [
            {
                "seq": int(s["seq"]),
                "kind": s["kind"],
                "role": s["role"] or "",
                "tool": s["tool_name"] or "",
                "content": (s["content"] or "")[:STEP_READ_MAX_CHARS],
                "truncated": bool(s["truncated"]) or len(s["content"] or "") > STEP_READ_MAX_CHARS,
            }
            for s in steps
        ]
    return payload


registry.register(
    name="read_subagent_result",
    description=(
        "Read a previous subagent run by its result_ref (run_id): returns the "
        "LLM-curated summary (L1) plus the full step-by-step transcript (L0) with "
        "per-step tool names and content. Use it only when the summarized output "
        "from the subagent tool is not enough — it may be large."
    ),
    parameters={
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "result_ref returned by the subagent tool"},
            "max_steps": {
                "type": "integer",
                "default": 50,
                "description": "Max steps to return (1-200)",
            },
            "include_steps": {
                "type": "boolean",
                "default": True,
                "description": "Set false to fetch only the summary and usage",
            },
        },
        "required": ["run_id"],
    },
    handler=read_subagent_result,
)
