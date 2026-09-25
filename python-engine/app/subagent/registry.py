"""子 Agent run 注册表 —— 取消与清理的**唯一真相源**。

替代原先散在 ``app/tools/subagent.py`` 的 ``_BG_TASKS``（那里只负责"持引用，别被 GC"）。
现在它同时承担三件事：

1. **持引用**（原职责，注释里那句"必须持引用否则可能被 GC 静默回收"仍成立）；
2. **取消**：按 run 或按会话取消 —— ``task.cancel()`` 之后，``SubAgentRunner`` 里早已写好的
   ``CancelledError`` 收尾分支会产出终态、事件与落库，**这里不需要重复实现收尾逻辑**；
3. **清理**：看门狗（空闲/超时自动中止）与取消订阅（跨实例广播）。

为什么必须跨实例广播：Go 侧没有"run → 引擎实例"的映射（后台 run 由父 turn 所在实例持有），
所以取消请求经 Redis ``subagent:cancel`` 广播，由**持有该 run 的实例**执行。

阈值（保守默认，环境变量可覆盖；设 0 关闭对应机制）：

* ``SUBAGENT_IDLE_TIMEOUT``（默认 300s）—— 距最近事件超过它即视为卡死；
* ``SUBAGENT_MAX_RUNTIME``（默认 1800s）—— 单 run 总时长硬上限。

心跳来源是**事件**：``main.py`` 的常驻投递器每投递一条事件就 ``touch(run_id)``。
因此"还在产出事件的长任务"不会被误判为空闲。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_IDLE_TIMEOUT = 300
DEFAULT_MAX_RUNTIME = 1800
WATCHDOG_INTERVAL = 60

#: 取消原因码（写入 subagent_runs.error，前端据此区分"被停掉"与"失败"）
REASON_USER = "cancelled_by_user"
REASON_SESSION = "cancelled_by_session"
REASON_PARENT = "parent_cancelled"
REASON_IDLE = "idle_timeout"
REASON_MAX_RUNTIME = "max_runtime"

#: 外部（网关广播）用的简写 → 内部原因码
_EXTERNAL_REASONS = {
    "user": REASON_USER,
    "session": REASON_SESSION,
    "parent": REASON_PARENT,
    "idle": REASON_IDLE,
    "timeout": REASON_MAX_RUNTIME,
}


@dataclass
class RunHandle:
    """一个活跃后台 run 的句柄（进程内）。"""

    task: asyncio.Task[Any]
    session_id: str = ""
    tenant_id: str = ""
    profile: str = ""
    started_at: float = field(default_factory=time.time)
    last_event_at: float = field(default_factory=time.time)
    reason: str = ""


_RUNS: dict[str, RunHandle] = {}
_BY_SESSION: dict[str, set[str]] = {}
_WATCHDOG: asyncio.Task[Any] | None = None
_SUBSCRIBER: asyncio.Task[Any] | None = None


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(0, value)


# ── 注册表（进程内，单事件循环，无需锁）────────────────────────────


def register(
    run_id: str,
    task: asyncio.Task[Any],
    *,
    session_id: str = "",
    tenant_id: str = "",
    profile: str = "",
) -> None:
    if not run_id:
        return
    _RUNS[run_id] = RunHandle(task=task, session_id=session_id,
                              tenant_id=tenant_id, profile=profile)
    if session_id:
        _BY_SESSION.setdefault(session_id, set()).add(run_id)


def unregister(run_id: str) -> None:
    handle = _RUNS.pop(run_id, None)
    if handle is None:
        return
    if handle.session_id:
        ids = _BY_SESSION.get(handle.session_id)
        if ids is not None:
            ids.discard(run_id)
            if not ids:
                _BY_SESSION.pop(handle.session_id, None)


def touch(run_id: str) -> None:
    """刷新心跳（由事件投递器调用：**事件即心跳**）。"""
    handle = _RUNS.get(run_id)
    if handle is not None:
        handle.last_event_at = time.time()


def cancel(run_id: str, reason: str) -> bool:
    """取消单个 run；返回是否真的发出了取消（未知 run / 已结束返回 False，幂等）。"""
    handle = _RUNS.get(run_id)
    if handle is None:
        return False
    handle.reason = reason
    handle.task.cancel()
    logger.warning("subagent cancel requested: run=%s reason=%s", run_id, reason)
    return True


def cancel_session(session_id: str, reason: str) -> int:
    """取消某会话下所有活跃 run，返回数量。"""
    if not session_id:
        return 0
    run_ids = list(_BY_SESSION.get(session_id, ()))
    return sum(1 for run_id in run_ids if cancel(run_id, reason))


def reason_of(run_id: str) -> str:
    """取取消原因（供 runner 收尾时写进 subagent_runs.error）。"""
    handle = _RUNS.get(run_id)
    return handle.reason if handle is not None else ""


def list_active(session_id: str = "") -> list[dict[str, Any]]:
    """活跃 run 快照（诊断/内部端点用）。"""
    now = time.time()
    out = []
    for run_id, handle in _RUNS.items():
        if session_id and handle.session_id != session_id:
            continue
        out.append({
            "run_id": run_id,
            "session_id": handle.session_id,
            "profile": handle.profile,
            "elapsed_s": round(now - handle.started_at, 1),
            "idle_s": round(now - handle.last_event_at, 1),
        })
    return out


# ── 看门狗：空闲 / 超时长自动中止 ──────────────────────────────────


async def watchdog_tick(*, idle_timeout: int, max_runtime: int, now: float | None = None) -> list[str]:
    """扫一遍活跃 run，命中阈值就取消；返回被取消的 run_id 列表。

    抽成"能单独调用"的形式，便于测试直接喂 ``now``（不必等真实时钟）。
    """
    if idle_timeout <= 0 and max_runtime <= 0:
        return []
    ts = time.time() if now is None else now
    fired: list[str] = []
    for run_id, handle in list(_RUNS.items()):
        reason = ""
        if idle_timeout > 0 and ts - handle.last_event_at > idle_timeout:
            reason = REASON_IDLE
        elif max_runtime > 0 and ts - handle.started_at > max_runtime:
            reason = REASON_MAX_RUNTIME
        if not reason:
            continue
        logger.warning(
            "subagent watchdog fired: run=%s reason=%s idle=%.0fs elapsed=%.0fs",
            run_id, reason, ts - handle.last_event_at, ts - handle.started_at,
        )
        if cancel(run_id, reason):
            fired.append(run_id)
    return fired


#: reaper 的连续失败状态，用于熔断与降频。
#:
#: 为什么需要：REAP_STALE_SQL 曾因参数类型错误（SQL 期望 text、调用方传 int）**每次执行
#: 都失败**，而这里原本只记一条 warning 且每 60s 重试 —— 于是这个"僵尸收口器"在长期
#: 不可用时没有任何人察觉，DB 里 status='running' 的行永久残留（实测有一行停了 37 小时）。
#: 收口器必须"会升级、会退避、会被看见"。
_reap_failures = 0
_reap_next_attempt = 0.0
_REAP_BACKOFF_CAP = 3600.0  # 失败退避上限 1 小时


async def _reap_once(max_age_hours: int) -> bool:
    """僵尸收口一次：进程内注册表管不到"重启前遗留"的 run，只能靠 DB 判定。

    返回是否成功。连续失败会指数退避，并在第 3 次起升级为 error 且写明后果。
    """
    global _reap_failures, _reap_next_attempt

    now = time.time()
    if _reap_failures and now < _reap_next_attempt:
        return False  # 熔断中：跳过本次，避免刷日志

    try:
        from app.tools.subagent import _get_store

        store = _get_store()
        if store is None:
            return False
        count = await store.reap_stale_runs(max_age_hours=max_age_hours)
        if _reap_failures:
            logger.info("subagent reaper recovered after %d failure(s)", _reap_failures)
        _reap_failures = 0
        if count:
            logger.warning("subagent reaper: marked %d stale run(s) as lost", count)
        return True
    except Exception as exc:  # noqa: BLE001 - 收口失败不影响引擎
        _reap_failures += 1
        backoff = min(2 ** (_reap_failures - 1) * WATCHDOG_INTERVAL, _REAP_BACKOFF_CAP)
        _reap_next_attempt = now + backoff
        if _reap_failures >= 3:
            logger.error(
                "subagent reaper failing repeatedly (%d in a row, backing off %.0fs): %s"
                " — 失联的 run 不会被标为 lost，前端会永久显示'运行中'",
                _reap_failures,
                backoff,
                str(exc)[:200],
            )
        else:
            logger.warning(
                "subagent reaper failed (%d): %s", _reap_failures, str(exc)[:200]
            )
        return False


async def watchdog_loop() -> None:
    idle_timeout = _env_int("SUBAGENT_IDLE_TIMEOUT", DEFAULT_IDLE_TIMEOUT)
    max_runtime = _env_int("SUBAGENT_MAX_RUNTIME", DEFAULT_MAX_RUNTIME)
    reap_after_hours = _env_int("SUBAGENT_REAP_AFTER_HOURS", 2)
    reap_interval = _env_int("SUBAGENT_REAP_INTERVAL", 600)
    logger.info(
        "subagent watchdog started: idle_timeout=%ss max_runtime=%ss reap_after=%sh reap_interval=%ss",
        idle_timeout, max_runtime, reap_after_hours, reap_interval,
    )
    last_reap = time.time()
    # 启动即收口一次：把上次进程遗留的 running 尽快收敛，别等一个周期
    if reap_after_hours > 0:
        await _reap_once(reap_after_hours)
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)
        try:
            await watchdog_tick(idle_timeout=idle_timeout, max_runtime=max_runtime)
            if reap_after_hours > 0 and reap_interval > 0 and time.time() - last_reap >= reap_interval:
                last_reap = time.time()
                await _reap_once(reap_after_hours)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 看门狗自己不能把循环搞死
            logger.warning("subagent watchdog tick failed: %s", str(exc)[:200])


def start_watchdog() -> None:
    global _WATCHDOG
    if _WATCHDOG is None or _WATCHDOG.done():
        _WATCHDOG = asyncio.create_task(watchdog_loop())


# ── 跨实例取消：订阅网关广播的 subagent:cancel ─────────────────────


async def subscribe_cancel_channel() -> None:
    """订阅取消广播（Go 的 POST /v1/subagent/runs/{id}/cancel 发布）。"""
    try:
        from app.redis_client import get_redis
        from app.redis_keys import rkey

        redis = await get_redis()
    except Exception as exc:  # noqa: BLE001
        logger.warning("subagent cancel subscriber disabled: %s", str(exc)[:160])
        return
    if redis is None:
        logger.warning("subagent cancel subscriber disabled: no redis client")
        return

    channel = rkey("subagent:cancel")
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)
    logger.info("subagent cancel subscriber started: %s", channel)
    try:
        async for message in pubsub.listen():
            if not isinstance(message, dict) or message.get("type") != "message":
                continue
            try:
                data = json.loads(message.get("data") or "{}")
            except Exception:  # noqa: BLE001 - 非法载荷直接忽略
                continue
            run_id = str(data.get("run_id") or "")
            session_id = str(data.get("session_id") or "")
            reason = _EXTERNAL_REASONS.get(str(data.get("reason") or ""), REASON_USER)
            if run_id and await _cancel_and_ack(redis, run_id, reason):
                continue
            if session_id and cancel_session(session_id, reason):
                continue
            logger.info("subagent cancel ignored (not held by this instance): run=%s session=%s",
                        run_id or "-", session_id or "-")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 订阅断开不该影响引擎
        logger.warning("subagent cancel subscriber stopped: %s", str(exc)[:200])
    finally:
        try:
            await pubsub.close()
        except Exception:  # noqa: BLE001
            pass


def start_cancel_subscriber() -> None:
    global _SUBSCRIBER
    if _SUBSCRIBER is None or _SUBSCRIBER.done():
        _SUBSCRIBER = asyncio.create_task(subscribe_cancel_channel())


async def _cancel_and_ack(redis: Any, run_id: str, reason: str) -> bool:
    """命中本地注册表 → 取消 + 写回执；返回是否真的发出了取消。

    回执（``subagent:cancel:ack:{run_id}``）是网关区分"真有人认领"与"广播无人应答"的
    **唯一依据**：没有它，实例已经重启/退出的场景下取消只能返回"已受理"——那正是
    "点了停止却一直转圈、DB 永远停在 running"的成因（见 P4 设计）。
    """
    if not cancel(run_id, reason):
        return False
    from app.subagent import affinity

    await affinity.ack_cancel(redis, run_id)
    return True


async def stop() -> None:
    """关机时取消后台任务（看门狗/订阅），并**注销持有的作业归属**（P4-3）。

    为什么要在关机时注销：进程退出后这些 run 的收尾代码不会再执行，留着归属会让网关
    以为"还有人在跑"，把取消路由到一个正在退出的实例；注销后网关能明确判定
    "无人认领"，把作业收敛为 ``lost``，而不是让用户对着 running 一直转圈。
    """
    for task in (_WATCHDOG, _SUBSCRIBER):
        if task is not None and not task.done():
            task.cancel()
    owned = list(_RUNS)
    if owned:
        from app.subagent import affinity

        try:
            await affinity.release_owners(owned)
        except Exception as exc:  # noqa: BLE001 - 注销失败不该阻断关机
            logger.warning("subagent affinity release on shutdown failed: %s", str(exc)[:160])
