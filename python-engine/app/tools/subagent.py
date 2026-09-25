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

import asyncio
import logging
import os
from typing import Any

from app.subagent import registry as subagent_registry
from app.subagent.budget import from_env as budget_from_env
from app.tools.context import (
    get_all,
    get_gateway,
    get_session_id,
    get_tenant_id,
    get_tool_context,
    get_user_id,
    restore_context,
)
from app.tools.registry import registry

logger = logging.getLogger(__name__)

MAX_TURNS_CAP = 10
MAX_DEPTH = 3  # 全局硬上限（S3）；Profile 的 max_depth 只能更严格

#: 同步委派的默认 wall 上限（秒）。后台委派由注册表看门狗按 idle / max_runtime 收口，
#: 而同步委派是"父 turn 原地等"的路径 —— 没有上限时，上游一挂就无限期占住父回合
#: （这正是"主 Agent 长期阻塞"的主因之一）。
DEFAULT_SYNC_MAX_SECONDS = 300

# 落库器单例（无 DB 时为 None → 子 Agent 照常运行，只是不留痕）
_store = None

# 后台委派的引用与生命周期统一交给 ``app.subagent.registry``：
#   * 持引用（否则 asyncio 任务可能被 GC 静默回收）；
#   * 按 run / 按会话取消（前端"停止"）；
#   * 看门狗（空闲/超时）与跨实例取消广播。
# 反注册见 _drive_background 的 finally。


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


def _get_store() -> Any:
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


def _sync_wall_seconds(run_in_background: bool, max_seconds: int) -> int:
    """同步委派的默认 wall 上限（秒）。

    取值顺序：显式 ``max_seconds`` > ``SUBAGENT_SYNC_MAX_SECONDS`` > :data:`DEFAULT_SYNC_MAX_SECONDS`。

    后台委派不需要这一层（它的 run 登记在注册表里，由看门狗按 idle / max_runtime 收口），
    所以直接返回调用方给的值（通常是 0 = 不限）。同步委派会原地阻塞父 turn，必须有个上限：
    没有它时，上游"建连成功但不返回"就会把父回合一直占住，直到 Go 侧的回合超时兜底。
    """
    if run_in_background or max_seconds:
        return max_seconds
    raw = (os.getenv("SUBAGENT_SYNC_MAX_SECONDS") or "").strip()
    try:
        return max(0, int(raw)) if raw else DEFAULT_SYNC_MAX_SECONDS
    except ValueError:
        return DEFAULT_SYNC_MAX_SECONDS


