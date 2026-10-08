"""ContextBus Pub/Sub 监听器的连接生命周期：每一轮结束都必须关闭 pubsub。

背景：`RedisContextBus._pubsub_listener` 每轮 `self.redis.pubsub()` 新建一个 Pub/Sub
（占用连接池里的一条连接），但原先**没有任何 `finally` 关闭它** —— 重连、无订阅
`continue`、任务被取消，三种路径都会把 pubsub 直接丢掉，连接随之泄漏。多副本长跑实例
每重连一次漏一条，最终耗尽连接池。这里用最小假件把「退出时必须关闭」钉住
（不需要真实 Redis）。
"""
from __future__ import annotations

import asyncio
import contextlib

from app.core.context_bus import RedisContextBus


class _FakePubSub:
    def __init__(self, listen) -> None:
        self._listen = listen
        self.closed = False
        self.subscribed: list[str] = []

    async def subscribe(self, *channels: str) -> None:
        self.subscribed.extend(channels)

    async def unsubscribe(self, *channels: str) -> None:
        return None

    def listen(self):
        return self._listen()

    async def aclose(self) -> None:
        self.closed = True


class _FakeRedis:
    def __init__(self, listen) -> None:
        self._listen = listen
        self.pubsubs: list[_FakePubSub] = []

    def pubsub(self) -> _FakePubSub:
        ps = _FakePubSub(self._listen)
        self.pubsubs.append(ps)
        return ps


async def _forever_listen():
    while True:
        await asyncio.sleep(3600)
        yield  # pragma: no cover - 只有被取消/关闭时才可能走到


async def test_listener_closes_pubsub_when_cancelled():
    fake = _FakeRedis(_forever_listen)
    bus = RedisContextBus(fake)
    bus._pubsub_channels.add("contextbus:t1:topic")  # 走真实订阅路径而不是「无订阅 continue」
    bus._listener_started = True
    task = asyncio.create_task(bus._pubsub_listener())

    await asyncio.sleep(0.05)  # 让它进入 subscribe + listen
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert fake.pubsubs, "监听器应至少创建过一个 Pub/Sub"
    assert fake.pubsubs[0].subscribed, "应真的订阅了注册的 channel"
    assert fake.pubsubs[0].closed, "任务结束时必须关闭 pubsub —— 否则每轮漏一条连接"
