"""subagent 工具 — 真子 Agent 委派（Profile 化 + L0/L1/L2 分层）。

父 agent 调用 ``subagent(task, profile="reviewer")``，在**独立 session** 上运行一个完整的
AgentRuntime 循环（独立消息历史、工具集、轮次与深度预算）。

分层（docs/subagent-design.md §3.4）：
* **L0 完整过程** → ``subagent_run_steps``（脱敏后落库，供审计/回放，**永不进父上下文**）
* **L1 有效消息** → ``subagent_runs.summary``（LLM 整理；父会话后续 turn 只注入这一条）
* **L2 当轮回传** → 限长 + ``<subagent-result>`` 不可信包装（仅当轮）

Profile 缺失或解析失败时退回通用子 Agent；落库失败不影响子 Agent 执行。
向后兼容：``mode`` / ``expert`` / ``max_turns`` 参数与旧版语义一致。
"""

from __future__ import annotations

import logging
import asyncio
from typing import Any

from app.tools.context import (get_all, get_gateway, get_session_id,
                              get_tenant_id, get_tool_context, get_user_id,
                              restore_context)
from app.tools.registry import registry

logger = logging.getLogger(__name__)

MAX_TURNS_CAP = 10
MAX_DEPTH = 3  # 全局硬上限（S3）；Profile 的 max_depth 只能更严格

# 落库器单例（无 DB 时为 None → 子 Agent 照常运行，只是不留痕）
_store = None

#: 后台委派的任务表：**必须持有引用**，否则 asyncio 任务可能被 GC 回收而静默消失。
#: 任务结束时自行移除（见 _drive_background 的 finally）。
_BG_TASKS: dict[str, asyncio.Task] = {}


def _expert_system_prompt(expert: str) -> str:
    """按名字从本次对话的专家清单里取人格；无匹配返回空串（退回通用 child）。

    只认清单里的名字，不接受自由文本 —— 否则模型可以用任意字符串把子代理塑造成
    它想要的人格，绕开用户在会话里选定的专家范围。
    """
    name = (expert or "").strip()
    if not name:
        return ""

    for item in get_tool_context("experts", []) or []:
        if isinstance(item, dict) and str(item.get("name", "")).strip() == name:
            return str(item.get("system_prompt", "") or "")
    return ""


def _get_store():
    """懒初始化子 Agent 落库器（失败则降级为不落库）。"""
    global _store
    if _store is not None:
        return _store
    try:
        from app.db import get_pool
        from app.subagent.store import SubagentRunStore

        pool = get_pool()
        if pool is None:
            return None
        _store = SubagentRunStore(pool)
    except Exception as exc:  # noqa: BLE001 - 落库不可用不应阻断委派
        logger.warning("subagent 落库器初始化失败（本次不落库）: %s", str(exc)[:200])
        return None
    return _store


async def subagent(
    task: str,
    mode: str = "normal",
    max_turns: int = 5,
    expert: str = "",
    profile: str = "",
    run_in_background: bool = False,
) -> dict[str, Any]:
    """Delegate *task* to a child agent running in its own session.

    The child runs a full agent loop (own message history, mode config, tool set
    narrowed by the Profile) and returns a structured payload:

    ``{status, output, result_ref, usage{tokens,steps}, summary, truncated}``

    ``output`` 已按 L2 契约限长并包上 ``<subagent-result>`` 不可信标记；完整过程用
    ``result_ref``（= run_id）配合 ``read_subagent_result`` 取用。

    profile: ``agents`` 表中 ``kind='subagent'`` 的 Profile id 或 name。省略时用
    通用子 Agent（与旧版行为一致）。max_turns 上限受 MAX_TURNS_CAP 约束。
    """
    if not task.strip():
        return {"error": "task is required"}
    gw = get_gateway()
    if gw is None:
        return {"error": "subagent requires an active agent runtime"}

    depth = int(get_tool_context("subagent_depth", 0) or 0)
    if depth >= MAX_DEPTH:
        return {"error": f"delegation depth exceeded (max {MAX_DEPTH})"}

    from app.agent.subagent_runner import SubAgentRunner

    parent_ctx = get_all()
    runner = SubAgentRunner(
        gw,
        store=_get_store(),
        pool=_get_pool(),
        depth=depth,
        parent_session_id=get_session_id(),
        turn_id=str(get_tool_context("turn_id", "") or ""),
        tenant_id=get_tenant_id(),
        user_id=get_user_id(),
    )
    # ── 后台委派：**不阻塞父 agent** ──
    #
    # 同步路径要 await 整轮子 Agent 循环（可能数分钟），父 turn 只能干等，
    # 于是"委派"在大任务上几乎不可用。后台路径把它变成 launch-and-return：
    #   * 立即返回 run_id，父模型可以继续别的工具调用；
    #   * 之后用 read_subagent_result(run_id) / subagent_runs 收产物与进度；
    #   * **这里不需要 restore_context** —— create_task 本就跑在 context 的副本里，
    #     父任务的 context 不会被改写（只有同步路径才必须 finally 还原）。
    if run_in_background:
        from app.agent.subagent_runner import new_run_id

        bg_run_id = new_run_id()
        bg_task = asyncio.create_task(
            _drive_background(
                runner,
                bg_run_id,
                task,
                profile_ref=profile,
                mode=mode or "normal",
                max_turns=max(1, min(int(max_turns or 5), MAX_TURNS_CAP)),
                expert_prompt=_expert_system_prompt(expert),
            ),
            name=f"subagent-bg-{bg_run_id}",
        )
        _BG_TASKS[bg_run_id] = bg_task  # 必须持引用，否则可能被 GC 静默回收
        logger.info(
            "subagent 后台委派已启动 run_id=%s profile=%s depth=%s",
            bg_run_id,
            profile or "-",
            depth,
        )
        return {
            "status": "async_launched",
            "isAsync": True,
            "run_id": bg_run_id,
            "result_ref": bg_run_id,
            "note": (
                "子 Agent 已在后台独立运行，**本轮父任务不必等它** —— 可以继续做别的事。"
                "用 read_subagent_result(run_id) 取它的产物与进度；它的过程也会以 "
                "subagent.* 事件汇入前端观测面板。注意：后台运行同样消耗 token。"
            ),
        }

    try:
        result = await runner.run(
            task,
            profile_ref=profile,
            mode=mode or "normal",
            max_turns=max(1, min(int(max_turns or 5), MAX_TURNS_CAP)),
            expert_prompt=_expert_system_prompt(expert),
        )
    finally:
        restore_context(parent_ctx)  # 子 agent 已改写 context，父任务必须还原

    payload = result.to_tool_payload()
    if result.status == "failed" and not result.output:
        # 失败且无任何输出：保留错误信息，便于父模型决策
        payload["error"] = result.error or "subagent failed"
    return payload


