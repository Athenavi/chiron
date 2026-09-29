"""C1 批 3+：run 恢复路径与 reconciler（方案 01 §3.1）。

两件事：

1. **入口恢复（用户重试续跑）**：新 run 进入时若发现该会话有可用 checkpoint，
   就以**快照**作为历史起点 —— 已完成回合与工具调用因此不重放（快照里已有它们的结果）。
   窗口判定沿用批 2 的 `resume_window`：热 1h / 冷 24h / 超窗 `abandoned`（**可见**，不静默）。
2. **reconciler**：找出"旧主已死"的僵尸 run 并把状态修正为可续跑/已放弃 + 记指标。

**接管的语义边界（与方案措辞的一处偏离，见评审 03 §17.2）**：方案写"reconciler 自动接管"，
但自动续跑需要**重建 `AgentTask`**（llm_config / workbench_context / tools 等只存在于**提交请求**里，
引擎持久层没有稳定来源）。用半个 task 去续跑比不续跑更危险，因此本实现里：

* reconciler 只做「**验证旧主不在** → 把 `running` 修正为 `checkpointed` / 超窗的标 `abandoned` → 记指标」；
* **实际续跑**由**用户重试路径**完成（入口恢复）—— 那时 task 是完整的。

这仍然满足方案给出的**接管不变量**：运行锁可获取 且 旧主确实不在。
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from app.agent.checkpoint import RunCheckpoint

logger = logging.getLogger(__name__)

#: reconciler 扫描周期（秒）与单轮上限（方案 §3.1：周期 60s + LIMIT 20 + 每实例抖动）
RECONCILE_INTERVAL_SECONDS = 60.0
RECONCILE_BATCH_LIMIT = 20
#: 判定"旧主已死"的超龄阈值（秒）。必须 **> Go 侧运行锁 TTL（5min）**：
#: 锁还在就说明旧主还活着，此时接管等于抢走活着的现场。
STALE_AFTER_SECONDS = 6 * 60

#: "已开始未结束"的调用在历史里的占位（**不自动重放**：那些调用可能已产生副作用）
UNKNOWN_RESULT = (
    "（本次调用已开始但未结束，结果未知；它可能已经产生了副作用，因此**没有**自动重放。"
    "如需重做，请重新发起该调用。）"
)


@dataclass
class ResumeState:
    """一次可用的续跑现场。"""

    messages: list[dict[str, Any]] = field(default_factory=list)
    turn_index: int = 0
    done_tools: tuple[str, ...] = ()
    #: "已开始未结束"的调用 id（`tool_calls.output = ''`）—— 不自动重放，只告知
    replay_pending: tuple[str, ...] = ()
    #: `hot`（用户无感）/ `cold`（续跑但不承诺事件补齐）
    window: str = ""
    #: 降级原因（非空 = **没能**续跑，调用方按现状语义走）
    reason: str = ""


async def load_resume_state(*, tenant_id: str, session_id: str) -> ResumeState | None:
    """读该会话的续跑现场；不可用时返回 None（调用方回落现状语义）。"""
    if not session_id:
        return None

    from app.agent import checkpoint as ckpt

    snapshot = await ckpt.load(tenant_id=tenant_id, session_id=session_id)
    if not snapshot:
        return None

    window = ckpt.resume_window(snapshot)
    if window == "abandoned":
        # 超窗：**可见**地放弃（方案 §3.1 的降级路径必须显式），并回落现状语义
        await ckpt.mark(
            tenant_id=tenant_id, session_id=session_id, status=ckpt.STATUS_ABANDONED
        )
        logger.warning(
            "checkpoint window exceeded (session=%s) — marked abandoned; "
            "the user can retry the whole turn",
            session_id,
        )
        return None

    messages = [m for m in (snapshot.get("messages") or []) if isinstance(m, dict)]
    if not messages:
        # 快照损坏 / 空：同样回落现状（并把原因写清楚，别让"没续跑"看起来像"续跑了"）
        logger.warning("checkpoint for session=%s is unusable (empty) — falling back", session_id)
        return None

    done = tuple(str(item) for item in (snapshot.get("done_tools") or []))
    pending_rows = await ckpt.pending_tool_calls(session_id=session_id)
    pending = tuple(
        str(row.get("id") or "")
        for row in pending_rows
        if row.get("id") and str(row.get("id")) not in done
    )

    return ResumeState(
        messages=messages,
        turn_index=int(snapshot.get("turn_index") or 0),
        done_tools=done,
        replay_pending=tuple(item for item in pending if item),
        window=window,
    )


def merge_user_message(
    messages: list[dict[str, Any]], content: str
) -> list[dict[str, Any]]:
    """把本次的用户消息并进续跑历史 —— **尾部已有同内容则不重复追加**。

    用户重试时那条消息通常**早就在快照里**（快照是回合末落的，当时的历史已含它）。
    无条件 append 会让同一句话出现两次，而模型会把它理解成"用户又说了一遍"。
    """
    text = (content or "").strip()
    if not text:
        return messages
    for msg in reversed(messages[-3:]):
        if msg.get("role") == "user" and str(msg.get("content", "")).strip() == text:
            return messages
    return [*messages, {"role": "user", "content": content}]


def with_replay_pending(
    messages: list[dict[str, Any]], pending_ids: tuple[str, ...]
) -> list[dict[str, Any]]:
    """给"已开始未结束"的调用补一条**结果未知**的 tool 消息。

    为什么不自动重放：那些调用可能**已经产生了副作用**（写文件、发请求），自动重放就是重复
    副作用。所以只把"结果未知"明确摆到历史里，由模型（或用户）决定是否重做。
    """
    wanted = [pid for pid in pending_ids if pid]
    if not wanted:
        return messages
    have = {
        str(msg.get("tool_call_id") or "")
        for msg in messages
        if msg.get("role") == "tool"
    }
    missing = [pid for pid in wanted if pid not in have]
    if not missing:
        return messages
    # 追加在末尾；调用方随后会跑 `_ensure_valid_tool_sequence` 修配对（与既有历史同一处理）
    return [
        *messages,
        *(
            {"role": "tool", "tool_call_id": pid, "content": UNKNOWN_RESULT}
            for pid in missing
        ),
    ]


# ── reconciler ──────────────────────────────────────────────────────────


def _metrics() -> Any:
    try:
        from app.observability import metrics

        return metrics
    except Exception:  # noqa: BLE001 — 指标不可用不该影响接管
        return None


def _count(outcome: str) -> None:
    """巡检指标（`run_reconcile_total{outcome}`）—— 指标失败绝不影响接管。"""
    metrics = _metrics()
    if metrics is None:
        return
    try:
        metrics.RUN_RECONCILE_TOTAL.labels(outcome=outcome).inc()
    except Exception:  # noqa: BLE001
        pass


def _parse_snapshot(raw: Any) -> dict[str, Any] | None:
    """把 `agent_runs.checkpoint` 列解析成 dict（JSONB 与字符串两种返回形态都接受）。"""
    if not raw:
        return None
    if isinstance(raw, dict):
        return dict(raw)
    try:
        import json as _json

        parsed = _json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _lock_is_held(session_id: str) -> bool:
    """Redis 里还有该会话的运行锁吗？（Go 侧 `agent:run-lock:<sid>`，TTL 5min + 心跳续期）

    **这是接管不变量的关键一半**：锁还在 = 旧主还活着 = 不能接管。
    Redis 不可用时保守返回 True（**不接管**）—— 宁可少接管，不可抢现场。
    """
    try:
        from app.redis_client import get_redis

        redis = await get_redis()
        if redis is None:
            return True
        raw = await redis.get(f"agent:run-lock:{session_id}")
        return raw is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconciler: lock check failed (session=%s): %s", session_id, exc)
        return True


def rebuild_task(
    *,
    tenant_id: str,
    session_id: str,
    snapshot: dict[str, Any],
    run_token: str,
) -> Any | None:
    """从快照的 `task_meta` 重建一个可续跑的 `AgentTask`；缺关键字段则返回 None。

    **不带 `content`**：那条用户消息已经在快照历史里（`merge_user_message` 也不会重复追加）。
    **不带 `tools`**：工具面由 `llm_config.mode` 决定（见 `_resume_task_meta` 的说明）。
    """
    from app.agent.runtime import AgentTask

    meta = snapshot.get("task_meta")
    if not isinstance(meta, dict) or not meta:
        return None
    llm_config = meta.get("llm_config")
    if not isinstance(llm_config, dict):
        return None

    return AgentTask(
        id=f"resume_{uuid.uuid4().hex[:8]}",
        tenant_id=tenant_id,
        user_id=str(meta.get("user_id") or ""),
        session_id=session_id,
        content="",
        system_prompt=str(meta.get("system_prompt") or ""),
        llm_config=dict(llm_config),
        workbench_context=dict(meta.get("workbench_context") or {}),
        max_turns=int(meta.get("max_turns") or 0) or 5,
        subagent_depth=int(meta.get("subagent_depth") or 0),
        run_token=run_token,
    )


def _auto_resume_enabled() -> bool:
    """自动续跑开关（默认开）。只在**热窗口**内生效 —— 见 `sweep_once`。"""
    try:
        from app.config import settings

        return bool(getattr(settings, "run_auto_resume_enabled", True))
    except Exception:  # noqa: BLE001
        return False


async def sweep_once(
    *, limit: int = RECONCILE_BATCH_LIMIT, runner: Any | None = None
) -> dict[str, int]:
    """扫一轮僵尸 run：验证旧主不在 → （热窗口内）**自动续跑** 或 标记可续跑 / 放弃。

    规则（保守优先）：

    * 运行锁**仍在** ⇒ 旧主活着 ⇒ 跳过（不抢现场）；Redis 不可用时同样跳过；
    * 快照已超**冷窗口** ⇒ `abandoned`（可见，不静默）；
    * 快照在**热窗口**（1h）内 **且** 提供了 `runner` **且** 开关打开 ⇒ 用 CAS 抢占后
      `runner(task)` **自动续跑**（带 `task_meta` 重建的 task）；
    * 其余（冷窗口 / 未提供 runner / 开关关闭 / 重建失败）⇒ 标记 `checkpointed`
      （"有快照、无人跑"，等**用户重试**走入口恢复）。

    **为什么只自动续跑热窗口**：热窗口内"用户无感"才成立（SSE 重放能补齐缺口）；冷窗口里
    用户早已离开，自动烧 token 替他跑完，是花钱买一个他可能不想要的答案。
    """
    from app.agent import checkpoint as ckpt

    stats = {
        "scanned": 0,
        "resumed": 0,
        "revived": 0,
        "abandoned": 0,
        "skipped_alive": 0,
        "contended": 0,
        "failed": 0,
    }
    pool = ckpt._pool()  # noqa: SLF001 — 与 checkpoint 同一池来源（同模块族）
    if pool is None:
        return stats

    try:
        rows = await pool.fetch(
            """
            SELECT session_id, tenant_id, status, checkpoint, run_token
              FROM agent_runs
             WHERE status IN ('running', 'checkpointed')
               AND COALESCE(checkpoint_at, updated_at, created_at)
                   < now() - make_interval(secs => $1::int)
             ORDER BY COALESCE(checkpoint_at, updated_at, created_at) ASC
             LIMIT $2
            """,
            int(STALE_AFTER_SECONDS),
            max(1, int(limit)),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconciler: scan failed: %s", exc)
        return stats

    for row in rows or []:
        stats["scanned"] += 1
        session_id = str(row["session_id"] or "")
        tenant_id = str(row["tenant_id"] or "")
        if not session_id:
            continue
        try:
            if await _lock_is_held(session_id):
                stats["skipped_alive"] += 1
                _count("skipped_alive")
                continue

            parsed = _parse_snapshot(row["checkpoint"])
            window = ckpt.resume_window(cast("RunCheckpoint", parsed)) if parsed else "abandoned"

            if window == "abandoned":
                await ckpt.mark(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    status=ckpt.STATUS_ABANDONED,
                )
                stats["abandoned"] += 1
                _count("abandoned")
                logger.warning(
                    "reconciler: session=%s 快照已超冷窗口 → abandoned（可见）", session_id
                )
                continue

            if window == "hot" and runner is not None and _auto_resume_enabled() and parsed:
                new_token = uuid.uuid4().hex
                claimed = await ckpt.claim(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    expected_run_token=str(row["run_token"] or ""),
                    new_run_token=new_token,
                )
                if claimed:
                    task = rebuild_task(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        snapshot=parsed,
                        run_token=new_token,
                    )
                    if task is not None:
                        logger.info(
                            "reconciler: session=%s 接管并续跑（hot window, turn=%s）",
                            session_id,
                            parsed.get("turn_index"),
                        )
                        await runner(task)
                        stats["resumed"] += 1
                        _count("resumed")
                        continue
                    # 重建失败：**回落**为"标记可续跑"（不把 run 卡在 resuming）
                    logger.warning(
                        "reconciler: session=%s 快照缺 task_meta，无法自动续跑 → 标记可续跑",
                        session_id,
                    )
                    await ckpt.mark(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        status=ckpt.STATUS_CHECKPOINTED,
                    )
                    stats["revived"] += 1
                    _count("revived")
                    continue
                # CAS 没抢到：说明**另一个实例正在接管** —— 这里必须什么都不做。
                # （`mark()` 的条件包含 `resuming`，此时再标记会把对方刚抢到的现场覆盖掉。）
                stats["contended"] += 1
                _count("contended")
                continue

            # 冷窗口 / 未开启自动续跑 / 没提供 runner：交给**用户重试**走入口恢复
            if str(row["status"]) == ckpt.STATUS_RUNNING and parsed is not None:
                await ckpt.mark(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    status=ckpt.STATUS_CHECKPOINTED,
                )
            stats["revived"] += 1
            _count("revived")
            logger.info(
                "reconciler: session=%s 旧主已不在 → 标记为可续跑（等用户重试）", session_id
            )
        except Exception as exc:  # noqa: BLE001 — 单行失败不影响其余
            stats["failed"] += 1
            logger.warning("reconciler: session=%s failed: %s", session_id, exc)

    if stats["scanned"]:
        logger.info("reconciler sweep: %s", stats)
    return stats


async def start_reconciler(
    *, interval: float = RECONCILE_INTERVAL_SECONDS, runner: Any | None = None
) -> asyncio.Task[None] | None:
    """启动 reconciler 后台任务：**启动一次 + 周期 + 每实例抖动**。

    抖动是刻意的：多副本同时扫描会把同一批行反复挑出来（虽然 CAS 能裁决，但那是白白的 DB
    往返）。抖动把各实例的扫描点错开。

    `runner` 是"真续跑"的执行体（由 `app/main.py` 注入 —— 只有那里有 gateway /
    session_store / memory 三件依赖）。为 None 时**不自动续跑**，只做状态修正。
    返回 task 以便关闭时取消；`interval <= 0` 时只跑一轮（单次巡检用）。
    """
    from app.config import settings

    if not getattr(settings, "run_reconciler_enabled", True):
        logger.info("run reconciler disabled by settings")
        return None

    async def _loop() -> None:
        # 启动时的抖动：避免所有副本在同一秒一起扫
        await asyncio.sleep(random.uniform(0, min(interval, 10.0)) if interval > 0 else 0)
        while True:
            try:
                await sweep_once(runner=runner)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 巡检失败不该杀死任务
                logger.warning("reconciler sweep failed: %s", exc)
            if interval <= 0:
                return
            await asyncio.sleep(interval + random.uniform(0, interval * 0.1))

    task = asyncio.create_task(_loop(), name="run-reconciler")
    logger.info(
        "run reconciler started (interval=%ss, auto_resume=%s)", interval, runner is not None
    )
    return task
