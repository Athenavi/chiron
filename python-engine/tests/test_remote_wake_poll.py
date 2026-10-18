"""跨副本审批/回答的 hybrid 唤醒轮询 `_take_key_with_wake` 的回归防线。

这个 helper 是 M10 把 0.15s 忙轮询改成「唤醒频道 + 兜底轮询」的核心，且坐在**单次消费**
这条不变量上（M6/M7 刚加固的人工介入路径）。用桩 Redis 直接测它，钉住四件事：
- 命中即 GET+DEL（仲裁仍是键，唤醒只是加速器）；
- 丢唤醒时兜底轮询仍取走（PUBLISH 不是正确性依赖）；
- 唤醒让认领即时（不等满 interval）；
- 超时返回 None 且不消费键。
"""

import asyncio

from app.agent.runtime import AgentRuntime


class _StubPubSub:
    """wake=True 时 get_message 立即吐一条唤醒；否则模拟「超时」返回 None（不真阻塞）。"""

    def __init__(self, wake: bool) -> None:
        self._wake = wake

    async def subscribe(self, channel: str) -> None:  # noqa: D401
        return None

    async def get_message(self, ignore_subscribe_messages: bool = False, timeout: float = 0):
        if self._wake:
            self._wake = False  # 只叫醒一次，逼出「唤醒后再 GET」的那一步
            return {"type": "message", "data": b"x"}
        await asyncio.sleep(0)  # 让出一次，避免纯桩忙等
        return None

    async def aclose(self) -> None:
        return None


class _StubRedis:
    """get 依脚本依次返回（越界视为 miss）；delete 记录被取走的键。"""

    def __init__(self, get_results: list, *, wake: bool) -> None:
        self._gets = list(get_results)
        self._n = 0
        self.deleted: list[str] = []
        self._wake = wake

    async def get(self, key: str):
        r = self._gets[self._n] if self._n < len(self._gets) else None
        self._n += 1
        return r

    async def delete(self, key: str) -> int:
        self.deleted.append(key)
        return 1

    def pubsub(self) -> _StubPubSub:
        return _StubPubSub(self._wake)


def _runtime() -> AgentRuntime:
    # helper 不读任何实例属性，绕开重量级 __init__ 即可单测。
    return AgentRuntime.__new__(AgentRuntime)


async def _inject(monkeypatch, redis: _StubRedis) -> None:
    async def fake_get_redis():
        return redis

    monkeypatch.setattr("app.redis_client.get_redis", fake_get_redis)


async def test_hit_consumes_atomically_and_returns_value(monkeypatch):
    redis = _StubRedis([b"value"], wake=False)
    await _inject(monkeypatch, redis)
    got = await _runtime()._take_key_with_wake("k", "c", timeout=1.0, interval=0.01)
    assert got == b"value"
    assert redis.deleted == ["k"]  # 单次消费：取走即删


async def test_wake_claims_before_interval_elapses(monkeypatch):
    # interval 远超 timeout：若只靠兜底轮询必然超时返回 None；能拿到值即证明是唤醒驱动的提前 GET。
    redis = _StubRedis([None, b"value"], wake=True)
    await _inject(monkeypatch, redis)
    got = await _runtime()._take_key_with_wake("k", "c", timeout=0.5, interval=100.0)
    assert got == b"value"
    assert redis.deleted == ["k"]


async def test_fallback_poll_recovers_when_wake_lost(monkeypatch):
    # wake=False 模拟 PUBLISH 丢失；短 interval 兜底轮询应在 deadline 前把值取回。
    redis = _StubRedis([None, None, b"value"], wake=False)
    await _inject(monkeypatch, redis)
    got = await _runtime()._take_key_with_wake("k", "c", timeout=1.0, interval=0.01)
    assert got == b"value"
    assert redis.deleted == ["k"]


async def test_timeout_returns_none_without_consuming(monkeypatch):
    redis = _StubRedis([None], wake=False)
    await _inject(monkeypatch, redis)
    got = await _runtime()._take_key_with_wake("k", "c", timeout=0.05, interval=0.01)
    assert got is None
    assert redis.deleted == []  # 从未命中 ⇒ 绝不删键


async def test_redis_unavailable_returns_none(monkeypatch):
    async def none_get_redis():
        return None

    monkeypatch.setattr("app.redis_client.get_redis", none_get_redis)
    # redis 为 None 时应在建 pubsub 之前短路返回。
    assert await _runtime()._take_key_with_wake("k", "c", timeout=0.05, interval=0.01) is None
