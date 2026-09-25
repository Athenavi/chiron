"""
SSE 生产者 — 写入 Redis Stream（零丢失保证）
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as aioredis

from app.redis_keys import rkey

logger = logging.getLogger(__name__)


class SSEProducer:
    """
    SSE 生产者 — 将事件写入 Redis Stream

    Go 侧订阅 Redis Stream 并转发到前端
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        maxlen: int = 10000,
    ):
        self._redis = redis
        self._maxlen = maxlen

    async def publish(self, task_id: str, event: dict[str, Any]) -> bool:
        """
        发布 SSE 事件到 Redis Stream

        Returns:
            True 表示发布成功，False 表示发布失败（调用方应据此决定是否重试或终止）
        """
        stream_key = rkey(f"sse:{task_id}")

        try:
            await self._redis.xadd(
                stream_key,
                {"data": json.dumps(event, ensure_ascii=False)},
                maxlen=self._maxlen,
            )
            logger.debug("Published SSE event to %s: %s", stream_key, event.get("type"))
            return True
        except Exception as e:
            logger.error("Failed to publish SSE event: %s", e)
            return False

    async def publish_text(self, task_id: str, content: str) -> None:
        """发布文本事件"""
        ok = await self.publish(
            task_id,
            {
                "type": "text",
                "content": content,
            },
        )
        if not ok:
            logger.warning("publish_text failed for task %s", task_id)

    async def publish_tool_call(
        self, task_id: str, tool_id: str, name: str, arguments: str
    ) -> None:
        """发布工具调用事件"""
        ok = await self.publish(
            task_id,
            {
                "type": "tool_call",
                "id": tool_id,
                "name": name,
                "arguments": arguments,
            },
        )
        if not ok:
            logger.warning("publish_tool_call failed for task %s", task_id)

    async def publish_tool_result(
        self, task_id: str, tool_id: str, result: dict[str, Any]
    ) -> None:
        """发布工具结果事件"""
        ok = await self.publish(
            task_id,
            {
                "type": "tool_result",
                "id": tool_id,
                "result": result,
            },
        )
        if not ok:
            logger.warning("publish_tool_result failed for task %s", task_id)

    async def publish_done(self, task_id: str, session_id: str = "") -> None:
        """发布完成事件"""
        ok = await self.publish(
            task_id,
            {
                "type": "done",
                "session_id": session_id,
            },
        )
        if not ok:
            logger.warning("publish_done failed for task %s", task_id)

    async def publish_error(self, task_id: str, error: str) -> None:
        """发布错误事件"""
        ok = await self.publish(
            task_id,
            {
                "type": "error",
                "message": error,
            },
        )
        if not ok:
            logger.warning("publish_error failed for task %s", task_id)

    async def publish_usage(
        self, task_id: str, input_tokens: int, output_tokens: int
    ) -> None:
        """发布 Token 用量事件"""
        ok = await self.publish(
            task_id,
            {
                "type": "usage",
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )
        if not ok:
            logger.warning("publish_usage failed for task %s", task_id)
