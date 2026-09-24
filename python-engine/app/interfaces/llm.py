"""
LLM Provider Protocol — 对标 Go 的 llm.Provider 接口
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol


class LLMResponse:
    """LLM 响应对象"""

    def __init__(
        self,
        content: str = "",
        tool_calls: list[dict] | None = None,
        usage: dict | None = None,
        finish_reason: str | None = None,
    ):
        self.content = content
        self.tool_calls = tool_calls or []
        self.usage = usage or {}
        self.finish_reason = finish_reason


class LLMProvider(Protocol):
    """LLM 提供者接口"""

    @property
    def name(self) -> str:
        """Provider 名称"""
        ...

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        stream: bool = True,
    ) -> AsyncIterator[dict] | LLMResponse:
        """发送聊天请求，返回流式响应或完整响应"""
        ...

    async def embed(
        self,
        text: str,
        model: str | None = None,
    ) -> list[float]:
        """生成文本嵌入向量"""
        ...

    async def close(self) -> None:
        """关闭连接"""
        ...
