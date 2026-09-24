"""
Session Store — Redis-backed 会话消息缓存（支持降级到内存）

职责：
1. 按 session_id 维护累积消息列表（system + 历史 + 工具调用）
2. 跨请求保持前缀稳定（append-only），让 DeepSeek prefix cache 命中
3. 多实例共享 Redis 后端，实现无状态扩展
4. Redis 不可用时降级到内存 LRU

使用方式：
    store = SessionStore(redis_client=redis_instance)
    messages = await store.get_or_init(session_id, history_from_go)
    await store.append(session_id, new_messages)
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict

from app.middleware.privacy_middleware import is_no_retention
from app.redis_keys import rkey

logger = logging.getLogger(__name__)

REDIS_KEY_PREFIX = rkey("session_cache:")
REDIS_TTL_SECONDS = 7200  # 2 小时
# 降级后 Redis 重探间隔：Redis 抖动恢复后自愈回共享后端
REDIS_RETRY_AFTER_SECS = 5.0


class SessionStore:
    """会话消息缓存，优先使用 Redis，不可用时降级到内存 LRU"""

    def __init__(self, redis_client=None, max_sessions: int = 200):
        self._redis = redis_client
        self._redis_enabled = redis_client is not None
        # 降级后重探窗口：Redis 抖动恢复后自动回到共享后端，避免实例永久“假降级”
        self._redis_retry_at = 0.0
        self._max_sessions = max_sessions
        # 内存降级后端（仅 Redis 不可用时使用）
        self._local: OrderedDict[str, list[dict]] = OrderedDict()

    def _redis_usable(self) -> bool:
        """是否尝试 Redis：正常启用，或降级后已到重探窗口（重开一次，失败会再次禁用）。"""
        if self._redis_enabled:
            return True
        if time.monotonic() >= self._redis_retry_at:
            self._redis_enabled = True
            logger.info("SessionStore re-probing Redis after degraded window")
        return self._redis_enabled

    def _disable_redis(self) -> None:
        """降级到本地，并安排一段时间后的重探（当前实例级标记，不修改共享 self._redis）。"""
        self._redis_enabled = False
        self._redis_retry_at = time.monotonic() + REDIS_RETRY_AFTER_SECS

    # ── 公共接口 ──

    async def get_or_init(self, session_id: str, history: list[dict]) -> list[dict]:
        if not session_id:
            return list(history)

        # 隐私模式：no_retention 时跳过 Redis 持久层，降级到内存（不落盘）
        if is_no_retention():
            logger.debug(
                "Privacy no_retention: skip Redis session persistence for %s",
                session_id,
            )
            return self._local_get_or_init(session_id, history)

        # 尝试 Redis 后端（降级后到窗口自动重探一次）
        if self._redis_usable():
            try:
                return await self._redis_get_or_init(session_id, history)
            except Exception as e:
                logger.warning("Redis session get failed, fallback to local: %s", e)
                self._disable_redis()

        # 内存降级
        return self._local_get_or_init(session_id, history)

    async def append(self, session_id: str, messages: list[dict]) -> None:
        if not session_id:
            return

        # 隐私模式：no_retention 时跳过 Redis 持久层，仅保留内存态（不落盘）
        if is_no_retention():
            logger.debug(
                "Privacy no_retention: skip Redis session persistence for %s",
                session_id,
            )
            self._local_set(session_id, messages)
            return

        if self._redis_usable():
            try:
                await self._redis_set(session_id, messages)
                return
            except Exception:
                self._disable_redis()

        self._local_set(session_id, messages)

    async def get(self, session_id: str) -> list[dict] | None:
        if self._redis_usable():
            try:
                return await self._redis_get(session_id)
            except Exception:
                self._disable_redis()
        return self._local.get(session_id)

    async def remove(self, session_id: str) -> None:
        if self._redis_usable():
            try:
                await self._redis.delete(REDIS_KEY_PREFIX + session_id)
            except Exception:
                pass
        self._local.pop(session_id, None)

    async def clear(self) -> None:
        self._local.clear()
        if self._redis_usable():
            try:
                cursor = 0
                while True:
                    cursor, keys = await self._redis.scan(
                        cursor, match=REDIS_KEY_PREFIX + "*", count=100
                    )
                    if keys:
                        await self._redis.delete(*keys)
                    if cursor == 0:
                        break
            except Exception:
                pass

    @property
    def size(self) -> int:
        return len(self._local)

    # ── Redis 后端 ──

    async def _redis_get_or_init(
        self, session_id: str, history: list[dict]
    ) -> list[dict]:
        data = await self._redis.get(REDIS_KEY_PREFIX + session_id)
        if data:
            # 延长 TTL
            await self._redis.expire(REDIS_KEY_PREFIX + session_id, REDIS_TTL_SECONDS)
            result = json.loads(data)
            logger.debug("Redis cache HIT: %s (%d messages)", session_id, len(result))
            return result

        logger.info(
            "Redis cache MISS: %s (init from history: %d msgs)",
            session_id,
            len(history),
        )
        messages = list(history)
        await self._redis_set(session_id, messages)
        return messages

    async def _redis_get(self, session_id: str) -> list[dict] | None:
        data = await self._redis.get(REDIS_KEY_PREFIX + session_id)
        if data:
            return json.loads(data)
        return None

    async def _redis_set(self, session_id: str, messages: list[dict]) -> None:
        await self._redis.setex(
            REDIS_KEY_PREFIX + session_id,
            REDIS_TTL_SECONDS,
            json.dumps(messages, ensure_ascii=False, default=str),
        )

    # ── 内存降级后端 ──

    def _local_get_or_init(self, session_id: str, history: list[dict]) -> list[dict]:
        if session_id in self._local:
            self._local.move_to_end(session_id)
            cached = self._local[session_id]
            logger.debug("Local cache HIT: %s (%d messages)", session_id, len(cached))
            # 返回拷贝：调用方（runtime）会 append/修改返回列表，
            # 若直接返回缓存引用会污染共享状态，导致并发/多轮上下文错乱
            return list(cached)

        logger.info(
            "Local cache MISS: %s (init from history: %d msgs)",
            session_id,
            len(history),
        )
        messages = list(history)
        self._local[session_id] = messages
        self._evict_if_needed()
        # S 修复：与 HIT 一致返回拷贝，避免调用方修改返回列表污染缓存
        return list(messages)

    def _local_set(self, session_id: str, messages: list[dict]) -> None:
        self._local[session_id] = messages
        self._local.move_to_end(session_id)
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        while len(self._local) > self._max_sessions:
            evicted_id, evicted = self._local.popitem(last=False)
            logger.info("Local cache EVICT: %s (%d messages)", evicted_id, len(evicted))
