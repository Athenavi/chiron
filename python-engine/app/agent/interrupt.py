"""Agent 运行中断（**真取消**）—— ACP `session/cancel` 的引擎侧前置。

**为什么不是"客户端停止读 SSE"**：那样引擎仍在跑、仍在烧 token、仍可能写文件 —— 用户以为停了
实际没停。真取消要求**引擎自己**在轮次边界停下来。

**为什么只在轮次边界检查**：轮次之间是唯一"没有半成品"的点。工具执行到一半被打断会留下说不清
的现场（与 C1 的 `pending_tool_calls` 同一类问题）。代价是取消最多延迟一轮 —— 这是刻意的取舍：
宁可慢一点，也不要一个状态说不清的工作区。

**跨副本**：中断请求可能落在与 run 不同的引擎副本上（网关按 session 路由，不保证同副本），
所以信号要落 Redis；同时保留一个进程内标志作为**快路径**（同副本时零网络往返）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

#: 信号存活时间（秒）。够一轮跑完即可；过期自动消失，避免"上一次的取消"误伤下一次会话。
INTERRUPT_TTL_SECONDS = 300

#: 进程内快路径：session_id → 置位时间。**同副本**的 run 不必为每次检查付一次 Redis 往返。
_LOCAL: dict[str, float] = {}


def interrupt_key(session_id: str) -> str:
    """Redis 键名（与 `run_registry.run_key` 同一命名约定）。"""
    from app.redis_keys import rkey

    return rkey("engine:interrupt:") + session_id


async def _resolve_redis(redis: Any = None) -> Any:
    if redis is not None:
        return redis
    try:
        from app.redis_client import get_redis

        return await get_redis()
    except Exception as exc:  # noqa: BLE001 — Redis 不可用时仍有本地快路径
        logger.debug("interrupt: redis unavailable (%s), local-only", exc)
        return None


def request_interrupt_local(session_id: str) -> None:
    """只置本地标志（同副本快速路径 / 测试用）。"""
    if session_id:
        _LOCAL[session_id] = time.time()


async def request_interrupt(session_id: str, *, redis: Any = None) -> bool:
    """置位中断信号（本地 + Redis）。

    返回 **Redis 是否置位成功**。Redis 失败**不影响**本地置位 —— 同副本的 run 照样能停下来；
    只有"run 在别的副本"这一种情况会因此失效，而那种情况下请求方本来就拿不到本地的 run。
    """
    if not session_id:
        return False
    request_interrupt_local(session_id)
    client = await _resolve_redis(redis)
    if client is None:
        return False
    try:
        await client.setex(interrupt_key(session_id), INTERRUPT_TTL_SECONDS, "1")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("interrupt: failed to set redis flag: %s", exc)
        return False


async def is_interrupted(session_id: str, *, redis: Any = None) -> bool:
    """查中断信号：**先本地**（免费），未置位才查 Redis（跨副本）。

    顺序有意如此：绝大多数取消来自同一副本（用户在同一个连接上点了取消），本地命中时不必付
    网络往返；跨副本是少数情况，那时才查 Redis。
    """
    if not session_id:
        return False
    if session_id in _LOCAL:
        return True
    client = await _resolve_redis(redis)
    if client is None:
        return False
    try:
        return bool(await client.get(interrupt_key(session_id)))
    except Exception as exc:  # noqa: BLE001 — 查不到就当没被取消，不阻断 run
        logger.warning("interrupt: failed to read redis flag: %s", exc)
        return False


async def clear_interrupt(session_id: str, *, redis: Any = None) -> None:
    """清除信号（run 收尾时调用）。

    **不清会误伤下一次会话** —— 同一个 `session_id` 的下一次 run 会在第一轮就"被取消"，
    而用户会以为自己什么都没做。这是本模块最容易漏的一步。
    """
    if not session_id:
        return
    _LOCAL.pop(session_id, None)
    client = await _resolve_redis(redis)
    if client is None:
        return
    try:
        await client.delete(interrupt_key(session_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("interrupt: failed to clear redis flag: %s", exc)
