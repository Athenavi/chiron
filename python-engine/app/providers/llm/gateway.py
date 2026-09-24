"""
LLM Gateway Provider — 适配 GatewayRouter 到 LLMProvider Protocol
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from app.gateway.provider import ChatMessage
from app.gateway.router import GatewayRouter
from app.interfaces.llm import LLMResponse

logger = logging.getLogger(__name__)


class GatewayLLMProvider:
    """LLM Gateway 适配器 — 实现 LLMProvider Protocol"""

    def __init__(
        self,
        gateway: GatewayRouter,
        default_model: str = "",
    ):
        self._gateway = gateway
        self._default_model = default_model

    @property
    def name(self) -> str:
        """Provider 名称"""
        return "gateway"

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        stream: bool = True,
    ) -> AsyncIterator[dict] | LLMResponse:
        """发送聊天请求"""
        # 转换消息格式
        chat_messages = [
            ChatMessage(
                role=m.get("role", "user"),
                content=m.get("content", ""),
                tool_call_id=m.get("tool_call_id", ""),
            )
            for m in messages
        ]

        use_model = model or self._default_model

        if stream:
            # 流式返回
            async for chunk in self._gateway.chat_stream(
                messages=chat_messages,
                model=use_model,
                tools=tools,
                max_tokens=max_tokens or 4096,
                temperature=temperature or 0.7,
            ):
                yield {
                    "content": chunk.content,
                    "tool_calls": (
                        [
                            {
                                "id": tc.id,
                                "name": tc.name,
                                "arguments": tc.arguments,
                            }
                            for tc in chunk.tool_calls
                        ]
                        if chunk.tool_calls
                        else []
                    ),
                    "finish_reason": chunk.finish_reason,
                    "usage": {
                        "prompt_tokens": chunk.input_tokens,
                        "completion_tokens": chunk.output_tokens,
                    },
                }
        else:
            # 非流式，收集所有响应
            content = ""
            tool_calls = []
            usage = {}
            finish_reason = ""

            async for chunk in self._gateway.chat_stream(
                messages=chat_messages,
                model=use_model,
                tools=tools,
                max_tokens=max_tokens or 4096,
                temperature=temperature or 0.7,
            ):
                content += chunk.content
                if chunk.tool_calls:
                    tool_calls.extend(
                        [
                            {
                                "id": tc.id,
                                "name": tc.name,
                                "arguments": tc.arguments,
                            }
                            for tc in chunk.tool_calls
                        ]
                    )
                if chunk.finish_reason:
                    finish_reason = chunk.finish_reason
                if chunk.input_tokens or chunk.output_tokens:
                    usage = {
                        "prompt_tokens": chunk.input_tokens,
                        "completion_tokens": chunk.output_tokens,
                    }

            yield {
                "content": content,
                "tool_calls": tool_calls,
                "usage": usage,
                "finish_reason": finish_reason,
            }

    async def embed(
        self,
        text: str,
        model: str | None = None,
    ) -> list[float]:
        """生成文本嵌入向量"""
        # 从 providers 中获取 openai provider 来做 embedding
        if "openai" in self._gateway._providers:
            resp = await self._gateway._providers["openai"].embed(text, model)
            return resp.embedding
        return []

    async def close(self) -> None:
        """关闭连接"""
        # GatewayRouter 不需要显式关闭
