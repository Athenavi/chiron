# Gateway Provider 接口定义
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str = ""
    tool_call_id: str = ""
    tool_calls: list[ToolCall] | None = None

    def to_dict(self) -> dict:
        d: dict = {"role": self.role}
        # tool_calls 存在时 content 应为 None（OpenAI API 规范），但非空文本应保留
        if self.tool_calls:
            d["content"] = self.content or None
        else:
            d["content"] = self.content
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            d["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        return d


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON string

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class ChatResponse:
    content: str = ""
    reasoning_content: str = ""  # DeepSeek thinking mode 思考过程
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""  # "stop" | "tool_calls" | "length" | "error"
    message: str = ""  # finish_reason="error" 时的真实原因（可诊断）
    input_tokens: int = 0
    output_tokens: int = 0
    #: 命中提示词缓存（prompt cache）的输入 token 数 —— 会话统计里「缓存命中率」的分子。
    #: 各家字段名不同（OpenAI: prompt_tokens_details.cached_tokens、DeepSeek:
    #: prompt_cache_hit_tokens、Anthropic: cache_read_input_tokens），
    #: 由 provider 层用 cached_tokens_from_usage 归一化后填入。
    cached_tokens: int = 0
    provider: str = ""
    model: str = ""
    latency_ms: float = 0.0


def cached_tokens_from_usage(usage: object) -> int:
    """从各家 LLM 的 usage 对象里取「缓存命中」的输入 token 数。

    三家的字段名都不一样，直接属性访问在取不到时会抛 AttributeError，所以逐层
    getattr 探测。取不到就返回 0：统计口径宁可少算，也不要因为某个 provider
    换了字段名而让整条流式响应失败。
    """
    if usage is None:
        return 0
    # OpenAI 兼容（含 DeepSeek / OpenCode 等）：prompt_tokens_details.cached_tokens
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        value = getattr(details, "cached_tokens", None)
        if isinstance(value, int) and value > 0:
            return value
    # DeepSeek 直连：顶层 prompt_cache_hit_tokens
    value = getattr(usage, "prompt_cache_hit_tokens", None)
    if isinstance(value, int) and value > 0:
        return value
    # Anthropic：cache_read_input_tokens
    value = getattr(usage, "cache_read_input_tokens", None)
    if isinstance(value, int) and value > 0:
        return value
    return 0


@dataclass
class EmbeddingResponse:
    embedding: list[float] = field(default_factory=list)
    tokens: int = 0


class LLMProvider(ABC):
    """LLM Provider 基类 — 每个 Provider 必须实现"""

    name: str = ""

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[ChatResponse]:
        """流式推理，逐 chunk yield"""
        ...

    @abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
    ) -> ChatResponse:
        """非流式推理"""
        ...

    @abstractmethod
    async def embed(self, text: str, model: str) -> EmbeddingResponse:
        """文本嵌入"""
        ...

    @abstractmethod
    async def close(self) -> None:
        """释放资源"""
        ...
