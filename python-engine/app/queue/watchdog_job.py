"""Watchdog 的调度面：一个 tick 做完整的一轮「采集 → 判断 → 处置 → 审计」。

**为什么长在队列 worker 上，而不是新起一个常驻进程**

roadmap P1-3 要求"走 `queue/worker.py` 的既有租约/重领/幂等骨架，避免多副本重复执行"。
多副本同时判断同一个回合不仅浪费，还会让**同一条提示被注入两次**（steer 通道没有
跨副本去重）。所以这里的互斥用 Redis 租约而非进程内锁：抢不到的副本这一拍什么都不做。

**默认什么都不做**

两道开关分开：

- ``SUPERAGENT_WATCHDOG_ENABLED``（默认 **false**）—— 关掉时连采集都不跑，零影响；
- ``SUPERAGENT_WATCHDOG_ACT``（默认 **false**）—— 开了观察但没开动作时，判断结果只进
  审计与日志，**不注入提示、不终止**。这就是 roadmap 说的"默认只观察不动作"，也是
  误判代价不对称时唯一站得住的默认值。

**样本历史放进程内**

判断需要"连续多拍"，但把采样序列写 Redis 等于给每个活跃回合加一份会腐烂的旁路数据。
代价是租约漂到另一个副本时历史归零 —— 那一拍会因 `insufficient samples` 判 ``CONTINUE``，
也就是**退化为不干预**，正是 fail-closed 想要的方向。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.agent.watchdog import Assessment, Sample, Verdict, assess, nudge_text

if TYPE_CHECKING:
    from app.queue.watchdog_probe import TurnSample

logger = logging.getLogger(__name__)

#: 租约键：同一时刻只允许一个副本判断（前缀由 `rkey` 统一加）。
LEASE_NAME = "engine:watchdog:lease"
#: 租约时长必须**大于**一次 tick 的耗时上限，否则两个副本会前后脚都跑起来。
LEASE_TTL_SECONDS = 120

#: 每个回合保留多少拍历史。判断阈值最多到 4 拍，留 8 拍足够看清趋势。
HISTORY_LIMIT = 8

#: 审计资源类型：用于把"watchdog 干预"从别的流水里筛出来。
AUDIT_RESOURCE = "agent_watchdog"
AUDIT_NUDGE = "superagent.watchdog.nudge"
AUDIT_SUGGEST_STOP = "superagent.watchdog.suggest_stop"

#: 进程内：turn_id → 最近若干拍。见模块 docstring 关于"租约漂移即退化"的取舍。
_HISTORY: dict[str, list[Sample]] = {}


@dataclass
class TickResult:
    """一拍的结果。返回出来是为了让测试断言"关掉时确实什么都没做"。"""

    observed: int = 0
    verdicts: list[tuple[str, Assessment]] = field(default_factory=list)
    acted: int = 0
    skipped: str = ""


def lease_key() -> str:
    from app.redis_keys import rkey

    return rkey(LEASE_NAME)


def reset_history() -> None:
    """清空进程内历史（测试用；生产不需要显式调用）。"""
    _HISTORY.clear()


async def _acquire_lease(redis: Any, *, ttl: int = LEASE_TTL_SECONDS) -> bool:
    """抢本拍的执行权。Redis 出错时返回 False（不判断优于重复判断）。"""
    if redis is None:
        return False
    try:
        return bool(await redis.set(lease_key(), uuid.uuid4().hex, nx=True, ex=ttl))
    except Exception as exc:  # noqa: BLE001 - 抢不到/出故障都按"本拍不跑"
        logger.debug("watchdog: lease acquire failed (%s)", exc)
        return False


async def _release_lease(redis: Any) -> None:
    """主动释放，把等待期从"租约自然过期"缩短到一个 tick 的间隔。"""
    if redis is None:
        return
    try:
        await redis.delete(lease_key())
    except Exception as exc:  # noqa: BLE001 - 释放失败最多让它多占一个 TTL
        logger.debug("watchdog: lease release failed (%s)", exc)


def _remember(turn_id: str, sample: Sample) -> list[Sample]:
    """把这个回合的最近若干拍取出来（含本拍）。"""
    history = _HISTORY.setdefault(turn_id, [])
    history.append(sample)
    if len(history) > HISTORY_LIMIT:
        del history[: len(history) - HISTORY_LIMIT]
    return list(history)


def _prune(active_turn_ids: set[str]) -> None:
    """丢掉已经不在 running 的回合历史，避免长期运行时无界增长。"""
    for turn_id in list(_HISTORY):
        if turn_id not in active_turn_ids:
            del _HISTORY[turn_id]


async def _audit(
    pool: Any,
    *,
    action: str,
    turn: TurnSample,
    assessment: Assessment,
    acted: bool,
) -> None:
    """把一次干预（或建议）落进 `audit_logs`。**fail-soft**：审计失败不影响判断。

    `details` 里只放摘要与结论，不放消息原文 —— 审计的用途是"解释系统为什么动手"，
    不是再存一份对话。
    """
    if pool is None:
        return
    details = {
        "turn_id": turn.sample.turn_id,
        "session_id": turn.session_id,
        "verdict": str(assessment.verdict),
        "stalled_samples": assessment.stalled,
        "reason": assessment.reason,
        "acted": acted,
        # 检索时最常用的两项放平铺字段会更方便，但 audit_logs.details 是 json，
        # 这里保持一致形状，便于统一按 jsonb 查询。
    }
    import json

    try:
        await pool.execute(
            """
            INSERT INTO audit_logs
                (id, tenant_id, user_id, action, resource_type, resource_id, details, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            str(uuid.uuid4()),
            turn.tenant_id,
            turn.user_id,
            action,
            AUDIT_RESOURCE,
            turn.sample.turn_id,
            json.dumps(details, ensure_ascii=False),
            datetime.now(UTC).replace(tzinfo=None),
        )
    except Exception as exc:  # noqa: BLE001 - 审计是旁路
        logger.warning(
            "watchdog: audit failed (turn=%s action=%s): %s",
            turn.sample.turn_id,
            action,
            str(exc)[:160],
        )


