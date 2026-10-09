"""resume_subagent 工具 —— 带着**已记录的步骤**继续跑，或从某一步分叉（R5(b)）。

与 `rerun_subagent` 的差别只在**起始上下文**：

* `rerun_subagent`：按原任务**再派一次**（起始上下文仍是父会话 seed / Profile）；
* `resume_subagent`：沿着**它自己已产生的步骤**继续，并可选在 `at_step` 处**截断**（分叉）。

为什么两者都要：后台子 agent 常在中途结束（实例重启 → `lost`、用户停止 → `cancelled`、
上游报错 → `failed`）。重跑是"从头再来"，而很多时候模型只需要"接着做剩下的事" —— 那时把
已经做完的过程丢掉，等于白烧一遍 token，还会让新 run 重复副作用。

三条边界（设计见 `docs/subagent-resume-design.md`）：

* **只许终态 run**（`RERUNNABLE`，与重跑同一集合）：`running` 的已有主，续跑会得到两份并发作业；
* **原 run 一个字不改**：续跑产生**新 run**，旧 run 保持终态（它是证据）；
* **写权限只能收紧**：`read_only` 的 run 续跑后仍只读（与"重跑不是提权通道"同一条纪律）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.subagent.access import RUN_ACCESS_CLAUSE
from app.subagent.resume import build_resume_context
from app.tools.context import get_session_id, get_tenant_id, get_user_id
from app.tools.registry import registry
from app.tools.subagent_rerun import RERUNNABLE

logger = logging.getLogger(__name__)

#: 读取被续跑的 run（访问谓词与 `read_subagent_result` / `rerun_subagent` **同一份**，
#: 见 `app/subagent/access.py`：跨租户 / 跨会话一律拒）。
RUN_SQL = f"""
SELECT id, task, profile_name, read_only, status, depth
  FROM subagent_runs
 WHERE id = $1{RUN_ACCESS_CLAUSE}
"""

#: 取步骤：`at_step = 0` ⇒ 全部（续到底）；否则只取 `seq < at_step`（分叉）。
STEPS_SQL = """
SELECT seq, kind, role, tool_name, content
  FROM subagent_run_steps
 WHERE run_id = $1
   AND ($2::int = 0 OR seq < $2::int)
 ORDER BY seq
"""


async def resume_subagent(run_id: str, instruction: str, at_step: int = 0) -> dict[str, Any]:
    """从 *run_id* 的已有步骤继续（或从 `at_step` 分叉），并给出新的指令。

    Args:
        run_id: 要续跑的**已结束** run（来自 ``subagent`` / ``list_subagent_runs``）。
        instruction: 接下来要做什么。**必填** —— 续跑不是"再跑一遍同一件事"（那是
            ``rerun_subagent``），它需要有新的目标，否则只是重复副作用。
        at_step: 分叉点。`0`（默认）= **续到底**（带上全部步骤）；`N > 0` = 只带
            ``seq < N`` 的步骤，从第 N 步之前另走一条路。

    Returns:
        与 `subagent` 相同的载荷，外加 `resumed_from` / `resume_at_step` /
        `resumed_steps`（真正带上了多少步）/ `resumed_truncated`。
    """
    if not settings.subagent_resume_enabled:
        return {
            "error": (
                "resume_subagent is disabled on this deployment "
                "(set SUBAGENT_RESUME_ENABLED=true to enable it)"
            )
        }
    run_id = (run_id or "").strip()
    if not run_id:
        return {"error": "run_id is required"}
    goal = (instruction or "").strip()
    if not goal:
        return {
            "error": (
                "instruction is required — resuming needs a new goal; to re-run the same task "
                "use rerun_subagent(run_id)"
            )
        }
    step = int(at_step or 0)
    if step < 0:
        return {"error": "at_step must be >= 0 (0 = resume all recorded steps)"}

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
    except Exception as exc:  # noqa: BLE001 — 表缺失 / 老库没有这两列等
        logger.warning("resume_subagent 查询失败: %s", str(exc)[:200])
        return {"error": "subagent run store unavailable (check migrations)"}
    if not row:
        return {
            "error": (
                f"subagent run not found: {run_id} (or it belongs to another tenant/session)"
            )
        }

    status = str(row["status"] or "")
    if status not in RERUNNABLE:
        return {
            "error": (
                f"run {run_id} is {status or 'unknown'}; only finished runs can be resumed "
                f"({', '.join(RERUNNABLE)}) — a running one already has an owner"
            ),
            "status": status,
        }

    try:
        steps = await pool.fetch(STEPS_SQL, run_id, step)
    except Exception as exc:  # noqa: BLE001 — 步骤表缺失等
        logger.warning("resume_subagent 读取步骤失败: %s", str(exc)[:200])
        return {"error": "subagent step store unavailable (check migrations)"}
    context = build_resume_context(
        [dict(r) for r in (steps or [])], run_id=run_id, at_step=step or None
    )
    if not context:
        # 没有步骤可带：仍然允许（沿同一条任务线继续），但**说清楚** —— 否则调用方会以为
        # 自己续上了某个过程，实际是空手开始（那和重跑没区别，只是不带原任务）。
        logger.info("resume_subagent: run %s has no recorded steps at at_step=%s", run_id, step)

    # 局部导入：工具模块之间不互相 import，避免注册期的循环依赖（与 rerun_subagent 同款）
    from app.tools.subagent import subagent as delegate

    try:
        result = await delegate(
            task=goal,
            profile=str(row["profile_name"] or ""),
            run_in_background=True,
            # 写权限**只能收紧**：原 run 只读 ⇒ 续跑也只读（续跑不是提权通道）
            allow_write=not bool(row["read_only"]),
            resume_context=context.text,
            resumed_from=run_id,
            resume_at_step=step or None,
        )
    except Exception as exc:  # noqa: BLE001 - 派发失败要如实返回
        logger.warning("resume_subagent 派发失败: %s", str(exc)[:200])
        return {"error": f"resume failed: {str(exc)[:160]}"}

    if isinstance(result, dict) and result.get("error"):
        return result
    if isinstance(result, dict):
        result["resumed_from"] = run_id
        result["resume_at_step"] = step or None
        result["resumed_steps"] = context.used_steps
        result["resumed_truncated"] = context.truncated
        result.pop("note", None)
    logger.info(
        "subagent resumed: from=%s steps=%d at_step=%s",
        run_id,
        context.used_steps,
        step or "all",
    )
    return result if isinstance(result, dict) else {"error": "unexpected dispatch result"}


registry.register(
    name="resume_subagent",
    description=(
        "**Continue** a finished subagent run along its own recorded steps, optionally forking "
        "from an earlier step — instead of re-running it from scratch. Use it when a run ended "
        "(failed / cancelled / lost) but its recorded work is still useful and you only need to "
        "carry on with a new goal. To simply re-run the SAME task use rerun_subagent instead. "
        "The original run is never modified: resuming creates a NEW run with "
        "resumed_from=<run_id>, and keeps its read-only setting."
    ),
    parameters={
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "The finished run to continue (from subagent or list_subagent_runs)",
            },
            "instruction": {
                "type": "string",
                "description": (
                    "What to do next — required. Resuming needs a new goal; re-running the same "
                    "task is rerun_subagent's job."
                ),
            },
            "at_step": {
                "type": "integer",
                "default": 0,
                "description": (
                    "Fork point: 0 = carry on from the very end (all recorded steps); N > 0 = keep "
                    "only steps with seq < N and take a different path from there."
                ),
            },
        },
        "required": ["run_id", "instruction"],
    },
    handler=resume_subagent,
)
