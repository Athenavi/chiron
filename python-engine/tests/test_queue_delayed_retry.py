"""P2-3：回传链路的"要等"必须真的能等 —— 延迟重试而不是耗尽重试预算。

背景（docs/subagent-interaction-redesign.md §八「尚未完成」）：

``agent_followup`` 要把"后台子 Agent 已结束"变成父会话上的新一轮。它最常见的失败
不是任务本身有问题，而是**父会话此刻正锁着**（网关返回 409）。修复前的行为是：

    409 → 立刻放回队尾 → 再次 409 → … → 3 次重试在毫秒级耗尽 → 进 DLQ

于是"子 Agent 的结论已经落库"却再也回不到主对话 —— 而原因从头到尾只是**需要等一会儿**。

这里钉住三件事：
1. "要等"的任务走延迟集合，**不消耗** ``retry_count``；
2. 到点由 :meth:`QueueWorker._promote_delayed` 搬回主队列，且多实例下只有一个实例重投；
3. 等不到头（超过 ``DELAY_MAX_ATTEMPTS``）或载荷本身有问题（4xx）时，**可见地结束**，
   不留"排着队空转"的中间态。
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from app.queue.worker import DELAY_MAX_ATTEMPTS, DLQ_STREAM, TASK_STREAM, QueueWorker, RetryLaterError


class FakeRedis:
    """只实现本文件用到的命令：xadd/xack + 延迟集合的 zadd/zrem/zrangebyscore。"""

    def __init__(self) -> None:
        self.streams: list[dict] = []
        self.zset: dict[str, float] = {}
        self.acked: list[str] = []

    async def xadd(self, stream, fields, maxlen=None):
        self.streams.append({"stream": stream, "fields": fields})
        return f"{len(self.streams)}-0"

    async def xack(self, stream, group, msg_id):
        self.acked.append(msg_id)
        return 1

    async def zadd(self, key, mapping):
        self.zset.update(mapping)
        return len(mapping)

    async def zrem(self, key, member):
        return 1 if self.zset.pop(member, None) is not None else 0

    async def zrangebyscore(self, key, min, max, start=None, num=None):
        due = [m for m, s in sorted(self.zset.items(), key=lambda kv: kv[1]) if s <= max]
        begin = int(start or 0)
        return due[begin:] if num is None else due[begin:begin + int(num)]


def fields(task_type="agent_followup", task_id="t1", payload="{}", retry_count=0, **extra):
    data = {
        b"task_type": task_type.encode(),
        b"task_id": task_id.encode(),
        b"payload": payload.encode(),
        b"retry_count": str(retry_count).encode(),
    }
    data.update({k.encode(): str(v).encode() for k, v in extra.items()})
    return data


def received(redis: FakeRedis) -> list[dict]:
    return [m for m in redis.streams if m["stream"] == TASK_STREAM]


@pytest.fixture(autouse=True)
def _no_pg_idempotency(monkeypatch):
    """幂等闸门走 PG：单测里替换掉，避免依赖真实数据库。"""
    from app.queue import idempotency

    monkeypatch.setattr(idempotency, "claim", AsyncMock(return_value=True))
    monkeypatch.setattr(idempotency, "complete", AsyncMock())
    monkeypatch.setattr(idempotency, "fail", AsyncMock())


class TestDeferredRetry:
    async def test_deferred_task_does_not_spend_retry_budget(self):
        """409 之类的"要等"必须进延迟集合，而不是立刻重投（后者会瞬间耗尽预算）。"""
        redis = FakeRedis()
        worker = QueueWorker(redis=redis)
        worker._dispatch = AsyncMock(side_effect=RetryLaterError("session busy", delay_seconds=0))

        await worker._process_message("1-0", fields(retry_count=0))

        assert not received(redis), "延迟重试不应立刻把消息放回主队列"
        assert len(redis.zset) == 1
        import json

        message = json.loads(next(iter(redis.zset)))
        assert message["retry_count"] == "0", "延迟重试不是失败，不能消耗重试预算"
        assert message["delay_count"] == "1"
        assert redis.acked == ["1-0"], "原消息必须 ACK，否则会被 reclaim 重复处理"

    async def test_promote_moves_due_task_back_to_stream(self):
        redis = FakeRedis()
        worker = QueueWorker(redis=redis)
        worker._dispatch = AsyncMock(side_effect=RetryLaterError("session busy"))
        await worker._process_message("1-0", fields())

        # 未到期：不动
        assert await worker._promote_delayed(now=time.time()) == 0
        assert not received(redis)

        # 到期：搬回主队列
        assert await worker._promote_delayed(now=time.time() + 10_000) == 1
        assert len(received(redis)) == 1
        moved = received(redis)[0]["fields"]
        assert moved["task_type"] == "agent_followup"
        assert moved["retry_count"] == "0"

    async def test_promote_is_single_owner_across_instances(self):
        """两个实例同时搬：只能有一个成功（ZREM 原子抢占），否则会重复开一轮。"""
        redis = FakeRedis()
        worker_a = QueueWorker(redis=redis)
        worker_b = QueueWorker(redis=redis)
        worker_a._dispatch = AsyncMock(side_effect=RetryLaterError("session busy"))
        await worker_a._process_message("1-0", fields())

        now = time.time() + 10_000
        first = await worker_a._promote_delayed(now=now)
        second = await worker_b._promote_delayed(now=now)
        assert (first, second) == (1, 0)
        assert len(received(redis)) == 1

    async def test_gives_up_visibly_after_max_attempts(self):
        """等不到头时进 DLQ（可见、可人工重投），而不是无限排着队空转。"""
        redis = FakeRedis()
        worker = QueueWorker(redis=redis)
        worker._dispatch = AsyncMock(side_effect=RetryLaterError("session busy"))

        await worker._process_message(
            "1-0", fields(delay_count=DELAY_MAX_ATTEMPTS)
        )

        dlq = [m for m in redis.streams if m["stream"] == DLQ_STREAM]
        assert len(dlq) == 1
        assert "deferred retry limit" in dlq[0]["fields"]["error"]
        assert not redis.zset
        assert redis.acked == ["1-0"]

    async def test_falls_back_to_immediate_requeue_when_zset_unavailable(self):
        """延迟集合写不进去（Redis 故障）时必须退回立即重投：宁可早试，不能丢。"""
        class _BrokenZset(FakeRedis):
            async def zadd(self, key, mapping):
                raise RuntimeError("WRONGTYPE")

        redis = _BrokenZset()
        worker = QueueWorker(redis=redis)
        worker._dispatch = AsyncMock(side_effect=RetryLaterError("session busy"))

        await worker._process_message("1-0", fields(retry_count=0))

        queued = received(redis)
        assert len(queued) == 1
        assert queued[0]["fields"]["retry_count"] == "1"  # 退化为普通重试，预算正常消耗
        assert redis.acked == ["1-0"]

    async def test_backoff_grows_and_is_capped(self):
        exc = RetryLaterError("busy")
        delays = [QueueWorker._defer_delay(n, exc) for n in range(1, 8)]
        assert delays[0] < delays[1] < delays[2]
        assert all(d <= 300 for d in delays)
        assert QueueWorker._defer_delay(1, RetryLaterError("busy", delay_seconds=5)) == 5


class _FakeResponse:
    def __init__(self, status_code, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    @property
    def is_success(self):
        return 200 <= self.status_code < 300


class _FakeClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        return self._response


def _patch_gateway(response):
    """让 handler 用的 httpx 客户端返回给定的响应，并提供 internal_token。"""
    from app.config import settings

    return (
        patch("httpx.AsyncClient", lambda **kwargs: _FakeClient(response)),
        patch.object(settings, "internal_token", "tok"),
    )


class TestAgentFollowupHandler:
    async def test_409_defers_instead_of_failing(self):
        worker = QueueWorker(redis=FakeRedis())
        client_patch, token_patch = _patch_gateway(
            _FakeResponse(409, "session busy", {"retry-after": "30"})
        )
        with client_patch, token_patch:
            with pytest.raises(RetryLaterError) as err:
                await worker._handle_agent_followup(
                    {"run_id": "rs_1", "session_id": "s1", "tenant_id": "t1", "user_id": "u1"}
                )
        assert err.value.delay_seconds == 30  # 上游说了等 30s，就等 30s

    async def test_429_defers(self):
        worker = QueueWorker(redis=FakeRedis())
        client_patch, token_patch = _patch_gateway(_FakeResponse(429, "too many"))
        with client_patch, token_patch:
            with pytest.raises(RetryLaterError):
                await worker._handle_agent_followup(
                    {"run_id": "rs_1", "session_id": "s1", "user_id": "u1"}
                )

    async def test_transport_error_defers(self):
        """网关重启/滚动升级：等它回来，而不是把这条信号在毫秒级重试里烧掉。"""
        worker = QueueWorker(redis=FakeRedis())
        from app.config import settings

        with patch("httpx.AsyncClient", side_effect=RuntimeError("connection refused")), \
             patch.object(settings, "internal_token", "tok"):
            with pytest.raises(RetryLaterError):
                await worker._handle_agent_followup(
                    {"run_id": "rs_1", "session_id": "s1", "user_id": "u1"}
                )

    async def test_payload_error_is_not_retried(self):
        """4xx 是载荷问题：重试也没用，明确记录后直接 ACK（可见地失败）。"""
        worker = QueueWorker(redis=FakeRedis())
        client_patch, token_patch = _patch_gateway(_FakeResponse(400, "bad payload"))
        with client_patch, token_patch:
            await worker._handle_agent_followup(
                {"run_id": "rs_1", "session_id": "s1", "user_id": "u1"}
            )  # 不抛：调用方不该重试

    async def test_5xx_still_fails_hard(self):
        """服务端错误保留硬失败语义（重试 → DLQ），不被"要等"掩盖。"""
        worker = QueueWorker(redis=FakeRedis())
        client_patch, token_patch = _patch_gateway(_FakeResponse(503, "unavailable"))
        with client_patch, token_patch:
            with pytest.raises(RuntimeError) as err:
                await worker._handle_agent_followup(
                    {"run_id": "rs_1", "session_id": "s1", "user_id": "u1"}
                )
        assert not isinstance(err.value, RetryLaterError)
