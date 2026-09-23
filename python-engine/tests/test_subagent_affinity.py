"""P4：子 Agent 作业的归属登记、回执与注销。

背景：后台 run 的任务活在**父 turn 所在实例**的进程里，而网关没有 run → 实例的映射，
取消只能靠一条广播。实例在取消前重启/被驱逐时，广播无人认领，而网关**仍然返回
accepted** —— 用户看到"点了停止一直转圈"，DB 里那行会一直停在 running（默认 2 小时后
才被判 lost）。

这里钉住四件事：
1. 归属登记：键与载荷（`subagent:run:{run_id}`）能表达"谁在跑"，且实例标识**进程内稳定**；
2. 取消回执（`subagent:cancel:ack:{run_id}`）：网关据此区分"真取消"与"无人认领"；
3. 收尾注销：run 结束后不再声称自己持有它（否则取消会被路由到早已不持有它的实例）；
4. 关机会批量注销（进程要走了，键留着只会误导网关）。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.subagent import affinity, registry

# ── fake redis（只实现归属路径用到的命令）──


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.expires: dict[str, int] = {}
        self.lists: dict[str, list] = {}
        self.deleted: list[str] = []

    async def set(self, key, value, ex=None):
        self.kv[key] = value
        self.expires[key] = ex
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def delete(self, key):
        self.kv.pop(key, None)
        self.deleted.append(key)
        return 1

    async def expire(self, key, ttl):
        self.expires[key] = ttl
        return True

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    async def eval(self, script, numkeys, *args):
        """只实现 affinity 的 release Lua：token 匹配才删。"""
        key, token = args[0], args[-1]
        raw = self.kv.get(key)
        if raw is None:
            return 0
        try:
            data = json.loads(raw)
        except ValueError:
            return 0
        if data.get("token") == token:
            del self.kv[key]
            return 1
        return 0


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


# ── 归属登记 ──


@pytest.mark.asyncio
async def test_publish_owner_writes_identity_and_context(fake_redis):
    lease = affinity.OwnerLease("rs_1", session_id="s1", tenant_id="t1", redis=fake_redis)
    assert await lease.start() is True

    raw = fake_redis.kv[affinity.owner_key("rs_1")]
    data = json.loads(raw)
    assert data["instance_id"], "必须写明是哪个实例在跑（网关据此判定可达性）"
    assert data["session_id"] == "s1" and data["tenant_id"] == "t1"
    assert data["token"] == lease._token
    assert fake_redis.expires[affinity.owner_key("rs_1")] == affinity.owner_ttl()
    await lease.stop()


@pytest.mark.asyncio
async def test_owner_ttl_outlives_a_long_run():
    """TTL 必须显著长于单 run 的运行上限，否则跑得久的作业会中途"失去归属"。"""
    assert affinity.DEFAULT_OWNER_TTL >= 3600
    assert affinity.OWNER_HEARTBEAT_SECONDS * 3 <= affinity.DEFAULT_OWNER_TTL


def test_instance_id_is_stable_within_process(monkeypatch):
    """实例标识必须稳定：每次调用换名字会让网关看到的映射自相矛盾。"""
    from app.config import settings

    monkeypatch.setattr(affinity, "_INSTANCE_CACHE", "")
    monkeypatch.setattr(settings, "instance_id", "eng-test", raising=False)
    assert affinity.cached_instance_id() == "eng-test"

    # 兜底分支带随机后缀 —— 所以调用方必须经由 cached_instance_id（这正是缓存的理由）
    monkeypatch.setattr(settings, "instance_id", "", raising=False)
    monkeypatch.setattr(settings, "pod_name", "", raising=False)
    monkeypatch.setattr(affinity, "_INSTANCE_CACHE", "")
    first = affinity.cached_instance_id()
    assert first and first == affinity.cached_instance_id()


@pytest.mark.asyncio
async def test_owner_of_returns_none_when_missing(fake_redis):
    assert await affinity.owner_of(fake_redis, "rs_missing") is None
    assert await affinity.owner_of(None, "rs_1") is None


# ── 注销 ──


@pytest.mark.asyncio
async def test_stop_releases_only_with_matching_token(fake_redis):
    lease = affinity.OwnerLease("rs_1", redis=fake_redis)
    await lease.start()
    await lease.stop()
    assert affinity.owner_key("rs_1") not in fake_redis.kv

    # 迟到的清理不得删掉后继登记（token 不同）
    other = affinity.OwnerLease("rs_2", redis=fake_redis)
    await other.start()
    await affinity.release_owner(fake_redis, "rs_2", token="stale-token")
    assert affinity.owner_key("rs_2") in fake_redis.kv
    await other.stop()


@pytest.mark.asyncio
async def test_release_owners_deletes_all(fake_redis, monkeypatch):
    async def _fake_get():
        return fake_redis

    monkeypatch.setattr(affinity, "get_owner_redis", _fake_get)
    async def _noop_sleep(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    lease = affinity.OwnerLease("rs_1", redis=fake_redis)
    await lease.start()
    released = await affinity.release_owners(["rs_1", "rs_2"])
    assert released == 2
    assert affinity.owner_key("rs_1") not in fake_redis.kv
    await lease.stop()


# ── 取消回执 ──


@pytest.mark.asyncio
async def test_ack_cancel_writes_short_lived_receipt(fake_redis):
    assert await affinity.ack_cancel(fake_redis, "rs_1") is True
    key = affinity.ack_key("rs_1")
    assert fake_redis.lists[key] == ["1"]
    # 回执只给网关等几秒用：TTL 必须有，否则键会永久堆积
    assert fake_redis.expires[key] == affinity.ACK_TTL_SECONDS


@pytest.mark.asyncio
async def test_cancel_hit_writes_receipt(fake_redis):
    """命中本地注册表 → 真取消 + 回执（网关据此判定"有人认领"）。"""
    task = asyncio.create_task(asyncio.sleep(3600))
    registry.register("rs_1", task, session_id="s1", tenant_id="t1")
    try:
        assert await registry._cancel_and_ack(fake_redis, "rs_1", registry.REASON_USER) is True
        assert fake_redis.lists[affinity.ack_key("rs_1")] == ["1"]
        assert registry.reason_of("rs_1") == registry.REASON_USER
    finally:
        task.cancel()
        registry.unregister("rs_1")


@pytest.mark.asyncio
async def test_cancel_miss_writes_no_receipt(fake_redis):
    """没命中 → **不写回执**：网关等不到回执就会把作业判为 lost（而不是假成功）。"""
    assert await registry._cancel_and_ack(fake_redis, "rs_unknown", registry.REASON_USER) is False
    assert fake_redis.lists == {}


# ── 与 runner 的接入 ──


@pytest.mark.asyncio
async def test_runner_registers_then_releases_owner(monkeypatch):
    """run 期间归属存在，收尾后消失 —— 网关不该认为一个已结束的 run 还有人在跑。"""
    from app.agent import runtime as runtime_mod
    from app.agent.event_sink import EventSink
    from app.agent.subagent_runner import SubAgentRunner

    fake = FakeRedis()
    seen: list[bool] = []

    async def _fake_get_owner_redis():
        return fake

    async def _quick_run(self, task):  # noqa: ANN001
        # 执行期间必须已经有归属（否则取消找不到人）
        seen.append(affinity.owner_key("rs_probe") in fake.kv or bool(fake.kv))
        yield runtime_mod.AgentEvent(type="text", content="done")

    monkeypatch.setattr(affinity, "get_owner_redis", _fake_get_owner_redis)
    monkeypatch.setattr(runtime_mod.AgentRuntime, "run", _quick_run)

    runner = SubAgentRunner(
        store=None,
        sink=EventSink(),
        gateway=object(),
        parent_session_id="s1",
        tenant_id="t1",
        user_id="u1",
    )
    result = await runner.run("干点活", profile_ref="", mode="normal", max_turns=2)

    assert seen and seen[0] is True, "执行期间必须已经登记归属"
    assert fake.kv == {}, "收尾后必须注销归属（否则取消会被路由到不再持有它的实例）"
    assert result.run_id


@pytest.mark.asyncio
async def test_runner_survives_affinity_failure(monkeypatch):
    """归属登记失败（Redis 不可用）不能让子 Agent 起不来：降级为"无映射"。"""
    from app.agent import runtime as runtime_mod
    from app.agent.event_sink import EventSink
    from app.agent.subagent_runner import SubAgentRunner

    async def _broken():
        raise RuntimeError("redis down")

    async def _quick_run(self, task):  # noqa: ANN001
        yield runtime_mod.AgentEvent(type="text", content="done")

    monkeypatch.setattr(affinity, "get_owner_redis", _broken)
    monkeypatch.setattr(runtime_mod.AgentRuntime, "run", _quick_run)

    runner = SubAgentRunner(store=None, sink=EventSink(), gateway=object(),
                            parent_session_id="s1", tenant_id="t1", user_id="u1")
    result = await runner.run("干点活", profile_ref="", mode="normal", max_turns=2)
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_registry_stop_releases_owned_runs(monkeypatch):
    """关机时批量注销：进程要走了，键留着只会让网关以为"还有人跑"。"""
    released: list[list[str]] = []

    async def _fake_release(run_ids):
        released.append(list(run_ids))
        return len(run_ids)

    monkeypatch.setattr(affinity, "release_owners", _fake_release)

    task = asyncio.create_task(asyncio.sleep(3600))
    registry.register("rs_shutdown", task, session_id="s1")
    try:
        await registry.stop()
        assert released == [["rs_shutdown"]]
    finally:
        task.cancel()
        registry.unregister("rs_shutdown")
