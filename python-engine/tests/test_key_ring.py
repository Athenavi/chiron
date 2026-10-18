"""KeyRing V2 的非 Lua 逻辑单测（不需要 Redis）。

Lua 脚本本身（原子 LRU 选取、时间窗自愈、窗口化失败计数）只在
``tests/test_key_ring_live.py`` 打真 Redis 验；这里覆盖调用它们的 Python 一侧：
env 兜底的 LRU 轮转与软冷却、可用谓词、`-anthropic` 源回退、EVAL 抛错/返空时的降级。
"""
from __future__ import annotations

import json
import time
from typing import Any

import pytest

from app.gateway.key_ring import COOLDOWN_SOFT, KeyRing, _key_id


class _FakeRedis:
    """只实现 KeyRing 用到的 hgetall / eval；eval 的行为按用例脚本来。"""

    def __init__(
        self,
        hash_data: dict[str, dict[str, Any]] | None = None,
        eval_results: list[Any] | None = None,
        eval_raises: bool = False,
    ) -> None:
        self.hash_data = hash_data or {}  # key -> {digest: payload_json_str}
        self.eval_results = eval_results if eval_results is not None else [None]
        self.eval_raises = eval_raises
        self.eval_calls: list[tuple[str, tuple]] = []
        self._i = 0

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hash_data.get(key, {}))

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        key = args[0]
        self.eval_calls.append((key, args[1:]))
        if self.eval_raises:
            raise ConnectionError("redis down")
        result = self.eval_results[min(self._i, len(self.eval_results) - 1)]
        self._i += 1
        if isinstance(result, Exception):
            raise result
        return result


# ── 可用谓词 _usable ──


def test_usable_active_no_cooldown():
    assert KeyRing._usable({"k": "sk-1", "s": "active"}, 1000.0) == ("sk-1", 0.0)


def test_usable_missing_status_defaults_active():
    # Go 写入的 payload 没有 s 时按 active 处理（缺省即放行）。
    assert KeyRing._usable({"k": "sk-1"}, 1000.0) == ("sk-1", 0.0)


def test_usable_manual_status_excluded():
    # 引擎从不改 s；管理员置 rate_limited/circuit_open 必须硬排除（不看 c）。
    assert KeyRing._usable({"k": "x", "s": "rate_limited", "c": 0}, 1000.0) is None
    assert KeyRing._usable({"k": "x", "s": "circuit_open", "c": 100}, 1000.0) is None


def test_usable_cooldown_blocks_then_self_heals():
    # c 是到期时间戳：未到期排除，到期即自愈（A2 的核心修复）。
    assert KeyRing._usable({"k": "x", "s": "active", "c": 2000.0}, 1000.0) is None
    assert KeyRing._usable({"k": "x", "s": "active", "c": 500.0}, 1000.0) == ("x", 500.0)


def test_usable_empty_key_rejected():
    assert KeyRing._usable({"k": "", "s": "active"}, 1000.0) is None


# ── 源回退 _sources ──


def test_sources_plain():
    assert KeyRing._sources("openai") == ["openai"]


def test_sources_anthropic_fallback():
    assert KeyRing._sources("opencode-go-anthropic") == ["opencode-go-anthropic", "opencode-go"]


# ── active_keys：Redis 镜像优先、按谓词过滤 ──


async def test_active_keys_filters_by_predicate():
    now = time.time()
    data = {
        "llm:keys:openai": {
            "good": json.dumps({"k": "sk-good", "s": "active"}),
            "cooling": json.dumps({"k": "sk-cool", "s": "active", "c": now + 60}),
            "revoked": json.dumps({"k": "sk-rev", "s": "circuit_open"}),
            "broken": "{not json",
        }
    }
    ring = KeyRing(redis=_FakeRedis(hash_data=data))
    keys = await ring.active_keys("openai")
    assert [k["key"] for k in keys] == ["sk-good"]


async def test_active_keys_falls_to_env_when_redis_empty():
    ring = KeyRing(redis=_FakeRedis(hash_data={}), env_seeds={"openai": ["sk-env"]})
    keys = await ring.active_keys("openai")
    assert keys == [{"key": "sk-env", "id": _key_id("openai", "sk-env")}]


