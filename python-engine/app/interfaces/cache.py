"""
CacheClient Protocol — 对标 Go 的 db.RedisClient 接口
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol


class CacheClient(Protocol):
    """缓存客户端接口"""

    async def get(self, key: str) -> str | None:
        """获取缓存值"""
        ...

    async def set(
        self,
        key: str,
        value: str,
        ttl: int = 0,
    ) -> None:
        """设置缓存值，ttl=0 表示不设置过期时间"""
        ...

    async def delete(self, key: str) -> None:
        """删除缓存"""
        ...

    async def rpush(self, key: str, value: str) -> None:
        """向列表右侧推入值"""
        ...

    async def lrange(
        self,
        key: str,
        start: int,
        stop: int,
    ) -> list[str]:
        """获取列表范围内的值"""
        ...

    async def expire(self, key: str, ttl: int) -> None:
        """设置过期时间"""
        ...

    async def scan_iter(self, match: str) -> AsyncIterator[str]:
        """迭代匹配的键"""
        ...

    async def ping(self) -> bool:
        """测试连接"""
        ...

    async def close(self) -> None:
        """关闭连接"""
        ...
