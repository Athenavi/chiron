# 死信队列管理
from __future__ import annotations

import logging
from typing import Any

import redis.asyncio as aioredis

# 统一键前缀（与 Go 网关 RedisKey 语义一致，多环境隔离）
from app.redis_keys import rkey

logger = logging.getLogger(__name__)

# 主任务流与死信流（消费组名 engine-workers 挂在流上，无需前缀）
TASK_STREAM = rkey("engine:tasks")
DLQ_STREAM = rkey("engine:tasks:dlq")


class DeadLetterQueue:
    """死信队列管理 — 查看、重试、清理"""

    def __init__(self, redis: aioredis.Redis):
        self._redis = redis

    async def list(self, count: int = 50) -> list[dict[str, Any]]:
        """列出死信消息"""
        results = await self._redis.xrevrange(DLQ_STREAM, count=count)
        messages = []
        for stream_id, fields in results or []:
            msg: dict[Any, Any] = {}
            for k, v in (fields or {}).items():
                key = k.decode() if isinstance(k, bytes) else k
                val = v.decode() if isinstance(v, bytes) else v
                msg[key] = val
            msg["stream_id"] = stream_id
            messages.append(msg)
        return messages

    async def retry(self, stream_id: str) -> bool:
        """将死信消息重新入队"""
        # 读取消息
        results = await self._redis.xrange(DLQ_STREAM, min=stream_id, max=stream_id)
        if not results:
            return False

        _, fields = results[0]
        # 重建消息
        message: dict[Any, Any] = {}
        for k, v in (fields or {}).items():
            key = k.decode() if isinstance(k, bytes) else k
            val = v.decode() if isinstance(v, bytes) else v
            if key not in ("error", "stream_id"):
                message[key] = val
        message["retry_count"] = "0"

        # 重新入队（MAXLEN 限界，防长期运行下流无界增长）
        await self._redis.xadd(TASK_STREAM, message, maxlen=100000)
        # 从 DLQ 删除
        await self._redis.xdel(DLQ_STREAM, stream_id)
        logger.info("DLQ message requeued: %s", stream_id)
        return True

    async def retry_all(self) -> int:
        """重试所有死信消息"""
        messages = await self.list(count=1000)
        count = 0
        for msg in messages:
            if await self.retry(msg["stream_id"]):
                count += 1
        return count

    async def clear(self) -> int:
        """清空死信队列"""
        messages = await self.list(count=10000)
        count = 0
        for msg in messages:
            await self._redis.xdel(DLQ_STREAM, msg["stream_id"])
            count += 1
        return count

    async def depth(self) -> int:
        """死信队列深度"""
        try:
            info = await self._redis.xinfo_stream(DLQ_STREAM)
            return int(info.get("length", 0) or 0)
        except Exception:
            return 0