async def _apply(
    pool: Any,
    *,
    turn: TurnSample,
    assessment: Assessment,
    act: bool,
    redis: Any,
) -> bool:
    """按判断结果处置。返回"是否真的做了动作"。"""
    if assessment.verdict is Verdict.NUDGE:
        if not act:
            await _audit(pool, action=AUDIT_NUDGE, turn=turn, assessment=assessment, acted=False)
            return False
        text = nudge_text(assessment)
        # 复用 steer 通道：它已经在轮次边界注入、落库、发 SSE —— 再造一条注入链路
        # 只会多一套"什么时候生效"的语义。`source="watchdog"` 目前只作为调用意图的
        # 标注（`push_steer` 还没有把它写进落库内容），历史里**分辨不出来源**；
        # 要区分得看 `audit_logs` 的 `superagent.watchdog.nudge` 流水。
        from app.agent.steer import push_steer

        ok, reason = await push_steer(turn.session_id, text, source="watchdog", redis=redis)
        await _audit(pool, action=AUDIT_NUDGE, turn=turn, assessment=assessment, acted=ok)
        if not ok:
            logger.info("watchdog: nudge not delivered (turn=%s): %s", turn.sample.turn_id, reason)
        return ok

    if assessment.verdict is Verdict.SUGGEST_STOP:
        # **不终止**。终止是不可逆动作，必须由人来做 —— 这一档只留证据。
        await _audit(
            pool, action=AUDIT_SUGGEST_STOP, turn=turn, assessment=assessment, acted=False
        )
        logger.warning(
            "watchdog: turn looks stuck (turn=%s session=%s stalled=%d) — 建议人工确认是否终止",
            turn.sample.turn_id,
            turn.session_id,
            assessment.stalled,
        )
        return False

    return False


async def tick(
    *,
    redis: Any,
    enabled: bool = False,
    act: bool = False,
    nudge_after: int = 2,
    stop_after: int = 4,
    collect_fn: Any = None,
    pool: Any = None,
) -> TickResult:
    """跑一拍。``enabled=False`` 时立即返回（零副作用，连采集都不做）。

    ``collect_fn`` / ``pool`` 可注入，便于测试不碰数据库即可回放"兜圈子样本"、
    并断言审计确实写了。生产路径下两者都从既有单例取。
    """
    if not enabled:
        return TickResult(skipped="disabled")

    if not await _acquire_lease(redis):
        return TickResult(skipped="lease_busy")

    try:
        if collect_fn is None:
            from app.queue import watchdog_probe as _probe

            collect_fn = _probe.collect

        turns = await collect_fn()
        result = TickResult(observed=len(turns or []))
        _prune({t.sample.turn_id for t in (turns or [])})

        if pool is None:
            try:
                from app.db import get_pool

                pool = get_pool()
            except Exception as exc:  # noqa: BLE001 - 只影响审计与采集，不影响判断
                logger.debug("watchdog: db unavailable for audit (%s)", exc)

        for turn in turns or []:
            history = _remember(turn.sample.turn_id, turn.sample)
            judgement = assess(history, nudge_after=nudge_after, stop_after=stop_after)
            if judgement.verdict is Verdict.CONTINUE:
                continue
            result.verdicts.append((turn.sample.turn_id, judgement))
            if await _apply(pool, turn=turn, assessment=judgement, act=act, redis=redis):
                result.acted += 1
        return result
    finally:
        await _release_lease(redis)


def _setting(name: str, default: Any) -> Any:
    """读配置；读不到就用默认值（fail-closed：默认值是"关"）。"""
    try:
        from app.config import settings

        return getattr(settings, name, default)
    except Exception:  # noqa: BLE001
        return default


async def watchdog_loop(redis: Any, *, should_stop: Any = None) -> None:
    """后台循环。间隔取配置；异常不得把循环打散（与 `_delay_loop` 同一取向）。"""
    interval = int(_setting("superagent_watchdog_interval", 300) or 300)
    interval = max(60, interval)
    logger.info("superagent watchdog loop started (every %ss)", interval)
    while True:
        try:
            await _wait(interval)
            if should_stop is not None and should_stop():
                return
            await tick(
                redis=redis,
                enabled=bool(_setting("superagent_watchdog_enabled", False)),
                act=bool(_setting("superagent_watchdog_act", False)),
                nudge_after=int(_setting("superagent_watchdog_nudge_after", 2)),
                stop_after=int(_setting("superagent_watchdog_stop_after", 4)),
            )
        except Exception as exc:  # noqa: BLE001 - 看门狗自己不能把循环搞死
            logger.warning("superagent watchdog tick failed: %s", str(exc)[:200])


async def _wait(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(max(1.0, float(seconds)))