async def test_active_keys_anthropic_reads_base_source():
    # provider=X-anthropic 空镜像 → 回退读 X 的 keyset（与 _resolve_client 的静默假 key 同源问题）。
    data = {"llm:keys:base": {"d": json.dumps({"k": "sk-base", "s": "active"})}}
    ring = KeyRing(redis=_FakeRedis(hash_data=data))
    keys = await ring.active_keys("base-anthropic")
    assert [k["key"] for k in keys] == ["sk-base"]


# ── get_key：Redis 取到即返回；EVAL 返空/抛错 → env 兜底 ──


async def test_get_key_returns_redis_pick_and_decodes():
    fake = _FakeRedis(eval_results=[["dig", "sk-picked"]])
    ring = KeyRing(redis=fake, env_seeds={"openai": ["sk-env"]})
    item = await ring.get_key("openai")
    assert item == {"key": "sk-picked", "id": "dig"}
    # 只对基础 provider 打了一次 EVAL（env 不参与热路径）。
    assert [c[0] for c in fake.eval_calls] == ["llm:keys:openai"]


async def test_get_key_redis_empty_falls_back_to_env():
    ring = KeyRing(redis=_FakeRedis(eval_results=[None]), env_seeds={"openai": ["sk-env"]})
    assert await ring.get_key("openai") == {"key": "sk-env", "id": _key_id("openai", "sk-env")}


async def test_get_key_redis_down_falls_back_to_env():
    ring = KeyRing(redis=_FakeRedis(eval_raises=True), env_seeds={"openai": ["sk-env"]})
    assert await ring.get_key("openai") == {"key": "sk-env", "id": _key_id("openai", "sk-env")}


# ── env 兜底的 LRU 轮转（A1 的单机等价修复）──


async def test_env_pick_round_robin_lru():
    keys = ["sk-1", "sk-2", "sk-3"]
    ring = KeyRing(redis=None, env_seeds={"openai": keys})
    seen = [(await ring.get_key("openai"))["id"] for _ in range(4)]
    first_three = set(seen[:3])
    assert first_three == {_key_id("openai", k) for k in keys}, "前三次应各选一把"
    assert seen[3] == seen[0], "第四次应回到最久未用（第一把）"


async def test_env_pick_none_when_no_seeds():
    ring = KeyRing(redis=None, env_seeds={})
    assert await ring.get_key("openai") is None


# ── report_failure：Redis 未命中该字段则换源；进程内软冷却 ──


async def test_report_failure_anthropic_stamps_base_field():
    # 上游 key 存在基础 provider X 下：X-anthropic 的 EVAL 返 -1，应再打 X。
    fake = _FakeRedis(eval_results=[-1, 1])
    ring = KeyRing(redis=fake)
    await ring.report_failure("base-anthropic", "sk-real")
    called = [c[0] for c in fake.eval_calls]
    assert called == ["llm:keys:base-anthropic", "llm:keys:base"]


async def test_report_failure_no_redis_applies_env_soft_cooldown():
    ring = KeyRing(redis=None, env_seeds={"openai": ["sk-only"]})
    await ring.report_failure("openai", "sk-only")
    # 唯一一把进了 30s 冷却 → 暂无可用（get_key 返 None）。
    assert ring._env_cool[_key_id("openai", "sk-only")] > time.time()
    assert await ring.get_key("openai") is None
    # 冷却窗口写的是 now+COOLDOWN_SOFT（允许毫秒级误差）。
    assert abs(ring._env_cool[_key_id("openai", "sk-only")] - (time.time() + COOLDOWN_SOFT)) < 5.0


async def test_report_failure_no_redis_does_not_cool_other_keys():
    ring = KeyRing(redis=None, env_seeds={"openai": ["sk-a", "sk-b"]})
    await ring.report_failure("openai", "sk-a")
    item = await ring.get_key("openai")
    assert item == {"key": "sk-b", "id": _key_id("openai", "sk-b")}


@pytest.mark.parametrize("exc", [ConnectionError("boom"), TimeoutError("slow")])
async def test_get_key_swallows_eval_exceptions_per_source(exc):
    # 某源 EVAL 抛错应被吞掉并继续下一源/退 env，而不是打断调用方。
    fake = _FakeRedis(eval_results=[exc])
    ring = KeyRing(redis=fake, env_seeds={"openai": ["sk-env"]})
    assert await ring.get_key("openai") == {"key": "sk-env", "id": _key_id("openai", "sk-env")}
