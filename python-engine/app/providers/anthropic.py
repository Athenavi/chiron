# Anthropic Provider — 使用 anthropic SDK
from __future__ import annotations

import logging
from typing import AsyncIterator

import anthropic

from app.gateway.provider import (ChatMessage, ChatResponse, EmbeddingResponse,
                                  LLMProvider, ToolCall)

logger = logging.getLogger(__name__)


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, base_url: str = "", *, key_ring=None):
        from app.config import settings as _settings  # 延迟导入：避免 providers ← config 循环

        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        # 具名 UA：部分网关前置 Cloudflare 反滥用（如 opencode.ai），SDK 默认 UA 会 403。
        kwargs["default_headers"] = {"User-Agent": _settings.llm_http_user_agent}
        self._base_kwargs = dict(kwargs)
        self._client = anthropic.AsyncAnthropic(**kwargs)
        # 多 key(DR 集中派),与 OpenAIProvider 同型;key_ring 为 None 时单 key 直连。
        self._key_ring = key_ring
        self._clients: dict[str, anthropic.AsyncAnthropic] = {}

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[ChatResponse]:
        system_prompt, msgs = self._convert_messages(messages)
        kwargs: dict = {
            "model": model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        # OpenCode 的 Anthropic 协议端点同样强制要求 x-opencode-session
        # （缺失即 400 MissingSessionID，见 docs/service-providers.md）；用当前会话 ID 注入。
        if self.name.startswith("opencode"):
            from app.providers.session_context import get_llm_session_id  # 延迟导入：避免循环

            session_id = get_llm_session_id()
            if session_id:
                kwargs["extra_headers"] = {"x-opencode-session": session_id}
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = self._convert_tools(tools)

        input_tokens = 0
        output_tokens = 0
        client, key_item = await self._resolve_client()
        try:
            async with client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    if event.type == "content_block_delta":
                        delta = event.delta
                        if hasattr(delta, "text"):
                            yield ChatResponse(content=delta.text)
                        elif hasattr(delta, "partial_json"):
                            # tool_use delta — accumulate in parent
                            pass
                    elif event.type == "message_start":
                        usage = getattr(event.message, "usage", None)
                        if usage:
                            input_tokens = getattr(usage, "input_tokens", 0)
                    elif event.type == "message_delta":
                        usage = getattr(event, "usage", None)
                        if usage:
                            output_tokens = getattr(usage, "output_tokens", 0)

                # 获取完整消息以提取 tool_calls
                final = await stream.get_final_message()
                tool_calls = []
                if final.content:
                    for block in final.content:
                        if block.type == "tool_use":
                            tool_calls.append(
                                ToolCall(
                                    id=block.id,
                                    name=block.name,
                                    arguments=(
                                        block.input
                                        if isinstance(block.input, str)
                                        else __import__("json").dumps(block.input)
                                    ),
                                )
                            )
                finish = "tool_calls" if tool_calls else "stop"
                if final.stop_reason == "max_tokens":
                    finish = "length"

                yield ChatResponse(
                    tool_calls=tool_calls,
                    finish_reason=finish,
                    input_tokens=input_tokens or getattr(final.usage, "input_tokens", 0),
                    output_tokens=output_tokens or getattr(final.usage, "output_tokens", 0),
                )
        except Exception:
            await self._report_failure(key_item)
            raise

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
    ) -> ChatResponse:
        import json as _json

        system_prompt, msgs = self._convert_messages(messages)
        kwargs: dict = {
            "model": model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        # OpenCode 的 Anthropic 协议端点同样强制要求 x-opencode-session
        # （缺失即 400 MissingSessionID，见 docs/service-providers.md）；用当前会话 ID 注入。
        if self.name.startswith("opencode"):
            from app.providers.session_context import get_llm_session_id  # 延迟导入：避免循环

            session_id = get_llm_session_id()
            if session_id:
                kwargs["extra_headers"] = {"x-opencode-session": session_id}
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = self._convert_tools(tools)

        client, key_item = await self._resolve_client()
        try:
            resp = await client.messages.create(**kwargs)
        except Exception:
            await self._report_failure(key_item)
            raise

        content = ""
        tool_calls = []
        for block in resp.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=(
                            block.input
                            if isinstance(block.input, str)
                            else _json.dumps(block.input)
                        ),
                    )
                )

        finish = "tool_calls" if tool_calls else "stop"
        if resp.stop_reason == "max_tokens":
            finish = "length"

        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )

    async def embed(self, text: str, model: str) -> EmbeddingResponse:
        # Anthropic 没有 embedding API，抛出异常让 Router 路由到 OpenAI
        raise NotImplementedError("Anthropic does not provide embedding API")

    async def close(self) -> None:
        clients = set(self._clients.values())
        clients.add(self._client)
        for c in clients:
            try:
                await c.close()
            except Exception:
                pass

    # ── 多 key helpers（与 OpenAIProvider 同型）──

    async def _resolve_client(self) -> tuple:
        if self._key_ring is None:
            return self._client, None
        item = await self._key_ring.get_key(self.name)
        if item is None:
            # ⚠️ 显式警告（此前是静默回退）：见 openai.py 同名处的说明 ——
            # 退到 placeholder client 会拿假 key 打上游并得到 401，
            # 而真正的 key 可能挂在同产品的另一个协议变体名下。
            logger.warning(
                "KeyRing 无可用 key for %s：回退到 placeholder client，上游很可能返回 401。"
                "请检查该 provider（或同产品基础 provider）的 keyset。",
                self.name,
            )
            return self._client, None
        key = item["key"]
        client = self._clients.get(key)
        if client is None:
            if len(self._clients) >= 8:
                self._clients.pop(next(iter(self._clients)))
            kwargs = dict(self._base_kwargs)
            kwargs["api_key"] = key
            client = anthropic.AsyncAnthropic(**kwargs)
            self._clients[key] = client
        return client, item

    async def _report_failure(self, item) -> None:
        if self._key_ring is not None and item is not None:
            try:
                await self._key_ring.report_failure(self.name, item["key"])
            except Exception:
                logger.exception("key ring report_failure failed")

    # ── 转换 ──

    @staticmethod
    def _convert_messages(messages: list[ChatMessage]) -> tuple[str, list[dict]]:
        """分离 system prompt 并转换消息格式"""
        system = ""
        result = []
        for msg in messages:
            if msg.role == "system":
                system = msg.content
            elif msg.role == "tool":
                result.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.tool_call_id,
                                "content": msg.content,
                            }
                        ],
                    }
                )
            elif msg.role == "assistant" and msg.tool_calls:
                content = []
                if msg.content:
                    content.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    import json as _json

                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": _json.loads(tc.arguments) if tc.arguments else {},
                        }
                    )
                result.append({"role": "assistant", "content": content})
            else:
                result.append({"role": msg.role, "content": msg.content})
        return system, result

    @staticmethod
    def _convert_tools(tools: list[dict]) -> list[dict]:
        """OpenAI tools 格式 → Anthropic tools 格式"""
        import json as _json

        converted = []
        for t in tools:
            func = t.get("function", t)
            converted.append(
                {
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "input_schema": func.get(
                        "parameters", _json.loads(func.get("parameters_json", "{}"))
                    ),
                }
            )
        return converted
