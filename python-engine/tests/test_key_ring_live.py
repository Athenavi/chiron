"""KeyRing V2 的两段 Lua 脚本打真 Redis 的行为验证（integration）。

V1 的缺陷全在原子性/服务端记账上，fake 无法忠实模拟 cjson 与 EVAL 的读-改-写，
所以这里必须连真实 Redis。用 CHIRON_TEST_REDIS_ADDR（如 127.0.0.1:6379）或 settings.redis_url；
连不上即 skip。测试键都落在随机 provider 名下并自清理，绝不碰真实数据。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import pytest

# 用 `live` 而非 `integration`：这两段 Lua 只需要 Redis，不该被 conftest 里那个
# "integration ⇒ 必须有 PostgreSQL" 的 autouse fixture 拦下。CI 的 real-stack job 有真
# Redis，其 pytest 选择表达式 `-m "integration or live"` 会把本文件跑起来；无 Redis 的环境
# （如纯单测 job）里 `_addr()` 直接 skip，不发起连接。
pytestmark = pytest.mark.live

aioredis = pytest.importorskip("redis.asyncio")

from app.gateway.key_ring import (  # noqa: E402
    COOLDOWN_HARD,
    COOLDOWN_SOFT,
    FAIL_THRESHOLD,
    LLM_KEY_HASH_PREFIX,
    KeyRing,
    _key_id,
)


def _addr() -> tuple[str, int]:
    raw = (os.getenv("CHIRON_TEST_REDIS_ADDR") or "").strip()
    if not raw:
        from app.config import settings

        url = (settings.redis_url or "").strip()
        if not url:
            pytest.skip("未配置 Redis：integration 用例需要真实 Redis")
        # redis://host:port/0
        raw = url.split("://", 1)[-1].split("/", 1)[0]
    host, _, port = raw.partition(":")
    return host, int(port or 6379)


@pytest.fixture
async def redis():
    host, port = _addr()
    client = aioredis.Redis(host=host, port=port, decode_responses=True, socket_timeout=3)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001 - 连不上就跳过，而非判失败
        await client.aclose()
        pytest.skip(f"Redis 不可达（{host}:{port}）：{exc}")
    yield client
    await client.aclose()


@pytest.fixture
async def provider(redis):
    """随机 provider 名 ⇒ 键 llm:keys:pytest-<uuid> 与真实数据物理隔离；用完删。"""
    name = f"pytest-{uuid.uuid4().hex[:12]}"
    yield name
    await redis.delete(f"{LLM_KEY_HASH_PREFIX}{name}")


def _field(name: str, key: str) -> str:
    return _key_id(name, key)


async def _seed(redis, name: str, mapping: dict[str, dict[str, Any]]) -> None:
    payload = {digest: json.dumps(obj) for digest, obj in mapping.items()}
    await redis.hset(f"{LLM_KEY_HASH_PREFIX}{name}", mapping=payload)


async def _get_field(redis, name: str, digest: str) -> dict[str, Any]:
    raw = await redis.hget(f"{LLM_KEY_HASH_PREFIX}{name}", digest)
    return json.loads(raw) if raw else {}


# ── A1：原子 LRU 严格轮转 ──


async def test_pick_rotates_lru_across_keys(redis, provider):
    keys = [f"sk-{i}" for i in range(3)]
    await _seed(redis, provider, {_field(provider, k): {"k": k, "s": "active"} for k in keys})
    ring = KeyRing(redis=redis)
    picked = [(await ring.get_key(provider))["key"] for _ in range(6)]
    # 前 3 次各选一把（无重复），之后按最久未用继续轮转 → 6 次里每把恰好两次。
    assert set(picked[:3]) == set(keys)
    assert len(picked) == 6 and all(picked.count(k) == 2 for k in keys)
    # 绝不连续两次选同一把。
    assert all(picked[i] != picked[i + 1] for i in range(len(picked) - 1))


async def test_pick_skips_ghost_and_malformed(redis, provider):
    good = _field(provider, "sk-good")
    await _seed(
        redis,
        provider,
        {
            good: {"k": "sk-good", "s": "active"},
            _field(provider, ""): {"k": "", "s": "active"},  # 空 key 跳过
        },
    )
    await redis.hset(f"{LLM_KEY_HASH_PREFIX}{provider}", "broken", "{not json")
    ring = KeyRing(redis=redis)
    seen = {(await ring.get_key(provider))["key"] for _ in range(4)}
    assert seen == {"sk-good"}


# ── A2：时间窗自愈 + 管理员状态权威 ──


async def test_future_cooldown_excludes_then_self_heals(redis, provider):
    digest = _field(provider, "sk-1")
    now = time.time()
    await _seed(redis, provider, {digest: {"k": "sk-1", "s": "active", "c": now + 120}})
    ring = KeyRing(redis=redis)
    assert await ring.get_key(provider) is None  # 冷却中（c 在未来）
    # 把 c 挪到过去 ⇒ 同一个 active key 立即可用（引擎从不需要把 s 翻成 circuit_open）。
    await _seed(redis, provider, {digest: {"k": "sk-1", "s": "active", "c": now - 1}})
    assert (await ring.get_key(provider))["key"] == "sk-1"


@pytest.mark.parametrize("status", ["circuit_open", "rate_limited"])
async def test_admin_status_never_picked(redis, provider, status):
    # s 归管理员：非 active 一律硬排除，且 c 的存在与否不影响。
    digest = _field(provider, "sk-x")
    await _seed(redis, provider, {digest: {"k": "sk-x", "s": status}})
    assert await KeyRing(redis=redis).get_key(provider) is None


# ── A2：窗口化失败计数，达阈值由软冷却升级为硬冷却并复位窗口 ──


async def test_report_failure_escalates_at_threshold(redis, provider):
    digest = _field(provider, "sk-1")
    await _seed(redis, provider, {digest: {"k": "sk-1", "s": "active"}})
    ring = KeyRing(redis=redis)
    now = time.time()
    counts = []
    for _ in range(FAIL_THRESHOLD):
        await ring.report_failure(provider, "sk-1")
        counts.append(await _get_field(redis, provider, digest))
    first = counts[0]
    assert first["f"] == 1 and first["ft"] > 0
    assert first["c"] == pytest.approx(now + COOLDOWN_SOFT, abs=5)
    last = counts[-1]
    # 第 FAIL_THRESHOLD 次触发：硬冷却 60s + 窗口复位。
    assert last["c"] == pytest.approx(now + COOLDOWN_HARD, abs=8)
    assert last["f"] == 0 and last["ft"] == 0
    # 该 key 现在冷却中，取不到。
    assert await ring.get_key(provider) is None


async def test_fail_window_resets_after_staleness(redis, provider):
    digest = _field(provider, "sk-1")
    stale = time.time() - 10_000  # 远早于 FAIL_WINDOW
    await _seed(redis, provider, {digest: {"k": "sk-1", "s": "active", "f": 4, "ft": stale}})
    await KeyRing(redis=redis).report_failure(provider, "sk-1")
    obj = await _get_field(redis, provider, digest)
    # 窗口过期 ⇒ 计数从 1 重新起算，不是直接冲到阈值 5。
    assert obj["f"] == 1


async def test_report_failure_never_shortens_existing_cooldown(redis, provider):
    digest = _field(provider, "sk-1")
    now = time.time()
    long_until = now + 120
    await _seed(redis, provider, {digest: {"k": "sk-1", "s": "active", "c": long_until}})
    await KeyRing(redis=redis).report_failure(provider, "sk-1")  # 软冷却仅 30s
    obj = await _get_field(redis, provider, digest)
    # 30s < 已有的 120s ⇒ 保留更长的，绝不让冷却倒缩。
    assert obj["c"] == pytest.approx(long_until, abs=2)


# ── A1：跨实例共享一份 u ⇒ 两个 KeyRing 也能均衡 ──


async def test_two_instances_share_rotation(redis, provider):
    keys = [f"sk-{i}" for i in range(3)]
    await _seed(redis, provider, {_field(provider, k): {"k": k, "s": "active"} for k in keys})
    a, b = KeyRing(redis=redis), KeyRing(redis=redis)
    picked = []
    for _ in range(3):
        picked.append((await a.get_key(provider))["key"])
        picked.append((await b.get_key(provider))["key"])
    assert all(picked.count(k) == 2 for k in keys)  # 两个实例合计仍严格均衡


async def test_pick_writes_last_used_back(redis, provider):
    digest = _field(provider, "sk-1")
    await _seed(redis, provider, {digest: {"k": "sk-1", "s": "active"}})
    before = await _get_field(redis, provider, digest)
    assert "u" not in before
    await KeyRing(redis=redis).get_key(provider)
    after = await _get_field(redis, provider, digest)
    assert after["u"] == pytest.approx(time.time(), abs=5)
    assert after["k"] == "sk-1" and after["s"] == "active"  # 记账不破坏权威字段


# ── M27：每 key 权重（smooth WRR）──
#
# 现场：选择一直是纯 LRU 轮转（选 `u` 最小的），没有"某把 key 该多分流量"的表达能力。
# 权重来自 `llm_provider_keys.weight`（默认 1），只在**可用**的 key 之间分配。


async def test_equal_weights_rotate_strictly(redis, provider):
    """等权（默认 1）必须是**严格轮转** —— 这是"默认什么都不做"的落点：
    换算法不能改变"没人设权重时"的分配行为。"""
    keys = [f"sk-{i}" for i in range(3)]
    await _seed(redis, provider, {_field(provider, k): {"k": k, "s": "active"} for k in keys})
    picked = [(await KeyRing(redis=redis).get_key(provider))["key"] for _ in range(6)]

    assert set(picked[:3]) == set(keys)
    assert picked[:3] == picked[3:6], "等权 ⇒ 周期为 3 的严格轮转"


async def test_weight_shifts_the_ratio(redis, provider):
    """权重 2:1 ⇒ 3 次里高权重那把拿 2 次，且序列是 **A,B,A**（smooth WRR 无突发；
    朴素 WRR 会给出 A,A,B）。"""
    a, b = _field(provider, "sk-a"), _field(provider, "sk-b")
    await _seed(
        redis,
        provider,
        {a: {"k": "sk-a", "s": "active", "w": 2}, b: {"k": "sk-b", "s": "active", "w": 1}},
    )
    picked = [(await KeyRing(redis=redis).get_key(provider))["key"] for _ in range(3)]

    assert picked == ["sk-a", "sk-b", "sk-a"]


async def test_zero_weight_key_is_never_picked(redis, provider):
    """`w = 0` = 不参与选择（灰度摘流量），且**不计入 total** ——
    否则它会把其它 key 的 cw 分配拉偏。"""
    off, on = _field(provider, "sk-off"), _field(provider, "sk-on")
    await _seed(
        redis,
        provider,
        {
            off: {"k": "sk-off", "s": "active", "w": 0},
            on: {"k": "sk-on", "s": "active", "w": 1},
        },
    )
    picked = {(await KeyRing(redis=redis).get_key(provider))["key"] for _ in range(6)}

    assert picked == {"sk-on"}


async def test_cooldown_is_orthogonal_to_weight(redis, provider):
    """冷却与权重正交（LiteLLM 同一条纪律）：冷却中的 key 直接不参与，
    权重只在**可用**的 key 之间分配 —— 高权重也救不了一个正在冷却的 key。"""
    hot, cold = _field(provider, "sk-hot"), _field(provider, "sk-cold")
    await _seed(
        redis,
        provider,
        {
            hot: {"k": "sk-hot", "s": "active", "w": 1},
            cold: {"k": "sk-cold", "s": "active", "w": 100, "c": time.time() + 600},
        },
    )
    picked = {(await KeyRing(redis=redis).get_key(provider))["key"] for _ in range(5)}

    assert picked == {"sk-hot"}
