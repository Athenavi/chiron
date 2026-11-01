"""M55a：KeyRing 取不到可用 key 时**绝不**拿占位 key 打上游。

回归的是这台机器上真实发生的事故：一次瞬时失败让唯一一把 key 进 30s 冷却，同轮下一次
调用取不到 key ⇒ 旧的静默回退用 `sk-chiron-keyset-managed` 构造的 client 发出请求 ⇒
上游回一句真的 401（掩码尾缀是占位 key 的），前端据此告诉用户"你的 API Key 无效"，
而那把 key 直连实测 200。

这些用例都不联网：只要代码还在"回退到 placeholder"，就会走网络或返回错误类型的 client。
"""
from __future__ import annotations

import json
import time
from typing import Any

import pytest

from app.gateway.key_ring import (
    COOLDOWN_SOFT,
    LLM_KEY_HASH_PREFIX,
    KeyRing,
    NoUsableKeyError,
    _key_id,
)
from app.providers.anthropic import AnthropicProvider
from app.providers.named import (
    NamedAnthropicProvider,
    NamedOpenAIProvider,
    NamedResponsesProvider,
)
from app.providers.openai import OpenAIProvider
from app.providers.responses import ResponsesProvider

PLACEHOLDER = "sk-chiron-keyset-managed"


class _EmptyRing:
    """get_key 永远取不到（冷却中/被停用/没配），diagnose 给出原因。"""

    def __init__(self, diag: dict[str, Any] | None = None) -> None:
        self.diag = {"configured": 1, "blocked": 0, "cooling": 1, "retry_after": 12.0} | (
            diag or {}
        )
        self.reported: list[str] = []

    async def get_key(self, provider: str) -> dict[str, Any] | None:
        return None

    async def diagnose(self, provider: str) -> dict[str, Any]:
        return dict(self.diag)

    async def report_failure(self, provider: str, key: str) -> None:
        self.reported.append(f"{provider}:{key}")


class _OneKeyRing(_EmptyRing):
    def __init__(self, key: str) -> None:
        super().__init__()
        self._key = key

    async def get_key(self, provider: str) -> dict[str, Any] | None:
        return {"key": self._key, "id": _key_id(provider, self._key)}


def _markers(exc: NoUsableKeyError) -> str:
    return str(exc)


# ── provider 层：取不到 key 就抛，不再回退 placeholder ──────────────────────


@pytest.mark.parametrize(
    "cls", [OpenAIProvider, AnthropicProvider, NamedOpenAIProvider, NamedAnthropicProvider]
)
async def test_no_usable_key_raises_instead_of_placeholder(cls: type) -> None:
    ring = _EmptyRing()
    kwargs: dict[str, Any] = {"api_key": PLACEHOLDER, "key_ring": ring}
    if cls in (NamedOpenAIProvider, NamedAnthropicProvider):
        provider = cls("deepseek", **kwargs)  # type: ignore[call-arg]
    else:
        provider = cls(**kwargs)  # type: ignore[call-arg]

    with pytest.raises(NoUsableKeyError) as got:
        await provider._resolve_client()  # type: ignore[attr-defined]

    assert "no_usable_key" in _markers(got.value), "前端按这个标记分类，不能退化成一句 401"
    assert "cooling=1" in _markers(got.value)
    assert "retry_after_seconds=12" in _markers(got.value)
    assert ring.reported == [], "取不到 key 不是任何一把 key 的错，不该再上报"


async def test_real_key_still_builds_a_key_scoped_client() -> None:
    """有可用 key 时行为不变：按那把 key 建 client（placeholder 不参与）。"""
    provider = NamedOpenAIProvider(
        "deepseek", api_key=PLACEHOLDER, key_ring=_OneKeyRing("sk-realsecretkey")
    )
    client, item = await provider._resolve_client()

    assert item is not None and item["key"] == "sk-realsecretkey"
    assert client.api_key == "sk-realsecretkey"


async def test_env_only_provider_without_ring_keeps_single_key_path() -> None:
    """没有 key_ring 的单 key 部署（env 种子）不受影响 —— 别把降级路径一起砍了。"""
    provider = NamedOpenAIProvider("deepseek", api_key="sk-env-seed")
    client, item = await provider._resolve_client()

    assert item is None
    assert client.api_key == "sk-env-seed"


async def test_openai_chat_never_sends_the_placeholder_upstream() -> None:
    """非流式同样 fail loud：`chat()` 必须抛 NoUsableKeyError，而不是发出一个请求。"""
    provider = NamedOpenAIProvider("deepseek", api_key=PLACEHOLDER, key_ring=_EmptyRing())

    with pytest.raises(NoUsableKeyError):
        await provider.chat([], "deepseek-flash")


async def test_responses_provider_uses_ring_and_reports_no_key() -> None:
    """具名 responses provider 以前**丢掉** key_ring ⇒ 每次都拿占位 key 打上游（必然误判）。"""
    provider = NamedResponsesProvider("opencode", api_key=PLACEHOLDER, key_ring=_EmptyRing())

    res = await provider.chat([], "gpt-5-mini")  # type: ignore[arg-type]

    assert res.finish_reason == "error"
    assert "no_usable_key" in (res.message or "")
    assert "401" not in (res.message or ""), "不许把内部的「没 key」包装成上游的鉴权判决"


async def test_responses_provider_without_ring_uses_seed_key() -> None:
    provider = ResponsesProvider(api_key="sk-env-seed")
    assert await provider._resolve_key() == ("sk-env-seed", None)


# ── diagnose：区分"没配" / "被停用" / "在冷却" ────────────────────────────


class _FakeRedis:
    def __init__(self, hash_data: dict[str, dict[str, str]]) -> None:
        self.hash_data = hash_data

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hash_data.get(key, {}))

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        return None


def _payload(**over: Any) -> str:
    base: dict[str, Any] = {"k": "sk-x", "s": "active", "w": 1, "c": 0}
    return json.dumps(base | over)


async def test_diagnose_counts_cooling_and_blocked_separately() -> None:
    now = time.time()
    ring_hash = f"{LLM_KEY_HASH_PREFIX}deepseek"
    redis_ = _FakeRedis(
        {
            ring_hash: {
                "d1": _payload(c=now + COOLDOWN_SOFT),
                "d2": _payload(s="circuit_open"),
                "d3": _payload(),
            }
        }
    )
    diag = await KeyRing(redis=redis_).diagnose("deepseek")

    assert diag["configured"] == 3
    assert diag["cooling"] == 1
    assert diag["blocked"] == 1
    assert COOLDOWN_SOFT - 1 <= diag["retry_after"] <= COOLDOWN_SOFT + 1


async def test_diagnose_on_empty_keyset_reports_configured_zero() -> None:
    diag = await KeyRing(redis=_FakeRedis({})).diagnose("deepseek")

    assert diag == {"configured": 0, "blocked": 0, "cooling": 0, "retry_after": 0.0}
