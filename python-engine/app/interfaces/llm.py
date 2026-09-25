"""
LLM Provider Protocol — 对标 Go 的 llm.Provider 接口
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol


class LLMResponse:
    """LLM 响应对象"""

    def __init__(
        self,
        content: str = "",
        tool_calls: list[dict[str, Any]] | None = None,
        usage: dict[str, Any] | None = None,
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

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """发送聊天请求，返回流式响应。

        返回的是**异步生成器协议**（调用方 ``async for``），因此这里声明为普通 ``def``：
        写成 ``async def`` 会被当成协程，实现方的 ``yield`` 与调用处的 ``async for`` 都会报错。
        ``stream=False`` 时实现方也只是 yield 一个聚合结果，不是另返回 ``LLMResponse``。
        """
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
