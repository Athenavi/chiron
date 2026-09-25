# 租户级限流 — Redis 滑动窗口计数器
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import redis.asyncio as aioredis

from app.redis_keys import rkey

logger = logging.getLogger(__name__)


class TenantRateLimiter:
    """
    算法: Redis 有序集合滑动窗口

    key: "ratelimit:{tenant_id}:{window_type}"
    每次请求 ZADD score=timestamp, member=unique_id
    ZREMRANGEBYSCORE 清理窗口外的记录
    ZCARD 计算当前窗口内请求数
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        requests_per_minute: int = 60,
        requests_per_second: int = 10,
    ):
        self._redis = redis
        self.rpm = requests_per_minute
        self.rps = requests_per_second

    async def allow(self, tenant_id: str) -> bool:
        """检查是否允许请求通过"""
        if not tenant_id:
            return True

        now = time.time()

        # 检查每秒限流
        if not await self._check_window(rkey(f"ratelimit:{tenant_id}:s"), now, 1.0, self.rps):
            logger.warning(
                "Rate limit exceeded: tenant=%s (rps=%d)", tenant_id, self.rps
            )
            return False

        # 检查每分钟限流
        if not await self._check_window(
            rkey(f"ratelimit:{tenant_id}:m"), now, 60.0, self.rpm
        ):
            logger.warning(
                "Rate limit exceeded: tenant=%s (rpm=%d)", tenant_id, self.rpm
            )
            return False

        return True

    async def _check_window(
        self, key: str, now: float, window_seconds: float, max_requests: int
    ) -> bool:
        """滑动窗口检查 + 计数（原子操作，使用 Lua 脚本避免竞态）

        将清理、计数、判断、添加全部封装在 Redis Lua 脚本中一次 Eval 执行，
        杜绝并发请求同时通过 ZCARD 检查导致超限。
        """
        window_start = now - window_seconds
        member = f"{now}:{uuid.uuid4().hex[:16]}"

        lua = """
        local key = KEYS[1]
        local window_start = ARGV[1]
        local now = ARGV[2]
        local member = ARGV[3]
        local max_requests = tonumber(ARGV[4])
        local window_seconds = tonumber(ARGV[5])

        redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)
        local count = redis.call('ZCARD', key)
        if count >= max_requests then
            return {0, count}
        end
        redis.call('ZADD', key, now, member)
        redis.call('EXPIRE', key, window_seconds + 1)
        return {1, count}
        """
        ok, count = await self._redis.eval(
            lua, 1, key, str(window_start), str(now), member, str(max_requests), str(int(window_seconds))
        )
        if not ok:
            logger.warning(
                "Rate limit exceeded: key=%s (count=%d/%d)", key, count, max_requests
            )
            return False
        return True

    async def get_remaining(self, tenant_id: str) -> dict[str, Any]:
        """返回剩余额度"""
        now = time.time()
        s_key = rkey(f"ratelimit:{tenant_id}:s")
        m_key = rkey(f"ratelimit:{tenant_id}:m")

        s_count = await self._redis.zcount(s_key, now - 1.0, now)
        m_count = await self._redis.zcount(m_key, now - 60.0, now)

        return {
            "rps_remaining": max(0, self.rps - s_count),
            "rpm_remaining": max(0, self.rpm - m_count),
        }


class LocalTenantRateLimiter:
    """进程内滑动窗口限流——Redis 不可用时的兑底实现（单实例语义）。

    接口与 TenantRateLimiter 一致（allow/get_remaining），避免 Redis 故障时
    引擎完全裸奔（历史行为 limiter=None）。多引擎实例请启用 Redis 以使用
    分布式 TenantRateLimiter。
    """

    def __init__(
        self,
        requests_per_minute: int = 60,
        requests_per_second: int = 10,
    ):
        self.rpm = requests_per_minute
        self.rps = requests_per_second
        self._lock = asyncio.Lock()
        self._seconds: dict[str, list[float]] = {}
        self._minutes: dict[str, list[float]] = {}

    @staticmethod
    def _prune(bucket: list[float], now: float, window: float) -> int:
        bucket[:] = [t for t in bucket if t > now - window]
        return len(bucket)

    async def allow(self, tenant_id: str) -> bool:
        """检查是否允许请求通过（进程内，单实例语义）"""
        if not tenant_id:
            return True
        now = time.time()
        async with self._lock:
            s = self._seconds.setdefault(tenant_id, [])
            if self._prune(s, now, 1.0) >= self.rps:
                return False
            m = self._minutes.setdefault(tenant_id, [])
            if self._prune(m, now, 60.0) >= self.rpm:
                return False
            s.append(now)
            m.append(now)
            return True

    async def get_remaining(self, tenant_id: str) -> dict[str, Any]:
        """返回剩余额度"""
        now = time.time()
        async with self._lock:
            s = self._seconds.setdefault(tenant_id, [])
            m = self._minutes.setdefault(tenant_id, [])
            return {
                "rps_remaining": max(0, self.rps - self._prune(s, now, 1.0)),
                "rpm_remaining": max(0, self.rpm - self._prune(m, now, 60.0)),
            }
