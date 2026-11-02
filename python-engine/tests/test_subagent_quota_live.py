"""分布式子 Agent 配额在**真实 Redis** 上的行为。

`test_subagent_quota.py` 的 fake 用 Python 复刻了 ZSET 语义，测的是 `quota.py`
的调用契约（键名、参数顺序、返回值判定、异常兜底）。它测不到的三件事只有
真 Redis 能判，而这三条正是"进程被 OOM kill 后该会话还能不能继续委派"的实质：

1. Lua 脚本本身能否被 `EVAL` 执行（语法、KEYS/ARGV 索引、返回类型）；
2. `PEXPIRE` 是否真的设上了 TTL —— 崩溃时 `finally` 跑不到，槽位全靠它回收；
3. 过期租约在 `acquire` 时被 `ZREMRANGEBYSCORE` 清掉，槽位因此可被重新占用。

后者是整套设计的兜底承诺：宁可短暂超发，也不让一次崩溃把该会话永久堵死。
用 fake 验证它等于用同一个假设证明它自己，所以必须打真 Redis。
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from app.subagent import quota

_REDIS_URL = (
    os.getenv("CHIRON_TEST_REDIS_URL") or os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
)


@pytest.fixture
async def real_redis(monkeypatch: pytest.MonkeyPatch):
    """连真 Redis；连不上就按环境是否**声明**了 Redis 决定 skip 还是红。

    与 `test_line_ledger.py` 的取向一致：声明了 `REDIS_URL`（CI 的 real-stack job）
    却连不上，说明环境坏了，要红；没声明说明这台机器本来就不跑真 Redis。
    """
    aioredis = pytest.importorskip("redis.asyncio")
    client = aioredis.from_url(_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        if (os.getenv("REDIS_URL") or "").strip():
            pytest.fail(f"REDIS_URL 已设置但连不上（{_REDIS_URL}）：{exc}")
        pytest.skip(f"需要可达的 Redis（{_REDIS_URL}）才能跑配额的真脚本用例")

    async def _get_redis():
        return client

    # `quota.acquire` / `release` 内部是**函数内导入** `get_redis`，因此必须
    # 打在 `app.redis_client` 上才生效。
    monkeypatch.setattr("app.redis_client.get_redis", _get_redis)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def scope(real_redis):
    """每个用例一份隔离的 (tenant, session, 已占 owner 列表)。

    `subq:g` 是**全局**键，测试之间会互相影响，所以 teardown 必须把自己占的
    member 摘掉；租户/会话键按随机 id 天然隔离。
    """
    tenant_id = 900_000 + uuid.uuid4().int % 100_000
    session_id = f"sit{uuid.uuid4().hex[:12]}"
    owners: list[str] = []
    yield tenant_id, session_id, owners
    keys = quota._keys(tenant_id, session_id)
    for owner in owners:
        await real_redis.zrem(keys[2], owner)
    await real_redis.delete(keys[0], keys[1])


def _set_limits(monkeypatch: pytest.MonkeyPatch, tenant: int, session: int, glob: int) -> None:
    monkeypatch.setenv("SUBAGENT_QUOTA_PER_TENANT", str(tenant))
    monkeypatch.setenv("SUBAGENT_QUOTA_PER_SESSION", str(session))
    monkeypatch.setenv("SUBAGENT_QUOTA_GLOBAL", str(glob))


async def _acquire(scope, owners=None):
    tenant_id, session_id, recorded = scope
    owner = quota.new_owner_id()
    verdict = await quota.acquire(owner=owner, tenant_id=tenant_id, session_id=session_id)
    if verdict is None:
        (recorded if owners is None else owners).append(owner)
    return owner, verdict


async def test_lua_executes_and_grants(real_redis, scope, monkeypatch):
    """脚本在真 Redis 上能跑通，且三个键都记下了本 owner。

    这条看似平凡，但它同时验证了 KEYS/ARGV 的索引约定 —— `quota.py` 里
    `redis.eval(script, 3, *keys, now, lease, owner, t, s, g)` 的参数顺序一旦
    错位，fake 会照抄同样的错位，只有真 Redis 才暴露。
    """
    _set_limits(monkeypatch, 10, 10, 10)
    tenant_id, session_id, owners = scope

    owner, verdict = await _acquire(scope)

    assert verdict is None, f"首个子 agent 应被放行，实际被 {verdict} 拒绝"
    keys = quota._keys(tenant_id, session_id)
    for key in keys:
        assert await real_redis.zscore(key, owner) is not None, f"{key} 未记下租约"


async def test_session_layer_rejects_at_limit(real_redis, scope, monkeypatch):
    """会话层占满后必须拒绝，且回的是层名 `session`。

    父模型靠这个层名决定"稍后重试"还是"自己做"。返回一个笼统的失败会让它
    原样重试，而重试必然再被拒 —— 等于制造死循环。
    """
    _set_limits(monkeypatch, 10, 2, 10)

    assert (await _acquire(scope))[1] is None
    assert (await _acquire(scope))[1] is None
    _, verdict = await _acquire(scope)

    assert verdict == "session"


async def test_tenant_layer_reported_before_session(real_redis, scope, monkeypatch):
    """租户层更紧时先报租户 —— 三层按 tenant → session → global 的顺序判定。

    顺序影响父模型看到的理由：会话没满却报 session 会误导它去拆会话。
    """
    _set_limits(monkeypatch, 1, 10, 10)

    assert (await _acquire(scope))[1] is None
    _, verdict = await _acquire(scope)

    assert verdict == "tenant"


async def test_lease_sets_ttl_on_every_key(real_redis, scope, monkeypatch):
    """三个键都必须带上 TTL。

    漏设任何一个，进程崩溃后那个键里的成员就永不消失 —— ZSET 没有 TTL 兜底时
    只能靠下一次 acquire 的 ZREMRANGEBYSCORE，而 score 过期判断依赖租约本身正确。
    """
    _set_limits(monkeypatch, 10, 10, 10)
    monkeypatch.setenv("SUBAGENT_QUOTA_LEASE_SECONDS", "60")
    tenant_id, session_id, _ = scope

    await _acquire(scope)

    for key in quota._keys(tenant_id, session_id):
        ttl_ms = await real_redis.pttl(key)
        assert 0 < ttl_ms <= 60_000, f"{key} 的 TTL 异常：{ttl_ms}ms"


async def test_expired_lease_is_reclaimed(real_redis, scope, monkeypatch):
    """崩溃遗留的槽位在租约到期后自动让位。

    这是整套设计最重要的兜底：`finally` 在 OOM kill 时不会执行，若不回收，
    该会话此后再也起不了子 agent（永久堵死）。测试用 1 秒租约把等待压到最小。
    """
    _set_limits(monkeypatch, 10, 1, 10)
    monkeypatch.setenv("SUBAGENT_QUOTA_LEASE_SECONDS", "1")

    _, first = await _acquire(scope)
    assert first is None
    _, blocked = await _acquire(scope)
    assert blocked == "session", "租约未到期时应当占满"

    await asyncio.sleep(1.2)  # 等租约过期（Lua 里按毫秒比较 score）

    _, after_expiry = await _acquire(scope)
    assert after_expiry is None, "过期租约必须被 ZREMRANGEBYSCORE 清掉，否则会话永久堵死"


async def test_release_frees_slot(real_redis, scope, monkeypatch):
    """正常回收路径：release 后立即能再占。"""
    _set_limits(monkeypatch, 10, 1, 10)
    tenant_id, session_id, _ = scope

    owner = quota.new_owner_id()
    assert await quota.acquire(owner=owner, tenant_id=tenant_id, session_id=session_id) is None
    await quota.release(owner=owner, tenant_id=tenant_id, session_id=session_id)

    for key in quota._keys(tenant_id, session_id):
        assert await real_redis.zscore(key, owner) is None, f"{key} 残留了已释放的 owner"
    assert await quota.acquire(
        owner=quota.new_owner_id(), tenant_id=tenant_id, session_id=session_id
    ) is None


async def test_release_is_idempotent(real_redis, scope, monkeypatch):
    """重复 release 不抛：收尾路径可能被调用两次，抛错会顶掉原始异常。"""
    _set_limits(monkeypatch, 10, 1, 10)
    tenant_id, session_id, _ = scope

    owner = quota.new_owner_id()
    await quota.acquire(owner=owner, tenant_id=tenant_id, session_id=session_id)
    await quota.release(owner=owner, tenant_id=tenant_id, session_id=session_id)
    await quota.release(owner=owner, tenant_id=tenant_id, session_id=session_id)
    # 再 release 一个从未占用过的 owner 也不该抛
    await quota.release(owner="sq_never_used", tenant_id=tenant_id, session_id=session_id)