async def _drive_background(runner, run_id: str, task: str, **kwargs: Any) -> None:
    """后台驱动一次子 Agent 运行。

    异常一律吞掉并记日志：后台任务没有调用者在等它的异常，
    逃逸出去只会变成 "Task exception was never retrieved" 噪声。
    状态与产物由 runner 落库（subagent_runs / subagent_run_steps），
    父模型随后用 read_subagent_result(run_id) 取用。
    """
    try:
        result = await runner.run(task, run_id=run_id, **kwargs)
        logger.info(
            "subagent 后台运行结束 run_id=%s status=%s",
            run_id,
            getattr(result, "status", "?"),
        )
    except asyncio.CancelledError:
        logger.info("subagent 后台运行被取消 run_id=%s", run_id)
        raise
    except Exception as exc:  # noqa: BLE001 — 后台任务不能把异常漏给事件循环
        logger.error("subagent 后台运行失败 run_id=%s: %s", run_id, exc, exc_info=True)
    finally:
        _BG_TASKS.pop(run_id, None)


def _get_pool():
    from app.db import get_pool

    return get_pool()


registry.register(
    name="subagent",
    description=(
        "Delegate a task to a child agent that runs in its own session with "
        "its own message history, tool set and budget. Use it to parallelize "
        "independent work (read & summarize several files, draft a report, "
        "research a topic) or to run a specialized Profile (e.g. a read-only "
        "reviewer). Returns a structured payload whose 'output' is a length-capped, "
        "marked-as-untrusted summary; the full transcript stays out of this "
        "conversation and is reachable via 'result_ref'. "
        "Cost note: every subagent run consumes its own tokens (often several times "
        "the parent turn), so delegate deliberately."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The task for the child agent"},
            "profile": {
                "type": "string",
                "default": "",
                "description": (
                    "Optional: id or name of a subagent Profile (agents.kind='subagent'). "
                    "Profiles fix the system prompt, tool whitelist/blacklist, read-only "
                    "flag, model/effort and depth limit. Omit to use a general child agent."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["normal", "minimal", "ptc", "creative"],
                "default": "normal",
            },
            "max_turns": {
                "type": "integer",
                "default": 5,
                "description": "Child loop turns cap (max 10)",
            },
            "run_in_background": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Set true to launch the child in the background and return immediately "
                    "(status='async_launched' plus a run_id). The parent turn keeps working "
                    "instead of waiting for the whole child loop. Collect the result later "
                    "with read_subagent_result(run_id). Use it for long independent work "
                    "(multi-file research, drafting a report) that would otherwise block "
                    "this turn for minutes. Background runs still consume their own tokens."
                ),
            },
            "expert": {
                "type": "string",
                "default": "",
                "description": (
                    "Optional: name of an expert to delegate to, taken from the "
                    "'可委派的专家' list in your system prompt. Names outside that "
                    "list are ignored and the child falls back to a generalist."
                ),
            },
        },
        "required": ["task"],
    },
    handler=subagent,
)
