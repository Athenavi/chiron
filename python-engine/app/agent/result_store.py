"""工具结果落盘 —— 大结果只把「摘要 + 引用」放进上下文，细节按需取回。

与子 Agent 的 ``read_subagent_result`` 同构：上下文里放 ``result_ref``，模型需要细节时用
``read_tool_result(result_ref, offset, limit)`` 取回 —— 用户与模型都不必学两套。

**为什么用 Redis 而不是 DB**：主 Agent 的工具原始输出生命周期与会话一致，绝大部分没有长期
留存价值；DB 承载的是"要留下的审计记录"（`subagent_runs` / `turns`），不该被工具原文灌满。
TTL 到期自动清理，也省掉一套归档逻辑。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: 结果原文保存时长（秒）—— 与会话的自然生命周期对齐
RESULT_TTL_SECONDS = 2 * 60 * 60


def _key(session_id: str, ref: str) -> str:
    from app.redis_keys import rkey

    return rkey(f"tool_result:{session_id or 'nosession'}:{ref}")


async def store(session_id: str, ref: str, text: str) -> bool:
    """保存结果原文。返回是否成功 —— 失败只意味着"细节取不回"，不影响本轮对话。"""
    try:
        from app.redis_client import get_redis

        redis = await get_redis()
        if redis is None:
            return False
        await redis.set(_key(session_id, ref), text, ex=RESULT_TTL_SECONDS)
        return True
    except Exception as e:  # noqa: BLE001 - 落盘失败不该影响工具结果回灌
        logger.warning("tool result store failed (ref=%s): %s", ref, e)
        return False


async def load(session_id: str, ref: str, *, offset: int = 0, limit: int = 0) -> str | None:
    """取回结果原文（可分段）；不存在 / 已过期返回 None。"""
    try:
        from app.redis_client import get_redis

        redis = await get_redis()
        if redis is None:
            return None
        raw = await redis.get(_key(session_id, ref))
    except Exception as e:  # noqa: BLE001
        logger.warning("tool result load failed (ref=%s): %s", ref, e)
        return None
    if raw is None:
        return None
    text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
    if offset or limit:
        end = offset + limit if limit else len(text)
        return text[offset:end]
    return text
