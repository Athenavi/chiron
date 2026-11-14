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

#: 结果原文保存时长（秒）。
#:
#: 旧值 2 小时（`2 * 60 * 60`）与"长任务"这个前提相冲突：一个跨夜或跑上几小时的任务，
#: 中途压缩掉的工具原文在下一次续跑时就已经过期 —— 模型手里只剩 `result_ref`，
#: 取回却拿到 `None`，表现为"引用还在、内容没了"，比一开始就没存更让人困惑。
#:
#: 取 24 小时，与 `job_runner.JOB_RESULT_TTL` 对齐（后台任务结果也是这个量级）：
#: 长任务通常在同一工作日内完成，跨天则应该走 `read_archive` 的 **PG 长期通道**
#: （`messages` 持久表，见 `app/tools/archive.py`）—— 这里不需要再存更久。
RESULT_TTL_SECONDS = 24 * 60 * 60


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
