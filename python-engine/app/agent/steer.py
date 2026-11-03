"""运行中指导（steer）—— 在轮次边界把用户的追加指令注入正在跑的会话。

与 `interrupt.py` 同一个通道形状（Redis 信号 + 进程内快路径 + 归属校验在调用端），
区别只在**读到的是文本而非停止信号**：run 继续，只是多了一条用户约束。

只在轮次边界注入：工具执行到一半插消息会让 `tool_calls` 与 tool 结果对不上
（与 interrupt 只在轮次边界检查是同一个理由）。跨副本靠 Redis list，本副本走本地队列。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 队列存活时间（秒）。够一轮跑完即可，过期自动消失以免误伤下一次会话。
STEER_TTL_SECONDS = 300
#: 限流：每个会话每分钟最多接受这么多条，超出**报错**而不是静默丢弃。
MAX_PER_MINUTE = 6
#: 单条上限（字符）。指令是短句，超长的应该走正常提交。
MAX_CHARS = 2000
#: 队列长度上限，防止一条失控的 run 把 Redis 撑爆。
MAX_QUEUE = 20

#: 进程内快路径：session_id → [(置入时刻, 文本)]，同副本时零网络往返。
_LOCAL: dict[str, list[tuple[float, str]]] = {}


def steer_key(session_id: str) -> str:
    """Redis 键名（与 `interrupt_key` 同一命名约定）。"""
    from app.redis_keys import rkey

    return rkey("engine:steer:") + session_id


async def _resolve_redis(redis: Any = None) -> Any:
    if redis is not None:
        return redis
    try:
        from app.redis_client import get_redis

        return await get_redis()
    except Exception as exc:  # noqa: BLE001 — 无 Redis 时仍有本地队列
        logger.debug("steer: redis unavailable (%s), local-only", exc)
        return None


def _validate(text: str) -> str | None:
    """返回拒绝原因；空串表示通过。"""
    if not text.strip():
        return "text is required"
    if len(text) > MAX_CHARS:
        return f"text too long (>{MAX_CHARS} chars)"
    return None


async def push_steer(session_id: str, text: str, *, source: str = "user",
                     redis: Any = None) -> tuple[bool, str]:
    """入队一条指导；返回 (是否成功, 失败原因)。

    失败必须让调用方看见：静默丢弃会让用户以为话已经带到了正在跑的 Agent 那里。
    """
    if not session_id:
        return False, "session_id is required"
    reason = _validate(text)
    if reason:
        return False, reason

    client = await _resolve_redis(redis)

    # 限流按会话计数：跨副本也要一致，所以优先用 Redis 的计数窗口。
    if client is not None:
        try:
            from app.redis_keys import rkey

            rate_key = rkey("engine:steer:rate:") + session_id
            count = await client.incr(rate_key)
            if count == 1:
                await client.expire(rate_key, 60)
            if count > MAX_PER_MINUTE:
                return False, f"rate limited ({MAX_PER_MINUTE}/min)"
        except Exception as exc:  # noqa: BLE001 — 计数失败不阻断投递，本地队列仍可用
            logger.warning("steer: rate counter failed: %s", exc)
    else:
        logger.warning("steer: redis unavailable, message queued locally only")

    # 本地快路径与 Redis 都要写：同副本读本地零往返，跨副本读 Redis。
    _LOCAL.setdefault(session_id, []).append((_now(), text))
    _LOCAL[session_id] = _LOCAL[session_id][-MAX_QUEUE:]
    if client is not None:
        try:
            key = steer_key(session_id)
            await client.lpush(key, text)
            await client.ltrim(key, 0, MAX_QUEUE - 1)
            await client.expire(key, STEER_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("steer: redis push failed (local copy kept): %s", exc)
    return True, ""


async def drain_steer(session_id: str, *, redis: Any = None) -> list[str]:
    """取走该会话待注入的指导，按投递顺序返回。

    先本地后 Redis：本地命中通常是同副本（用户在同一个连接上），往 Redis 再查一遍
    会把同一条文本取到两次 —— 所以**两者取并集去重**没有意义，改为"本地非空就只信本地"。
    """
    if not session_id:
        return []
    local = _LOCAL.pop(session_id, [])
    if local:
        return [text for _, text in local]

    client = await _resolve_redis(redis)
    if client is None:
        return []
    try:
        key = steer_key(session_id)
        # lrange + delete 不是原子的：先后两次读之间可能有新消息进来，被一并删掉。
        # 用 RENAME 把"取走"收敛成单次原子操作；目标键必须唯一，否则多副本同时
        # drain 会互相覆盖（后到的 rename 覆盖前者，前者的内容就丢了）。
        import uuid

        take_key = f"{key}:drain:{uuid.uuid4().hex[:12]}"
        try:
            await client.rename(key, take_key)
        except Exception:  # noqa: BLE001 — 键不存在 = 没有待注入内容
            return []
        raw = await client.lrange(take_key, 0, -1) or []
        await client.delete(take_key)
        out = []
        for item in raw:
            text = item.decode() if isinstance(item, bytes) else str(item)
            out.append(text)
        out.reverse()  # lpush 是头插，取出后反序才是投递顺序
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("steer: drain failed: %s", exc)
        return []


def clear_steer_local(session_id: str) -> None:
    """清理本地队列（run 收尾时调用，避免残留误伤下一次会话）。"""
    if session_id:
        _LOCAL.pop(session_id, None)


def _now() -> float:
    import time

    return time.time()
