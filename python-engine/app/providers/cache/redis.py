"""
Redis Cache Client — 连接池化实现，对接 CacheClient Protocol
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import cast

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


class RedisCacheClient:
    """Redis 连接池化客户端 — 实现 CacheClient Protocol"""

    def __init__(
        self,
        url: str,
        max_connections: int = 20,
        decode_responses: bool = True,
    ):
        self._url = url
        self._max_connections = max_connections
        self._decode_responses = decode_responses
        self._pool: aioredis.Redis | None = None

    async def _get_pool(self) -> aioredis.Redis:
        """获取或创建连接池 - 使用统一 Redis 客户端"""
        if self._pool is None:
            from app.redis_client import get_redis

            self._pool = await get_redis()
            await self._pool.ping()
            logger.info(
                "Redis pool connected via unified client (pool=%d)",
                self._max_connections,
            )
        return self._pool

    async def get(self, key: str) -> str | None:
        """获取缓存值"""
        pool = await self._get_pool()
        # decode_responses=True（见 app/redis_client.get_redis）时响应必然是 str，
        # 而 redis-py 的类型把两种模式合成 bytes | str，故在此收敛。
        return cast("str | None", await pool.get(key))

    async def set(
        self,
        key: str,
        value: str,
        ttl: int = 0,
    ) -> None:
        """设置缓存值"""
        pool = await self._get_pool()
        if ttl > 0:
            await pool.set(key, value, ex=ttl)
        else:
            await pool.set(key, value)

    async def delete(self, key: str) -> None:
        """删除缓存"""
        pool = await self._get_pool()
        await pool.delete(key)

    async def rpush(self, key: str, value: str) -> None:
        """向列表右侧推入值"""
        pool = await self._get_pool()
        await pool.rpush(key, value)

    async def lrange(
        self,
        key: str,
        start: int,
        stop: int,
    ) -> list[str]:
        """获取列表范围内的值"""
        pool = await self._get_pool()
        return cast("list[str]", await pool.lrange(key, start, stop))

    async def expire(self, key: str, ttl: int) -> None:
        """设置过期时间"""
        pool = await self._get_pool()
        await pool.expire(key, ttl)

    async def scan_iter(self, match: str) -> AsyncIterator[str]:
        """迭代匹配的键"""
        pool = await self._get_pool()
        async for key in pool.scan_iter(match=match):
            yield key

    async def ping(self) -> bool:
        """测试连接"""
        try:
            pool = await self._get_pool()
            return await pool.ping()
        except Exception as e:
            logger.error("Redis ping failed: %s", e)
            return False

    async def close(self) -> None:
        """关闭连接"""
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("Redis pool closed")