async def subagent(
    task: str,
    mode: str = "normal",
    max_turns: int = 5,
    expert: str = "",
    profile: str = "",
    run_in_background: bool = True,
    allow_write: bool = False,
    max_tokens: int = 0,
    max_seconds: int = 0,
    rerun_of: str = "",
) -> dict[str, Any]:
    """Delegate *task* to a child agent running in its own session.

    The child runs a full agent loop (own message history, mode config, tool set
    narrowed by the Profile) and returns a structured payload:

    ``{status, output, result_ref, usage{tokens,steps}, summary, truncated}``

    ``output`` 已按 L2 契约限长并包上 ``<subagent-result>`` 不可信标记；完整过程用
    ``result_ref``（= run_id）配合 ``read_subagent_result`` 取用。

    profile: ``agents`` 表中 ``kind='subagent'`` 的 Profile id 或 name。省略时用
    通用子 Agent（与旧版行为一致）。max_turns 上限受 MAX_TURNS_CAP 约束。

    allow_write: 子 Agent 的**默认工具面是只读**（只读工具 + 不剥离委派），因为它与父
    共享同一工作区：写/执行既可能互相踩，又会在 ``tools_mode=auto`` 下**每步都要用户确认**
    （一次委派点十几次批准、每步都可能空等到超时）。需要它改文件/跑命令时**显式**传 true。
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
    # P7：显式把父 run 的事件旁路交给子 Agent，而不是依赖它在运行时从 tool context 回落 ——
    # 回落取不到时 sink 为 None，所有进度事件会被静默丢弃（前端空白且日志无任何记录）。
    from app.agent.event_sink import get_event_sink

    runner = SubAgentRunner(
        gw,
        store=_get_store(),
        pool=_get_pool(),
        depth=depth,
        parent_session_id=get_session_id(),
        turn_id=str(get_tool_context("turn_id", "") or ""),
        tenant_id=get_tenant_id(),
        user_id=get_user_id(),
        sink=get_event_sink(),
        # 后台委派（决定生命周期）；**只读与否由 allow_write 决定**（见下）
        background=bool(run_in_background),
        # 工具面：默认只读 —— 子 Agent 与父共享工作区，写/执行既可能互相踩，
        # 又会在 auto 模式下每一步都要用户确认。需要写/执行必须显式 allow_write=true。
        allow_write=bool(allow_write),
        # per-run 预算：显式参数 > 环境变量 > 默认（见 app/subagent/budget.py）。
        # 同步委派额外带一个 wall 默认值（见 _sync_wall_seconds）——它是"父 turn 原地等"
        # 的路径，没有上限时上游一挂就无限期占住父回合；后台委派由看门狗收口，不需要。
        budget=budget_from_env(
            max_tokens=max_tokens,
            max_seconds=_sync_wall_seconds(run_in_background, max_seconds),
        ),
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
                followup_ctx={
                    "session_id": get_session_id(),
                    "tenant_id": get_tenant_id(),
                    "user_id": get_user_id(),
                    "depth": depth,
                },
                profile_ref=profile,
                mode=mode or "normal",
                max_turns=max(1, min(int(max_turns or 5), MAX_TURNS_CAP)),
                expert_prompt=_expert_system_prompt(expert),
                # 重跑血缘（仅 rerun_subagent 会传）：写进 subagent_runs.rerun_of，
                # 让"这次是从哪一次重跑来的"在 DB 里可查（Redis 会丢，血缘不能丢）。
                rerun_of=rerun_of,
            ),
            name=f"subagent-bg-{bg_run_id}",
        )
        # 登记到注册表：既持引用（防 GC），也让"停止"与看门狗能拿到这个 task
        subagent_registry.register(
            bg_run_id, bg_task,
            session_id=get_session_id(),
            tenant_id=get_tenant_id(),
            profile=profile or "",
        )
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

    # ── 同步委派：**同样要进注册表** ──
    #
    # 直接 `await runner.run(...)` 时，这个协程属于**父 turn 的调用栈**：
    #   * 注册表里没有它 → 看门狗（空闲 / 总时长）与前端「停止」都管不到它；
    #   * 上游挂起或子 Agent 跑飞时，父 turn 只能一路等到 Go 侧的回合超时兜底，
    #     这正是"主 Agent 长期处于阻塞状态"的主因。
    # 所以这里包成**独立 task** 再 await：父 turn 照旧等结果（语义不变），但这个 task
    # 可以被单独取消 —— 看门狗或用户点「停止」都能让它以取消收尾。
    from app.agent.subagent_runner import new_run_id

    sync_run_id = new_run_id()
    work = asyncio.create_task(
        runner.run(
            task,
            run_id=sync_run_id,
            profile_ref=profile,
            mode=mode or "normal",
            max_turns=max(1, min(int(max_turns or 5), MAX_TURNS_CAP)),
            expert_prompt=_expert_system_prompt(expert),
        ),
        name=f"subagent-sync-{sync_run_id}",
    )
    subagent_registry.register(
        sync_run_id, work,
        session_id=get_session_id(),
        tenant_id=get_tenant_id(),
        profile=profile or "",
    )
    try:
        result = await work
    except asyncio.CancelledError:
        # 两种取消必须分开处理（SubAgentRunner 收尾后会把 CancelledError 重新抛出）：
        #   * **父 turn 自己也在被取消**（用户停回合 / 网关超时）→ 照常向上传播，
        #     同时确保独立 task 停下 —— 否则父已死、子还在跑，又造一个孤儿；
        #   * **只有子 Agent 被取消**（看门狗收口 / 用户点了「停止」）→ 父 turn 必须活下去，
        #     给它一份可解释的产物，而不是把整条回合一起取消掉。
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            if not work.done():
                work.cancel()
            raise
        return {
            "status": "cancelled",
            "output": "",
            "result_ref": sync_run_id,
            "error": "subagent cancelled (idle timeout / max runtime / stopped by user)",
            "note": (
                "子 Agent 已被中止，本轮没有拿到它的结论。需要的话请重新委派；"
                f"已完成的部分可用 read_subagent_result(run_id='{sync_run_id}') 取。"
            ),
        }
    finally:
        subagent_registry.unregister(sync_run_id)
        restore_context(parent_ctx)  # 子 agent 已改写 context，父任务必须还原

    payload = result.to_tool_payload()
    if result.status == "failed" and not result.output:
        # 失败且无任何输出：保留错误信息，便于父模型决策
        payload["error"] = result.error or "subagent failed"
    return payload


async def _drive_background(runner: Any, run_id: str, task: str,
                            followup_ctx: dict[str, Any] | None = None,
                            **kwargs: Any) -> None:
    """后台驱动一次子 Agent 运行。

    异常一律吞掉并记日志：后台任务没有调用者在等它的异常，
    逃逸出去只会变成 "Task exception was never retrieved" 噪声。
    状态与产物由 runner 落库（subagent_runs / subagent_run_steps）。

    正常收尾时会投递一次「唤起父会话新一轮」的信号（``app.subagent.followup``）——
    这是子 Agent 结论回到主对话的**唯一**通道：父 turn 通常在派发后就结束了，
    不会有任何一轮主动调用 read_subagent_result。取消/异常路径不触发
    （父 turn 被中断时不该自动再开一轮）。
    """
    try:
        result = await runner.run(task, run_id=run_id, **kwargs)
        logger.info(
            "subagent 后台运行结束 run_id=%s status=%s",
            run_id,
            getattr(result, "status", "?"),
        )
        if followup_ctx and followup_ctx.get("session_id"):
            from app.subagent.followup import fire_followup

            fire_followup(
                run_id=run_id,
                session_id=followup_ctx.get("session_id", ""),
                tenant_id=followup_ctx.get("tenant_id", ""),
                user_id=followup_ctx.get("user_id", ""),
                status=getattr(result, "status", "") or "",
                summary=getattr(result, "summary", "") or "",
                profile=getattr(result, "profile", "") or "",
                depth=int(followup_ctx.get("depth", 0) or 0),
            )
    except asyncio.CancelledError:
        logger.info("subagent 后台运行被取消 run_id=%s", run_id)
        raise
    except Exception as exc:  # noqa: BLE001 — 后台任务不能把异常漏给事件循环
        logger.error("subagent 后台运行失败 run_id=%s: %s", run_id, exc, exc_info=True)
    finally:
        subagent_registry.unregister(run_id)


def _get_pool() -> Any:
    """取 DB 连接池；不可用时返回 None。

    与 :func:`_get_store` 同源语义：**落库能力缺失不该阻断委派**。此前这里直接
    ``get_pool()``，池未初始化时抛 RuntimeError 一路冒到调用方 —— 于是"数据库暂时不可用"
    变成"子 Agent 完全不能用"，而不是"照常运行、只是不留痕"。
    """
    try:
        from app.db import get_pool

        return get_pool()
    except Exception as exc:  # noqa: BLE001 - None 的语义就是"不落库"
        logger.warning("subagent 连接池不可用（本次子 Agent 不落库）: %s", str(exc)[:200])
        return None


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
                "default": True,
                "description": (
                    "Default **true**: the child is launched in the background and this call "
                    "returns immediately (status='async_launched' plus a run_id) so the turn keeps "
                    "working; collect the result later with read_subagent_result(run_id). "
                    "Set false only when you need the child's output **within this turn** to "
                    "continue — a synchronous child blocks this turn for as long as it runs "
                    "(it is cancellable and wall-clock capped, but the turn still waits). "
                    "Background runs still consume their own tokens."
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
            "allow_write": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Default **false**: the child gets a **read-only** tool set (no write_file / "
                    "edit_file / shell_exec / execute_python …), because it shares your workspace — "
                    "and every write/exec call would additionally require a separate user approval "
                    "(tools_mode=auto), which stalls the run on each step. "
                    "Set true only when the children must actually modify files or run commands; "
                    "prefer a Profile that declares read_only=false for that."
                ),
            },
            "max_tokens": {
                "type": "integer",
                "default": 0,
                "description": (
                    "Optional per-run token ceiling (input+output). 0 = use the deployment "
                    "default (SUBAGENT_MAX_TOKENS). Exceeding it ends the run with "
                    "status=failed and error='budget_exceeded:tokens' instead of burning "
                    "unbounded tokens — use it for exploratory fan-out."
                ),
            },
            "max_seconds": {
                "type": "integer",
                "default": 0,
                "description": (
                    "Optional per-run wall-clock ceiling in seconds. 0 = use the default: "
                    "synchronous runs get SUBAGENT_SYNC_MAX_SECONDS (300s) so a stuck upstream "
                    "cannot hold this turn forever; background runs stay unlimited and are "
                    "reaped by the engine watchdog (SUBAGENT_MAX_RUNTIME)."
                ),
            },
        },
        "required": ["task"],
    },
    handler=subagent,
)
