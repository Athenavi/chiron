# 三级语义缓存: L1 进程内 LRU + L2 Redis 精确 + L3 Redis 语义
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from typing import Any

import redis.asyncio as aioredis

from app.gateway.provider import ChatMessage, ChatResponse
from app.redis_keys import rkey

logger = logging.getLogger(__name__)


class SemanticCache:
    """
    L1: 进程内 LRU (2048 条, <0.1ms)
    L2: Redis 精确匹配 (TTL 3600s, ~1ms)
    L3: Redis 语义匹配 (embedding cosine > threshold, ~5ms)

    仅对「无工具调用」的纯文本请求启用。
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        embed_fn: Any,  # Callable[[str], Awaitable[list[float]]]
        l1_capacity: int = 2048,
        l2_ttl: int = 3600,
        semantic_threshold: float = 0.95,
        semantic_prefix_dims: int = 64,
    ) -> None:
        self._redis = redis
        self._embed_fn = embed_fn
        self._l2_ttl = l2_ttl
        self._semantic_threshold = semantic_threshold
        self._semantic_prefix_dims = semantic_prefix_dims

        # L1: LRU dict
        self._l1: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._l1_capacity = l1_capacity

        # 统计
        self._hits_l1 = 0
        self._hits_l2 = 0
        self._hits_l3 = 0
        self._misses = 0

    # ── key 计算 ──

    @staticmethod
    def _exact_key(
        tenant_id: int,
        model: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """精确缓存 key: hash(tenant + model + messages + tools + temperature
        [+ response_format])

        tenant 必须参与哈希。少了它，两个不同租户只要 prompt 相同就会互相命中 ——
        后提问的那个会拿到对方回复的**完整内容**（含对方上下文里的私有事实），
        并被写进自己的对话历史。L2/L3 走共享 Redis，多副本下影响等同全站。

        ``response_format`` 也必须参与哈希，理由不是隐私而是**形状**：同一段 prompt
        先以纯文本、后以 ``json_object`` 请求时，若 key 相同，后者会命中前者并拿到
        根本不能 JSON.parse 的字符串（调用方按 JSON 解析 → 直接失败）。

        只在**非空**时才注入 payload：``response_format is None`` 的既有调用方
        （Agent / 工作流 / 对话）哈希结果与加这个参数之前**逐字节相同**，存量 L2/L3
        条目继续命中，不需要任何迁移。这条「默认不改变」的约束有单测守着
        （``tests/test_desktop_infer.py::test_cache_key_is_unchanged_without_response_format``）。
        """
        payload: dict[str, Any] = {
            "tenant": tenant_id,
            "model": model,
            "messages": [m.to_dict() for m in messages],
            "tools": tools,
            "temperature": temperature,
        }
        if response_format is not None:
            # 归一再哈希：dict 的插入顺序不该产生两个 key（否则缓存命中率白掉一截）。
            payload["response_format"] = json.dumps(
                response_format, sort_keys=True, ensure_ascii=False
            )
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    async def _semantic_key(
        self,
        tenant_id: int,
        messages: list[ChatMessage],
        model: str,
        response_format: dict[str, Any] | None = None,
    ) -> str | None:
        """语义缓存 key: embedding 前 64 维量化 → hash（含租户维度与输出形状）

        语义缓存的跨租户命中概率**远高于**精确缓存 —— 它命中的是"意思相近"
        而非"完全相同"。所以租户维度必须进哈希，不能只在存储层做过滤。

        ``response_format`` 同理：语义命中只保证"意思相近"，对输出的**形状**毫无
        保证，所以形状必须进 key，而不能指望命中后再补救。同样只在非空时注入，
        存量语义 key 不变。
        """
        try:
            # 取最后 3 轮对话作为语义摘要
            recent = messages[-6:] if len(messages) > 6 else messages
            # 含多模态 parts（图片）的对话不进语义缓存：`m.content` 是块数组，拼进
            # 摘要文本会把整段 base64 送去 embedding（既无意义又昂贵），而且"换一张图"
            # 在语义上高度相似 —— 命中反而是错的。
            if any(not isinstance(m.content, str) for m in recent):
                return None
            text = "\n".join(f"{m.role}:{m.content}" for m in recent if m.content)
            if len(text) < 10:
                return None

            embedding = await self._embed_fn(text)
            if not embedding:
                return None

            # 取前 N 维，量化为 int8
            prefix = embedding[: self._semantic_prefix_dims]
            quantized = bytes(max(0, min(255, int((v + 1) * 127.5))) for v in prefix)
            shape = (
                json.dumps(response_format, sort_keys=True, ensure_ascii=False).encode()
                if response_format is not None
                else b""
            )
            h = hashlib.sha256(
                quantized + str(tenant_id).encode() + model.encode() + shape
            ).hexdigest()[:16]
            return h
        except Exception as e:
            logger.debug("Semantic key computation failed: %s", e)
            return None

    # ── 查找 ──

    async def lookup(
        self,
        tenant_id: int,
        model: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResponse | None:
        """按 L1 → L2 → L3 顺序查找缓存（tenant_id 必填，理由见 _exact_key）

        ``response_format`` 是**尾随可选**参数：既有调用方的位置调用与语义都不变。
        """
        # 不缓存带工具调用的请求
        if tools:
            return None

        exact = self._exact_key(
            tenant_id, model, messages, tools, temperature, response_format
        )

        # L1
        if exact in self._l1:
            self._l1.move_to_end(exact)
            self._hits_l1 += 1
            data = self._l1[exact]
            return self._decode(data, model)

        # L2
        raw = await self._redis.get(rkey(f"llm:cache:{exact}"))
        if raw:
            data = json.loads(raw)
            self._l1_set(exact, data)
            self._hits_l2 += 1
            return self._decode(data, model)

        # L3
        sem_key = await self._semantic_key(
            tenant_id, messages, model, response_format
        )
        if sem_key:
            bucket_key = rkey(f"llm:semantic:{sem_key[:8]}")
            cached = await self._redis.hget(bucket_key, sem_key)
            if cached:
                data = json.loads(cached)
                self._l1_set(exact, data)
                self._hits_l3 += 1
                return self._decode(data, model)

        self._misses += 1
        return None

    # ── 存储 ──

    async def store(
        self,
        tenant_id: int,
        model: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        response: ChatResponse,
        response_format: dict[str, Any] | None = None,
    ) -> None:
        """存储到 L1 + L2 + L3（tenant_id 必填，理由见 _exact_key）

        ``response_format`` 放在 ``response`` **之后**：既有调用点按位置传 6 个参数，
        把新参数插在中间会让它们把响应对象喂进 response_format。
        """
        if tools:
            return

        exact = self._exact_key(
            tenant_id, model, messages, tools, temperature, response_format
        )
        data = self._encode(response)

        # L1
        self._l1_set(exact, data)

        # L2
        await self._redis.set(
            rkey(f"llm:cache:{exact}"),
            json.dumps(data, ensure_ascii=False),
            ex=self._l2_ttl,
        )

        # L3 (语义)
        sem_key = await self._semantic_key(
            tenant_id, messages, model, response_format
        )
        if sem_key:
            bucket_key = rkey(f"llm:semantic:{sem_key[:8]}")
            await self._redis.hset(
                bucket_key, sem_key, json.dumps(data, ensure_ascii=False)
            )
            await self._redis.expire(bucket_key, self._l2_ttl)

    # ── 内部 ──

    def _l1_set(self, key: str, data: dict[str, Any]) -> None:
        self._l1[key] = data
        self._l1.move_to_end(key)
        while len(self._l1) > self._l1_capacity:
            self._l1.popitem(last=False)

    @staticmethod
    def _encode(resp: ChatResponse) -> dict[str, Any]:
        return {
            "content": resp.content,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in resp.tool_calls
            ],
            "finish_reason": resp.finish_reason,
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
            "cached_at": time.time(),
        }

    @staticmethod
    def _decode(data: dict[str, Any], model: str) -> ChatResponse:
        from app.gateway.provider import ToolCall

        return ChatResponse(
            content=data.get("content", ""),
            tool_calls=[
                ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                for tc in data.get("tool_calls", [])
            ],
            finish_reason=data.get("finish_reason", ""),
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            model=model,
            provider="cache",
        )

    def stats(self) -> dict[str, Any]:
        total = self._hits_l1 + self._hits_l2 + self._hits_l3 + self._misses
        return {
            "l1_hits": self._hits_l1,
            "l2_hits": self._hits_l2,
            "l3_hits": self._hits_l3,
            "misses": self._misses,
            "hit_rate": (self._hits_l1 + self._hits_l2 + self._hits_l3) / max(total, 1),
        }
