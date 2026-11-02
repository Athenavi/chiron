"""分布式子 Agent 配额（设计 §9.1）。

这些断言锁定的是**拒绝语义**，不是功能完整性：

1. 三层键名逐字稳定 —— Go 侧若也要读同一批键，改一个字符就失效；
2. 超限必须分得出是哪一层（父模型据此决定改策略还是干脆自己做）；
3. 崩溃遗留的僵尸槽位会被清掉 —— 否则一次 OOM 就能让该会话永久无法委派；
4. Redis 不可用时**放行** —— 配额是保护，不该退化成"所有委派一律失败"。

⚠ 这里的 fake 用 Python 复刻了 Lua 的 ZSET 语义，测的是 `quota.py` 的调用契约
（键名、参数顺序、返回值判定、异常兜底）。**Lua 脚本本身在真实 Redis 上的行为
未被验证** —— 本地无 Redis 集群时无法覆盖，需在预发环境补一次集成测试。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.subagent import quota


class _FakeRedis:
    """只实现配额用到的 ZSET 子集，语义对齐 Lua 脚本。"""

    def __init__(self, fail: bool = False) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.evals: list[tuple] = []
        self.fail = fail

    async def eval(self, script: str, numkeys: int, *rest: Any) -> Any:
        if self.fail:
            raise RuntimeError("redis down")
        keys = list(rest[:numkeys])
        args = list(rest[numkeys:])
        self.evals.append((keys, args))

        if "ZREMRANGEBYSCORE" in script:  # acquire
            now, lease_ms, member = args[0], args[1], args[2]
            limits = [int(args[3]), int(args[4]), int(args[5])]
            names = ["tenant", "session", "global"]

            for key in keys:
                self._purge(key, now)
            for i, key in enumerate(keys):
                if limits[i] > 0 and len(self.zsets.get(key, {})) >= limits[i]:
                    return names[i]
            for key in keys:
                self.zsets.setdefault(key, {})[member] = now + lease_ms
            return "ok"

        # release
        member = args[0]
        for key in keys:
            self.zsets.get(key, {}).pop(member, None)
        return 1

    def _purge(self, key: str, now: float) -> None:
        bucket = self.zsets.get(key)
        if not bucket:
            return
        for member, score in list(bucket.items()):
            if score <= now:
                del bucket[member]


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    redis = _FakeRedis()

    async def _get_redis() -> _FakeRedis:
        return redis

    monkeypatch.setattr("app.redis_client.get_redis", _get_redis)
    # 固定上限，避免宿主机的环境变量影响断言
    monkeypatch.setenv("SUBAGENT_QUOTA_PER_TENANT", "3")
    monkeypatch.setenv("SUBAGENT_QUOTA_PER_SESSION", "2")
    monkeypatch.setenv("SUBAGENT_QUOTA_GLOBAL", "5")
    return redis


def test_keys_are_stable():
    """键名逐字稳定：Go 侧可能用同一约定做观测。"""
    assert quota._keys(7, "s1") == ["subq:t:7", "subq:s:s1", "subq:g"]


def test_limits_read_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SUBAGENT_QUOTA_PER_TENANT", "11")
    monkeypatch.setenv("SUBAGENT_QUOTA_PER_SESSION", "22")
    monkeypatch.setenv("SUBAGENT_QUOTA_GLOBAL", "33")

    assert quota.limits() == (11, 22, 33)


def test_limits_fall_back_on_garbage(monkeypatch: pytest.MonkeyPatch):
    """坏值不能抛异常 —— 一个手误的配置不该让委派直接崩。"""
    monkeypatch.setenv("SUBAGENT_QUOTA_PER_TENANT", "not-a-number")

    t_lim, _, _ = quota.limits()
    assert t_lim == quota.DEFAULT_TENANT_LIMIT


@pytest.mark.asyncio
async def test_acquire_grants_when_under_limit(fake_redis: _FakeRedis):
    verdict = await quota.acquire(owner="sq_a", tenant_id=7, session_id="s1")

    assert verdict is None
    # 三层都要占：少一层就等于没限流
    assert set(fake_redis.zsets) == {"subq:t:7", "subq:s:s1", "subq:g"}
    assert all("sq_a" in bucket for bucket in fake_redis.zsets.values())


@pytest.mark.asyncio
async def test_session_layer_rejects_first(fake_redis: _FakeRedis):
    """会话层最紧（2），应先于租户层（3）触发。"""
    for i in range(2):
        assert await quota.acquire(owner=f"sq_{i}", tenant_id=7, session_id="s1") is None

    assert await quota.acquire(owner="sq_x", tenant_id=7, session_id="s1") == "session"


@pytest.mark.asyncio
async def test_tenant_layer_rejects_across_sessions(fake_redis: _FakeRedis):
    """会话配额是**每会话**的：换会话不该绕过租户总量。"""
    # 每个会话各占 1，避开会话层上限 2
    assert await quota.acquire(owner="sq_a", tenant_id=7, session_id="s1") is None
    assert await quota.acquire(owner="sq_b", tenant_id=7, session_id="s2") is None
    assert await quota.acquire(owner="sq_c", tenant_id=7, session_id="s3") is None

    assert await quota.acquire(owner="sq_d", tenant_id=7, session_id="s4") == "tenant"


@pytest.mark.asyncio
async def test_global_layer_rejects_across_tenants(fake_redis: _FakeRedis):
    """不同租户共享全局上限（5）。"""
    for i in range(5):
        # 租户各不相同，会话也各不相同，只可能撞上全局
        verdict = await quota.acquire(owner=f"sq_{i}", tenant_id=i, session_id=f"s{i}")
        assert verdict is None, f"第 {i} 个就被拒了，全局上限没生效"

    assert await quota.acquire(owner="sq_6", tenant_id=9, session_id="s9") == "global"


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed(fake_redis: _FakeRedis):
    """崩溃留下的僵尸槽位必须能被清掉。

    进程被 OOM kill 时 `finally` 跑不到。若过期租约不清理，该会话此后
    **永远**无法再委派 —— 这是比超发严重得多的故障。
    """
    stale = time.time() * 1000 - 1  # 已过期
    fake_redis.zsets["subq:s:s1"] = {"sq_dead": stale}
    fake_redis.zsets["subq:t:7"] = {"sq_dead": stale}
    fake_redis.zsets["subq:g"] = {"sq_dead": stale}

    # 会话上限 2，僵尸被清掉后应能正常占用
    assert await quota.acquire(owner="sq_live", tenant_id=7, session_id="s1") is None
    assert "sq_dead" not in fake_redis.zsets["subq:s:s1"]


@pytest.mark.asyncio
async def test_release_frees_all_three_layers(fake_redis: _FakeRedis):
    """释放必须三层一起摘 —— 漏一层就会在那一层持续泄漏。"""
    await quota.acquire(owner="sq_a", tenant_id=7, session_id="s1")
    await quota.release(owner="sq_a", tenant_id=7, session_id="s1")

    for key, bucket in fake_redis.zsets.items():
        assert "sq_a" not in bucket, f"{key} 里的槽位没释放"


@pytest.mark.asyncio
async def test_release_then_acquire_again_succeeds(fake_redis: _FakeRedis):
    """正常收尾后应能接着委派：这是"回收没写对"最直接的暴露方式。"""
    for i in range(2):
        assert await quota.acquire(owner=f"sq_{i}", tenant_id=7, session_id="s1") is None
    await quota.release(owner="sq_0", tenant_id=7, session_id="s1")

    assert await quota.acquire(owner="sq_new", tenant_id=7, session_id="s1") is None


@pytest.mark.asyncio
async def test_redis_failure_allows_delegation(monkeypatch: pytest.MonkeyPatch):
    """Redis 抖动不能让所有委派失败 —— 配额是保护，不是正确性依赖。"""
    broken = _FakeRedis(fail=True)

    async def _get_redis() -> _FakeRedis:
        return broken

    monkeypatch.setattr("app.redis_client.get_redis", _get_redis)
    monkeypatch.setenv("SUBAGENT_QUOTA_PER_SESSION", "1")

    assert await quota.acquire(owner="sq_a", tenant_id=7, session_id="s1") is None


@pytest.mark.asyncio
async def test_release_never_raises(monkeypatch: pytest.MonkeyPatch):
    """回收失败只告警：已经收尾的对话不该再被一个 Redis 错误打挂。"""
    broken = _FakeRedis(fail=True)

    async def _get_redis() -> _FakeRedis:
        return broken

    monkeypatch.setattr("app.redis_client.get_redis", _get_redis)

    await quota.release(owner="sq_a", tenant_id=7, session_id="s1")  # 不抛即通过


@pytest.mark.asyncio
async def test_missing_session_is_not_counted(fake_redis: _FakeRedis):
    """无 session 时不编造配额桶 —— 否则所有异常调用会挤进同一个键。"""
    assert await quota.acquire(owner="sq_a", tenant_id=7, session_id="") is None
    assert fake_redis.zsets == {}


def test_rejection_payload_tells_model_not_to_retry():
    """拒绝理由必须阻止父模型原样重试，否则就是制造死循环。"""
    payload = quota.rejection_payload("session", tenant_id=7, session_id="s1")

    assert payload["status"] == "rejected"
    assert payload["layer"] == "session"
    assert payload["retryable"] is False
    assert "retrying" in payload["error"].lower()
    # 要给出替代动作，而不只是宣判失败
    assert "yourself" in payload["error"].lower()
