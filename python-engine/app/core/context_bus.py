"""通用上下文总线 (ContextBus)

设计目标:
- 类似 Apache Kafka + Redis Pub/Sub 的共享内存机制
- 各工作台发布/订阅状态变更
- 支持消息持久化、回放和 TTL 自动清理
- SaaS 租户隔离

架构思想:
类比微服务架构的事件总线 (Event Bus)
- Agent 执行完成后发布结果到总线
- 下游工作流节点订阅该主题获取输入
- 支持跨进程、跨实例的状态共享
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.config import settings
from app.redis_keys import rkey

logger = logging.getLogger(__name__)


class MessageType(StrEnum):
    """消息类型"""

    STATE_CHANGE = "state_change"  # 状态变更
    RESULT_PUBLISH = "result_publish"  # 结果发布
    COMMAND = "command"  # 命令下发
    QUERY = "query"  # 查询请求
    EVENT = "event"  # 事件通知


@dataclass
class ContextMessage:
    """上下文消息"""

    topic: str  # 主题: "agent.results", "workflow.state"
    message_type: MessageType
    data: dict[str, Any]
    tenant_id: str  # SaaS 安全: 租户隔离
    message_id: str = ""  # UUID
    timestamp: float = field(default_factory=time.time)
    ttl: int = 3600  # TTL (秒), 默认 1 小时
    reply_to: str = ""  # 回复主题 (请求-响应模式)


class InMemoryContextBus:
    """内存版 ContextBus (单机测试用)"""

    def __init__(self) -> None:
        self._subscriptions: dict[str, list[Callable[..., Any]]] = defaultdict(
            list
        )  # topic -> [callbacks]
        self._message_log: dict[str, list[ContextMessage]] = defaultdict(
            list
        )  # topic -> [messages]
        self._max_log_size = 1000  # 每个主题最多保留 1000 条消息

    async def publish(
        self,
        topic: str,
        data: dict[str, Any],
        tenant_id: str,
        message_type: MessageType = MessageType.RESULT_PUBLISH,
        ttl: int = 3600,
    ) -> ContextMessage:
        """发布消息到主题"""
        import uuid

        message = ContextMessage(
            topic=topic,
            message_type=message_type,
            data=data,
            tenant_id=tenant_id,
            message_id=uuid.uuid4().hex[:12],
            ttl=ttl,
        )

        # 存储到日志
        self._message_log[topic].append(message)
        if len(self._message_log[topic]) > self._max_log_size:
            self._message_log[topic] = self._message_log[topic][-self._max_log_size :]

        # 触发订阅者回调
        for callback in self._subscriptions[topic]:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(message)
                else:
                    callback(message)
            except Exception as e:
                logger.error(f"Subscription callback failed: {e}", exc_info=True)

        logger.debug(
            f"Published message to topic '{topic}' (tenant={tenant_id}, id={message.message_id})"
        )
        return message

    async def subscribe(
        self, topic: str, callback: Callable[[ContextMessage], Awaitable[None]]
    ) -> None:
        """订阅主题"""
        self._subscriptions[topic].append(callback)
        logger.info(f"Subscribed to topic '{topic}'")

    async def unsubscribe(self, topic: str, callback: Callable[..., Any]) -> bool:
        """取消订阅"""
        if topic in self._subscriptions:
            try:
                self._subscriptions[topic].remove(callback)
                return True
            except ValueError:
                return False
        return False

    async def query(
        self, topic: str, tenant_id: str, limit: int = 10
    ) -> list[ContextMessage]:
        """查询主题的历史消息"""
        messages = self._message_log.get(topic, [])
        # 过滤租户
        filtered = [m for m in messages if m.tenant_id == tenant_id]
        return filtered[-limit:]

    async def get_latest(self, topic: str, tenant_id: str) -> ContextMessage | None:
        """获取主题的最近一条消息"""
        messages = await self.query(topic, tenant_id, limit=1)
        return messages[0] if messages else None


class RedisContextBus:
    """Redis 版 ContextBus (生产环境使用)

    实现方式:
    - 使用 Redis Stream 作为消息日志
    - 使用 Redis Pub/Sub 实现实时推送
    - 使用 Redis Hash 存储最新状态 (Snapshot)
    """

    def __init__(self, redis_client: Any) -> None:
        self.redis = redis_client
        self._local_subs: dict[str, list[Callable[..., Any]]] = defaultdict(list)
        # Pub/Sub 监听：每对 (tenant_id, topic) 一个 channel
        self._pubsub_channels: set[str] = set()
        self._listener_task: asyncio.Task[None] | None = None
        self._listener_started = False

    async def publish(
        self,
        topic: str,
        data: dict[str, Any],
        tenant_id: str,
        message_type: MessageType = MessageType.RESULT_PUBLISH,
        ttl: int = 3600,
    ) -> ContextMessage:
        """发布消息到 Redis Stream

        Stream key: contextbus:{tenant}:{topic}
        """
        import uuid

        message = ContextMessage(
            topic=topic,
            message_type=message_type,
            data=data,
            tenant_id=tenant_id,
            message_id=uuid.uuid4().hex[:12],
            ttl=ttl,
        )

        # 写入 Redis Stream
        stream_key = rkey(f"contextbus:{tenant_id}:{topic}")
        entry = {
            "message_id": message.message_id,
            "message_type": message_type.value,
            "timestamp": str(message.timestamp),
            "data": json.dumps(data, ensure_ascii=False),
            "ttl": str(ttl),
        }

        await self.redis.xadd(stream_key, entry, maxlen=1000, approximate=True)

        # 更新 Snapshot (最新状态)
        snapshot_key = rkey(f"contextbus:snapshot:{tenant_id}:{topic}")
        await self.redis.setex(snapshot_key, ttl, json.dumps(data, ensure_ascii=False))

        # 发布到 Pub/Sub channel（跨进程通知）
        channel = rkey(f"contextbus:{tenant_id}:{topic}")
        await self.redis.publish(channel, json.dumps(entry, ensure_ascii=False))

        # 本地订阅者推送 (同一进程)
        for callback in self._local_subs[topic]:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(message)
                else:
                    callback(message)
            except Exception as e:
                logger.error(f"Local subscription callback failed: {e}", exc_info=True)

        logger.debug(
            f"Published message to Redis Stream '{stream_key}' (id={message.message_id})"
        )
        return message

    async def subscribe(
        self, topic: str, callback: Callable[..., Any], tenant_id: str = ""
    ) -> None:
        """订阅主题 (Redis Pub/Sub + 本地回调)

        创建独立的 Redis Pub/Sub 连接监听跨进程消息。
        本地回调既接收跨进程 Pub/Sub 消息，也接收同进程 Stream 消息。
        """
        # 注册本地回调
        self._local_subs[topic].append(callback)

        # 确定 Pub/Sub channel
        if tenant_id:
            channel = rkey(f"contextbus:{tenant_id}:{topic}")
        else:
            # 通配符：监听所有租户的该 topic
            # Redis Pub/Sub 支持 glob pattern: contextbus:*:topic
            channel = rkey(f"contextbus:*:{topic}")

        if channel not in self._pubsub_channels:
            self._pubsub_channels.add(channel)
            logger.info(f"Subscribed to Redis channel '{channel}'")

        # 启动后台监听任务（懒启动，整个实例只一个 task）
        if not self._listener_started:
            self._listener_started = True
            self._listener_task = asyncio.create_task(
                self._pubsub_listener(), name="context_bus_pubsub"
            )
            logger.info("ContextBus Pub/Sub listener started")

    async def _pubsub_listener(self) -> None:
        """后台监听 Redis Pub/Sub 消息，分发到本地回调。

        使用独立的 Redis 连接（从连接池获取）避免阻塞主连接。
        当新 channel 加入时重新订阅（重连机制）。
        """

        while True:
            try:
                # 从已有 client 获取 Pub/Sub 对象
                pubsub = self.redis.pubsub()

                # 订阅当前所有 channel
                channels = list(self._pubsub_channels)
                if not channels:
                    # 没有订阅，等待一会儿再检查
                    await asyncio.sleep(0.5)
                    continue

                await pubsub.subscribe(*channels)
                logger.debug(f"Pub/Sub subscribed to {len(channels)} channels")

                async for raw_msg in pubsub.listen():
                    if raw_msg["type"] != "message":
                        continue

                    try:
                        channel = raw_msg["channel"]
                        if isinstance(channel, bytes):
                            channel = channel.decode("utf-8")

                        # 解析 channel 格式: contextbus:{tenant_id}:{topic}
                        # 或 contextbus:*:{topic} 的通配匹配
                        # 统一键前缀模式下 channel 以 REDIS_KEY_PREFIX 开头，先剥离再解析
                        if channel.startswith(settings.redis_key_prefix):
                            channel = channel[len(settings.redis_key_prefix):]
                        parts = channel.split(":", 2)
                        if len(parts) != 3:
                            continue
                        _, msg_tenant_id, topic = parts

                        data_raw = raw_msg["data"]
                        if isinstance(data_raw, bytes):
                            data_raw = data_raw.decode("utf-8")
                        entry = json.loads(data_raw)

                        # 构造 ContextMessage
                        message = ContextMessage(
                            topic=topic,
                            message_type=MessageType(
                                entry.get("message_type", "event")
                            ),
                            data=json.loads(entry.get("data", "{}")),
                            tenant_id=msg_tenant_id,
                            message_id=entry.get("message_id", ""),
                            timestamp=float(entry.get("timestamp", time.time())),
                        )

                        # 分发到本地回调
                        for cb in self._local_subs.get(topic, []):
                            try:
                                if asyncio.iscoroutinefunction(cb):
                                    await cb(message)
                                else:
                                    cb(message)
                            except Exception as e:
                                logger.error(
                                    f"Pub/Sub callback failed: {e}", exc_info=True
                                )

                    except Exception as e:
                        logger.error(
                            f"Pub/Sub message dispatch failed: {e}", exc_info=True
                        )

            except asyncio.CancelledError:
                logger.info("ContextBus Pub/Sub listener cancelled")
                break
            except Exception as e:
                logger.error(f"ContextBus Pub/Sub listener error: {e}", exc_info=True)
                await asyncio.sleep(1.0)  # 退避后重连

    async def unsubscribe(
        self, topic: str, callback: Callable[..., Any], tenant_id: str = ""
    ) -> bool:
        """取消订阅"""
        removed = False
        if topic in self._local_subs:
            try:
                self._local_subs[topic].remove(callback)
                removed = True
            except ValueError:
                pass

        # 如果该 topic 已无任何本地订阅者，移除 channel
        if topic in self._local_subs and not self._local_subs[topic]:
            if tenant_id:
                channel = rkey(f"contextbus:{tenant_id}:{topic}")
            else:
                channel = rkey(f"contextbus:*:{topic}")
            self._pubsub_channels.discard(channel)

        return removed

    async def query(
        self, topic: str, tenant_id: str, limit: int = 10
    ) -> list[ContextMessage]:
        """查询历史消息 (从 Redis Stream)"""
        stream_key = rkey(f"contextbus:{tenant_id}:{topic}")

        entries = await self.redis.xrange(stream_key, count=limit)
        messages = []

        for entry in entries:
            data = json.loads(entry["data"])
            messages.append(
                ContextMessage(
                    topic=topic,
                    message_type=MessageType(entry["message_type"]),
                    data=data,
                    tenant_id=tenant_id,
                    message_id=entry["message_id"],
                    timestamp=float(entry["timestamp"]),
                )
            )

        return messages

    async def get_latest(self, topic: str, tenant_id: str) -> ContextMessage | None:
        """获取最新状态 (从 Snapshot)"""
        snapshot_key = rkey(f"contextbus:snapshot:{tenant_id}:{topic}")

        data = await self.redis.get(snapshot_key)
        if data:
            return ContextMessage(
                topic=topic,
                message_type=MessageType.RESULT_PUBLISH,
                data=json.loads(data),
                tenant_id=tenant_id,
                timestamp=time.time(),
            )
        return None


# ── 工厂函数 ───────────────────────────────────────────────────────
_context_bus_instance: Any = None
_is_redis = False


async def get_context_bus(redis_client: Any = None) -> Any:
    """获取 ContextBus 实例 (单例模式)

    Args:
        redis_client: Redis 客户端 (生产环境传入,否则使用内存版)
    """
    global _context_bus_instance, _is_redis

    if _context_bus_instance is None:
        if redis_client:
            _context_bus_instance = RedisContextBus(redis_client)
            _is_redis = True
            logger.info("ContextBus initialized with Redis")
        else:
            _context_bus_instance = InMemoryContextBus()
            _is_redis = False
            logger.info("ContextBus initialized with in-memory store")

    return _context_bus_instance


# ── 便捷函数 ────────────────────────────────────────────────────────
async def publish_result(
    topic: str, data: dict[str, Any], tenant_id: str, trace_id: str = ""
) -> Any:
    """便捷函数: 发布结果到总线"""
    bus = await get_context_bus()
    return await bus.publish(topic, data, tenant_id, MessageType.RESULT_PUBLISH)


async def publish_state_change(
    topic: str, data: dict[str, Any], tenant_id: str
) -> Any:
    """便捷函数: 发布状态变更"""
    bus = await get_context_bus()
    return await bus.publish(topic, data, tenant_id, MessageType.STATE_CHANGE)


async def subscribe_topic(
    topic: str, callback: Callable[..., Any], tenant_id: str = ""
) -> Any:
    """便捷函数: 订阅主题"""
    bus = await get_context_bus()
    return await bus.subscribe(topic, callback, tenant_id)


# ── 典型使用场景 ────────────────────────────────────────────────────
"""
典型用例:

1. Agent 执行完成后发布结果:
   await publish_result(
       topic="agent.tasks.analysis",
       data={"output": "...", "status": "completed"},
       tenant_id="tenant_001",
   )

2. 工作流节点订阅结果:
   async def on_agent_result(message):
       print(f"收到结果: {message.data}")

   await subscribe_topic("agent.tasks.analysis", on_agent_result)

3. 查询历史状态:
   latest = await get_context_bus().get_latest("workflow.status", "tenant_001")
"""
