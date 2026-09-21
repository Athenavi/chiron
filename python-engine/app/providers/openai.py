# OpenAI Provider — 使用 openai SDK
from __future__ import annotations

import logging
from typing import AsyncIterator

from openai import AsyncOpenAI

from app.gateway.provider import (ChatMessage, ChatResponse, EmbeddingResponse,
                                  LLMProvider, ToolCall)

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str, base_url: str = "", *, key_ring=None):
        from app.config import settings as _settings  # 延迟导入：避免 providers ← config 循环

        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        # 具名 UA：部分网关前置 Cloudflare 反滥用（如 opencode.ai），SDK 默认 UA 会 403。
        kwargs["default_headers"] = {"User-Agent": _settings.llm_http_user_agent}
        self._base_kwargs = dict(kwargs)
        self._client = AsyncOpenAI(**kwargs)
        # 多 key(DR 集中派):key_ring 提供各 provider 的活跃 key 集;client 按 key 缓存。
        # key_ring 为空/不可用时的兜底 = self._client(env 首 key)。
        self._key_ring = key_ring
        self._clients: dict[str, AsyncOpenAI] = {}


    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[ChatResponse]:
        kwargs = self._build_kwargs(messages, model, max_tokens, temperature, tools)
        kwargs["stream"] = True

        client, key_item = await self._resolve_client()
        try:
            response = await client.chat.completions.create(**kwargs)
        except Exception:
            await self._report_failure(key_item)
            raise

        tool_calls: list[dict] = []
        input_tokens = 0
        output_tokens = 0
        reasoning_content = ""
        usage_reported = False  # usage 是否已随 finish chunk 发出（避免重复累加）

        async for chunk in response:
            if not chunk.choices:
                # 最后一个 chunk 可能只有 usage
                if chunk.usage:
                    input_tokens = chunk.usage.prompt_tokens
                    output_tokens = chunk.usage.completion_tokens
                continue

            delta = chunk.choices[0].delta

            # DeepSeek thinking mode：获取 reasoning_content（API 发送的是增量文本）
            if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                reasoning_content += delta.reasoning_content  # 累积完整思考文本
                yield ChatResponse(
                    reasoning_content=delta.reasoning_content
                )  # 传递增量

            if delta.content:
                yield ChatResponse(content=delta.content)

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    while len(tool_calls) <= tc.index:
                        tool_calls.append({"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        tool_calls[tc.index]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            tool_calls[tc.index]["name"] = tc.function.name
                        if tc.function.arguments:
                            tool_calls[tc.index]["arguments"] += tc.function.arguments

            finish = chunk.choices[0].finish_reason
            if finish:
                # 捕获 Token 计数（可能和 finish_reason 在同一 chunk）
                if chunk.usage:
                    input_tokens = chunk.usage.prompt_tokens
                    output_tokens = chunk.usage.completion_tokens
                parsed = [
                    ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                    for tc in tool_calls
                    if tc["id"]
                ]
                yield ChatResponse(
                    tool_calls=parsed,
                    finish_reason="tool_calls" if parsed else (finish or "stop"),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                usage_reported = input_tokens > 0 or output_tokens > 0

        # 发出 token 统计（usage chunk 可能没有 finish_reason；
        # 若已随 finish chunk 发出则跳过，避免调用方把用量累加两次）
        if not usage_reported and (input_tokens > 0 or output_tokens > 0):
            yield ChatResponse(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                finish_reason="stop",
            )

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
    ) -> ChatResponse:
        kwargs = self._build_kwargs(messages, model, max_tokens, temperature, tools)
        client, key_item = await self._resolve_client()
        try:
            resp = await client.chat.completions.create(**kwargs)
        except Exception:
            await self._report_failure(key_item)
            raise

        choice = resp.choices[0]
        content = choice.message.content or ""
        tool_calls = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=tc.id, name=tc.function.name, arguments=tc.function.arguments
                    )
                )

        usage = resp.usage
        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=(
                "tool_calls" if tool_calls else (choice.finish_reason or "stop")
            ),
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )

    async def embed(self, text: str, model: str) -> EmbeddingResponse:
        client, key_item = await self._resolve_client()
        try:
            resp = await client.embeddings.create(model=model, input=text)
        except Exception:
            await self._report_failure(key_item)
            raise
        usage = resp.usage
        return EmbeddingResponse(
            embedding=resp.data[0].embedding,
            tokens=usage.total_tokens if usage else 0,
        )

    async def close(self) -> None:
        clients = set(self._clients.values())
        clients.add(self._client)
        for c in clients:
            try:
                await c.close()
            except Exception:
                pass

    # ── helpers ──

    async def _resolve_client(self) -> tuple:
        """返回本次请求使用的 client 与其 key 指纹(可选)。
        有 key_ring 时按 provider 取活跃 key(按 key 缓存 client);取不到(无 key/异常)
        回退到 env 首 key 的 self._client(单 key/降级兼容)。"""
        if self._key_ring is None:
            return self._client, None
        item = await self._key_ring.get_key(self.name)
        if item is None:
            return self._client, None
        key = item["key"]
        client = self._clients.get(key)
        if client is None:
            if len(self._clients) >= 8:
                self._clients.pop(next(iter(self._clients)))  # 简单 LRU 上限
            kwargs = dict(self._base_kwargs)
            kwargs["api_key"] = key
            client = AsyncOpenAI(**kwargs)
            self._clients[key] = client
        return client, item

    async def _report_failure(self, item) -> None:
        if self._key_ring is not None and item is not None:
            try:
                await self._key_ring.report_failure(self.name, item["key"])
            except Exception:
                logger.exception("key ring report_failure failed")

    def _build_kwargs(
        self,
        messages: list[ChatMessage],
        model: str,
        max_tokens: int,
        temperature: float,
        tools: list[dict] | None,
    ) -> dict:
        kwargs: dict = {
            "model": model,
            "messages": [m.to_dict() if hasattr(m, "to_dict") else m for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        # OpenCode（Zen / Go）要求请求带 `x-opencode-session`，否则直接
        # 400 MissingSessionID: "Request is missing x-opencode-session and cannot be
        # routed efficiently"。该头只影响对方的路由与 prompt 缓存，所以用**同一会话的
        # 稳定 ID**：同对话请求走同一路由，prompt 缓存命中更好。
        # 见 docs/service-providers.md 的 OpenCode 一节。
        if self.name.startswith("opencode"):
            from app.providers.session_context import get_llm_session_id  # 延迟导入：避免循环

            session_id = get_llm_session_id()
            if session_id:
                kwargs["extra_headers"] = {"x-opencode-session": session_id}
        # ── 调试：检查消息序列中 tool 消息的配对 ──
        msgs = kwargs["messages"]
        for i, m in enumerate(msgs):
            if m.get("role") == "tool":
                prev = msgs[i - 1] if i > 0 else None
                if prev:
                    has_tc = bool(prev.get("tool_calls"))
                    logger.info(
                        "Tool msg #%d: prev role=%s has_tool_calls=%s tool_call_id=%s",
                        i,
                        prev.get("role"),
                        has_tc,
                        m.get("tool_call_id", ""),
                    )
        logger.info(
            "LLM call: model=%s msgs=%d roles=%s",
            model,
            len(msgs),
            [m.get("role") for m in msgs],
        )
        if tools:
            # 统一转为 OpenAI function 格式
            converted = []
            for t in tools:
                if "type" in t and t["type"] == "function":
                    converted.append(t)
                else:
                    converted.append({"type": "function", "function": t})
            kwargs["tools"] = converted
            kwargs["tool_choice"] = "auto"
        return kwargs
