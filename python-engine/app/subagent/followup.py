"""子 Agent 完成后的「自动唤起父会话新一轮」(followup) 信号。

为什么需要
----------
``run_in_background=True`` 的子 Agent 结束后，父模型只能**主动**调用
``read_subagent_result(run_id)`` —— 而父 turn 往往在派发后立刻结束了，没有任何一轮会去
读它，子 Agent 的结论就此沉底。这正是"主 Agent 与子 Agent 没打通"在语义层的根因：
上行通道（进度事件）与下行通道（结果回到主对话）**都依赖父 turn 还活着**。

本模块只负责把"某个后台 run 结束了"变成一条**幂等、可重试、跨实例**的信号：

    engine:tasks 队列  →  Go 内部端点  →  复用 HandleSubmit 开新一轮

执行**不**在引擎侧：新一轮必须由 Go 发起 —— ``messages`` 落库、SSE 推送、turn 状态、
计费与会话锁全部在 Go（引擎侧零 ``INSERT INTO messages``）。见
``internal/api/agent_followup.go``。

风控（保守默认，可用环境变量放宽）
----------------------------------
* **幂等**：同一 ``run_id`` 只投递一次（``SET NX``，TTL 24h）—— 取消路径与正常终态
  都会走到这里，靠它去重；
* **速率**：每会话每小时最多 ``SUBAGENT_FOLLOWUP_MAX_PER_HOUR`` 次（默认 3）；
* **禁级联**：由 followup 唤起的那一轮里再派生的子 Agent 不再触发（``cascade``）——
  否则会自我放大成递归；
* Redis 不可用时**不投递**：宁可不动，也不要降级成"可能重复唤起"。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

FOLLOWUP_TASK_TYPE = "agent_followup"
DEFAULT_MAX_PER_HOUR = 3
_IDEMPOTENCY_TTL = 24 * 3600
_RATE_WINDOW = 3600

#: 不阻塞的投递任务需持引用，否则可能被 GC 静默回收
_PENDING: set[asyncio.Task] = set()


def max_per_hour() -> int:
    """每会话每小时的自动唤起上限（<=0 表示关闭该机制）。"""
    raw = (os.getenv("SUBAGENT_FOLLOWUP_MAX_PER_HOUR") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_PER_HOUR
    except ValueError:
        value = DEFAULT_MAX_PER_HOUR
    return max(0, value)


async def enqueue_followup(
    *,
    run_id: str,
    session_id: str,
    tenant_id: str = "",
    user_id: str = "",
    status: str = "",
    summary: str = "",
    profile: str = "",
    depth: int = 0,
    cascade: bool = False,
) -> bool:
    """投递一次「唤起父会话新一轮」的信号，返回是否真的投递了。

    Args:
        run_id: 子 Agent run（同时用作幂等键 —— 必须是稳定 id，不能用时间戳）。
        session_id: **父**会话 id：新一轮就开在这个会话上。
        cascade: 该 run 是否由 followup 唤起的那一轮派生（True 则不再触发，防递归）。
    """
    if not run_id or not session_id:
        return False
    if cascade:
        logger.info("subagent followup 跳过（级联保护）: run=%s", run_id)
        return False

    limit = max_per_hour()
    if limit <= 0:
        logger.info("subagent followup 已关闭（上限<=0）: run=%s", run_id)
        return False

    try:
        from app.redis_client import get_redis
        from app.redis_keys import rkey

        redis = await get_redis()
    except Exception as exc:  # noqa: BLE001 - 拿不到 Redis 就不投递
        logger.warning("subagent followup 跳过（Redis 不可用）: %s", str(exc)[:160])
        return False
    if redis is None:
        logger.warning("subagent followup 跳过（无 Redis 客户端）: run=%s", run_id)
        return False

    try:
        # ① 幂等：同一 run 只投递一次（取消路径 + 正常终态都会走到这里）
        first = await redis.set(rkey(f"agent_followup:run:{run_id}"), "1",
                               nx=True, ex=_IDEMPOTENCY_TTL)
        if not first:
            logger.debug("subagent followup 已投递过: run=%s", run_id)
            return False

        # ② 速率：每会话每小时上限（超限保留幂等位，避免同一 run 反复尝试）
        rate_key = rkey(f"agent_followup:rate:{session_id}")
        count = await redis.incr(rate_key)
        if count == 1:
            await redis.expire(rate_key, _RATE_WINDOW)
        if count > limit:
            # 释放幂等位再退出：否则这个 run 的结果**永久**无法投递
            # （幂等位已占 → 下次进来会直接 return False）。
            # 队列侧本来就有 idempotency_key=agent_followup:{run_id}，重复投递是安全的，
            # 所以这里宁可让它以后还能再试，也不要"静默永久丢弃"。
            try:
                await redis.delete(rkey(f"agent_followup:run:{run_id}"))
            except Exception:  # noqa: BLE001 - 释放失败不影响本次返回
                pass
            logger.warning(
                "subagent followup 超速率上限（%s/h）: session=%s run=%s "
                "— 已保留重试机会（结果仍可通过 read_subagent_result 取回）",
                limit, session_id, run_id,
            )
            return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("subagent followup 前置检查失败: %s", str(exc)[:160])
        return False

    payload: dict[str, Any] = {
        "run_id": run_id,
        "session_id": session_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "status": status,
        "profile": profile,
        "depth": int(depth or 0),
        "summary": (summary or "")[:2000],
    }
    try:
        from app.queue.producer import QueueProducer

        producer = QueueProducer(redis)
        task_id = await producer.enqueue(
            FOLLOWUP_TASK_TYPE,
            tenant_id or "default",
            payload,
            priority=1,
            deadline_seconds=1800,
            # 稳定业务键：worker 的 claim 只拒绝 completed，同 run 重投会被丢弃
            idempotency_key=f"agent_followup:{run_id}",
        )
        logger.info("subagent followup 已投递: run=%s task=%s", run_id, task_id)
        return True
    except Exception as exc:  # noqa: BLE001 - 投递失败只记日志（run 本身已经收尾）
        # 同样释放幂等位：投递失败若留下幂等位，这个 run 的结果就再也没机会送达了。
        # run 本身已收尾并落库，因此这里失败只表现为"父会话少了一轮自动追加"，
        # 结果仍可通过 read_subagent_result 取回。
        try:
            from app.redis_client import get_redis
            from app.redis_keys import rkey as _rkey

            _r = await get_redis()
            if _r is not None:
                await _r.delete(_rkey(f"agent_followup:run:{run_id}"))
        except Exception:  # noqa: BLE001
            pass
        logger.error(
            "subagent followup 投递失败（已释放幂等位，可重试）: run=%s err=%s",
            run_id,
            str(exc)[:200],
        )
        return False


def fire_followup(**kwargs: Any) -> None:
    """不阻塞地触发一次 followup 投递（收口点用，绝不能拖慢/阻塞子 Agent 收尾）。"""
    try:
        task = asyncio.create_task(enqueue_followup(**kwargs))
    except RuntimeError:  # 无运行中的事件循环：放弃投递（不是致命问题）
        logger.debug("subagent followup 无事件循环可用，跳过")
        return
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
