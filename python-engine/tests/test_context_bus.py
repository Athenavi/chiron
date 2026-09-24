"""Context Bus (RedisContextBus) Pub/Sub 测试。

核心意图:
- subscribe 必须真正注册 Redis Pub/Sub channel (而非只记录日志)
- 发布的消息必须同时写入 Stream、更新 Snapshot、广播到 Pub/Sub channel
- 后台监听器必须把跨进程 Pub/Sub 消息分发到本地回调
- unsubscribe 后 channel 无订阅者时应清理
"""
from __future__ import annotations

import pytest

import app.main  # noqa: F401 — 初始化 app 包，避免循环导入
from app.core.context_bus import InMemoryContextBus, RedisContextBus


class FakeRedis:
    """最小 Redis 假实现: 记录 xadd/setex/publish 调用。"""

    def __init__(self):
        self.xadd_calls: list[tuple[str, dict]] = []
        self.setex_calls: list[tuple[str, int, str]] = []
        self.publish_calls: list[tuple[str, str]] = []

    async def xadd(self, key: str, entry: dict, **kwargs) -> str:
        self.xadd_calls.append((key, entry))
        return "1-0"

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.setex_calls.append((key, ttl, value))

    async def publish(self, channel: str, message: str) -> int:
        self.publish_calls.append((channel, message))
        return 1


async def test_publish_writes_stream_snapshot_and_pubsub():
    """publish 必须三写: Stream + Snapshot + Pub/Sub channel。"""
    redis = FakeRedis()
    bus = RedisContextBus(redis)

    await bus.publish("agent.results", {"output": "x"}, tenant_id="t1")

    # Stream
    assert redis.xadd_calls and redis.xadd_calls[0][0] == "contextbus:t1:agent.results"
    # Snapshot
    assert redis.setex_calls and redis.setex_calls[0][0] == "contextbus:snapshot:t1:agent.results"
    # Pub/Sub broadcast
    assert redis.publish_calls and redis.publish_calls[0][0] == "contextbus:t1:agent.results"


async def test_subscribe_registers_pubsub_channel():
    """subscribe 必须把 channel 记入 _pubsub_channels (真实订阅的前提)。"""
    redis = FakeRedis()
    bus = RedisContextBus(redis)

    async def _cb(msg):
        pass

    await bus.subscribe("agent.results", _cb, tenant_id="t1")
    assert "contextbus:t1:agent.results" in bus._pubsub_channels

    # 不带 tenant_id → 通配符 channel
    await bus.subscribe("workflow.state", _cb)
    assert "contextbus:*:workflow.state" in bus._pubsub_channels


async def test_unsubscribe_cleans_up_empty_channel():
    """最后一个订阅者取消后,channel 应从集合中移除。"""
    redis = FakeRedis()
    bus = RedisContextBus(redis)

    async def _cb(msg):
        pass

    await bus.subscribe("agent.results", _cb, tenant_id="t1")
    removed = await bus.unsubscribe("agent.results", _cb, tenant_id="t1")
    assert removed is True
    assert "contextbus:t1:agent.results" not in bus._pubsub_channels


async def test_unsubscribe_keeps_channel_with_other_subscribers():
    """仍有其他订阅者时,channel 不应被移除。"""
    redis = FakeRedis()
    bus = RedisContextBus(redis)

    async def _cb1(msg):
        pass

    async def _cb2(msg):
        pass

    await bus.subscribe("agent.results", _cb1, tenant_id="t1")
    await bus.subscribe("agent.results", _cb2, tenant_id="t1")
    await bus.unsubscribe("agent.results", _cb1, tenant_id="t1")
    assert "contextbus:t1:agent.results" in bus._pubsub_channels
    assert len(bus._local_subs["agent.results"]) == 1


async def test_in_memory_bus_roundtrip():
    """InMemoryContextBus: 发布消息必须触发本地回调并可查询历史。"""
    bus = InMemoryContextBus()
    received = []

    async def _cb(msg):
        received.append(msg)

    await bus.subscribe("topic.a", _cb)
    await bus.publish("topic.a", {"k": 1}, tenant_id="t1")

    assert len(received) == 1
    assert received[0].data == {"k": 1}
    history = await bus.query("topic.a", "t1")
    assert len(history) == 1

    latest = await bus.get_latest("topic.a", "t1")
    assert latest is not None and latest.data == {"k": 1}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
