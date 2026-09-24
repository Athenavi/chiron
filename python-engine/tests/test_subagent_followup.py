"""子 Agent followup 信号的风控与投递测试。

覆盖（对应 ``app/subagent/followup.py`` 的保守默认）：
* 幂等：同一 run 只投递一次（正常终态与取消路径都会调用它）；
* 速率：每会话每小时上限；
* 级联保护：由 followup 唤起的那一轮里再派生的子 Agent 不再触发；
* 关闭开关（上限 <= 0）；
* Redis 不可用时不投递（宁可不动，也不要降级成可能重复唤起）；
* 投递载荷：task_type / 稳定幂等键 / payload 字段齐全。
"""
import asyncio
import json

from app.queue.producer import TASK_STREAM
from app.subagent import followup


class FakeRedis:
    """只实现 followup 与 QueueProducer 用到的那几个命令。"""

    def __init__(self, *, fail: bool = False):
        self.store: dict[str, object] = {}
        self.stream: list[tuple[str, dict]] = []
        self.fail = fail

    async def set(self, key, value, nx=False, ex=None):  # noqa: ANN001, ANN003
        if self.fail:
            raise RuntimeError("redis unavailable")
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def incr(self, key):  # noqa: ANN001
        if self.fail:
            raise RuntimeError("redis unavailable")
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    async def expire(self, key, ttl):  # noqa: ANN001
        return True

    async def xadd(self, stream, fields, maxlen=None, approximate=False):  # noqa: ANN001, ANN003
        if self.fail:
            raise RuntimeError("redis unavailable")
        self.stream.append((stream, fields))
        return f"{len(self.stream)}-0"


def _patch(monkeypatch, redis, *, limit=3):  # noqa: ANN001
    async def _get_redis():
        return redis

    monkeypatch.setattr("app.redis_client.get_redis", _get_redis)
    monkeypatch.setattr(followup, "max_per_hour", lambda: limit)


def _call(run_id="rs_1", session_id="s1", **kwargs):
    return asyncio.run(followup.enqueue_followup(
        run_id=run_id, session_id=session_id, tenant_id="t1", user_id="u1",
        status="completed", summary="结论", profile="reviewer", depth=1, **kwargs,
    ))


def test_投递一次成功_第二次被幂等挡下(monkeypatch):
    redis = FakeRedis()
    _patch(monkeypatch, redis)
    assert _call() is True
    assert _call() is False, "同一 run 不得投递第二次"
    assert len(redis.stream) == 1


def test_payload_形状(monkeypatch):
    redis = FakeRedis()
    _patch(monkeypatch, redis)
    assert _call() is True
    stream, fields = redis.stream[0]
    assert stream == TASK_STREAM
    assert fields["task_type"] == "agent_followup"
    # 稳定业务键：worker 的 claim 只拒绝 completed，同 run 重投会被丢弃
    assert fields["idempotency_key"] == "agent_followup:rs_1"
    payload = json.loads(fields["payload"])
    assert payload["run_id"] == "rs_1"
    assert payload["session_id"] == "s1"
    assert payload["summary"] == "结论"


def test_每会话每小时上限(monkeypatch):
    redis = FakeRedis()
    _patch(monkeypatch, redis, limit=3)
    assert _call(run_id="rs_1") is True
    assert _call(run_id="rs_2") is True
    assert _call(run_id="rs_3") is True
    assert _call(run_id="rs_4") is False, "第 4 次应被速率上限挡下"
    assert len(redis.stream) == 3


def test_不同会话各自计数(monkeypatch):
    redis = FakeRedis()
    _patch(monkeypatch, redis, limit=1)
    assert _call(run_id="rs_1", session_id="s1") is True
    assert _call(run_id="rs_2", session_id="s2") is True
    assert _call(run_id="rs_3", session_id="s1") is False


def test_级联保护(monkeypatch):
    redis = FakeRedis()
    _patch(monkeypatch, redis)
    assert _call(cascade=True) is False
    assert redis.stream == []


def test_上限为0时关闭(monkeypatch):
    redis = FakeRedis()
    _patch(monkeypatch, redis, limit=0)
    assert _call() is False
    assert redis.stream == []


def test_redis不可用时不投递(monkeypatch):
    redis = FakeRedis(fail=True)
    _patch(monkeypatch, redis)
    assert _call() is False
    assert redis.stream == []


def test_无redis客户端时不投递(monkeypatch):
    async def _none():
        return None

    monkeypatch.setattr("app.redis_client.get_redis", _none)
    assert _call() is False


def test_缺少必填参数不投递(monkeypatch):
    redis = FakeRedis()
    _patch(monkeypatch, redis)
    assert asyncio.run(followup.enqueue_followup(run_id="", session_id="s1")) is False
    assert asyncio.run(followup.enqueue_followup(run_id="rs_1", session_id="")) is False
    assert redis.stream == []


def test_max_per_hour_读环境变量(monkeypatch):
    monkeypatch.setenv("SUBAGENT_FOLLOWUP_MAX_PER_HOUR", "7")
    assert followup.max_per_hour() == 7
    monkeypatch.setenv("SUBAGENT_FOLLOWUP_MAX_PER_HOUR", "abc")
    assert followup.max_per_hour() == followup.DEFAULT_MAX_PER_HOUR
    monkeypatch.delenv("SUBAGENT_FOLLOWUP_MAX_PER_HOUR", raising=False)
    assert followup.max_per_hour() == followup.DEFAULT_MAX_PER_HOUR
