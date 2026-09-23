"""Redis 连接（进程内单例，线程安全初始化）。

**只有一条路径**：直接用 ``redis.asyncio`` 从 ``settings.redis_url`` 建连。

历史上这里还有一条 "unified" 分支，把操作转发到经 Go 网关的 ``UnifiedRedisClient``。
那个适配层（``_UnifiedRedisWrapper``）只实现了 get / set / delete / ping，而
``xadd`` / ``exists`` / ``expire`` / ``incr`` 要么缺失、要么直接 raise NotImplementedError ——
于是 ``USE_UNIFIED_REDIS_CLIENT=true`` 时，工作流入队（用 ``xadd``）会**永远失败**；
而失败又被上层当成"队列暂不可达"，故障因此被伪装成了环境问题。

删掉这条分支的理由很简单：**一个"实现了一半的接口"比没有这个接口更危险** ——
调用方看到的是 ``redis.asyncio.Redis`` 的类型，实际拿到的是会抛异常的子集，
而类型注解和代码阅读都不会暴露这件事。统一走 aioredis 后，返回值和它的类型声明一致。

Usage:
    from app.redis_client import get_redis

    r = await get_redis()
    await r.set("key", "value")
    val = await r.get("key")
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

_redis_instance: Optional[aioredis.Redis] = None
_redis_lock = asyncio.Lock()  # P0-2: Thread-safe initialization


async def get_redis() -> aioredis.Redis:
    """Get the Redis connection with thread-safe lazy initialization."""
    global _redis_instance

    # Double-checked locking pattern for thread safety
    if _redis_instance is None:
        async with _redis_lock:
            # Re-check after acquiring lock
            if _redis_instance is None:
                if not settings.redis_url:
                    raise RuntimeError("Redis URL not configured")

                # P0-2: Add timeout parameters to prevent hanging connections
                redis_url_with_timeout = settings.redis_url
                if "?" not in redis_url_with_timeout:
                    redis_url_with_timeout += "?socket_timeout=5&socket_connect_timeout=3&retry_on_timeout=true"
                else:
                    # Ensure timeout params are present
                    if "socket_timeout" not in redis_url_with_timeout:
                        redis_url_with_timeout += "&socket_timeout=5"
                    if "socket_connect_timeout" not in redis_url_with_timeout:
                        redis_url_with_timeout += "&socket_connect_timeout=3"

                _redis_instance = aioredis.from_url(
                    redis_url_with_timeout,
                    decode_responses=True,
                    max_connections=settings.redis_max_connections,
                    socket_keepalive=True,  # Enable TCP keepalive
                )
                logger.info(
                    "Redis connected with timeouts: %s (pool=%d)",
                    settings.redis_url,
                    settings.redis_max_connections,
                )

    return _redis_instance
