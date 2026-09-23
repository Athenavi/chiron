"""LLM 缓存键必须包含租户维度。

回归背景：``_exact_key`` 原先只哈希 ``(model, messages, tools, temperature)``，
``_semantic_key`` 只量化 prompt —— 都**没有** tenant。后果是两个不同租户只要
prompt 相同（语义缓存下"意思相近"即可）就会互相命中：

- 后提问的用户拿到前一个用户回复的**完整内容**（含其上下文里的私有事实）；
- 而且这条内容会被写进他自己的对话历史。

L2/L3 都走共享 Redis，多副本下影响等同于全站。所以租户维度必须进哈希，
不能只在存储层做过滤 —— 语义缓存的命中语义是"相似"，过滤晚一步就已经晚了。
"""

from __future__ import annotations

import pytest

from app.gateway.cache import SemanticCache
from app.gateway.provider import ChatMessage


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex=None):  # noqa: D102
        self.store[key] = value

    async def hget(self, key: str, field: str):
        return None

    async def hset(self, key: str, field: str, value: str):  # noqa: D102
        return 1

    async def expire(self, key: str, ttl: int):  # noqa: D102
        return True


def _cache() -> SemanticCache:
    async def _embed(text: str) -> list[float]:
        return [0.5] * 8

    return SemanticCache(redis=_FakeRedis(), embed_fn=_embed)


_MSGS = [ChatMessage(role="user", content="我的邮箱是 a@example.com，帮我看看")]


def test_exact_key_differs_by_tenant():
    """同样的 prompt，不同租户必须得到不同的精确键。"""
    k_a = SemanticCache._exact_key("tenant-a", "gpt-x", _MSGS, None, 0.7)
    k_b = SemanticCache._exact_key("tenant-b", "gpt-x", _MSGS, None, 0.7)
    assert k_a != k_b, "跨租户共用了精确缓存键：会读到别人的回复内容"
    # 同租户同请求仍然命中（缓存本身要有效）
    assert k_a == SemanticCache._exact_key("tenant-a", "gpt-x", _MSGS, None, 0.7)


@pytest.mark.asyncio
async def test_semantic_key_differs_by_tenant():
    """语义键同样必须区分租户 —— 它命中的是"相似"而非"相同"，风险更高。"""
    cache = _cache()
    k_a = await cache._semantic_key("tenant-a", _MSGS, "gpt-x")
    k_b = await cache._semantic_key("tenant-b", _MSGS, "gpt-x")
    assert k_a and k_b and k_a != k_b, "跨租户共用了语义缓存键"
    assert k_a == await cache._semantic_key("tenant-a", _MSGS, "gpt-x")


def test_tenant_id_is_required_on_public_api():
    """lookup/store 的 tenant_id 不设默认值 —— 否则又会退化成"忘了传就泄漏"。"""
    import inspect

    for fn in (SemanticCache.lookup, SemanticCache.store):
        params = inspect.signature(fn).parameters
        assert "tenant_id" in params, f"{fn.__name__} 缺少 tenant_id 参数"
        assert params["tenant_id"].default is inspect.Parameter.empty, (
            f"{fn.__name__}.tenant_id 必须必填，不能有默认值"
        )
