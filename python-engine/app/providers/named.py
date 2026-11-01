"""具名 provider —— 目录驱动注册时把 provider id 注入实例 name。

``LLMProvider.name`` 决定 KeyRing 的 keyset 分区（``llm:keys:{name}``）以及路由
加权/熔断索引，因此目录里每个 provider id 都必须落到 **同名** 实例上，不能共用
基类的 ``"openai"`` / ``"anthropic"`` 默认值（否则所有 OpenAI 兼容 provider 会
抢同一份 key 池）。
"""
from __future__ import annotations

from typing import Any

from app.providers.anthropic import AnthropicProvider
from app.providers.openai import OpenAIProvider
from app.providers.responses import ResponsesProvider


class NamedOpenAIProvider(OpenAIProvider):
    """OpenAI 兼容协议的具名 provider（DeepSeek / Kimi / GLM / Ollama 等）。"""

    def __init__(
        self,
        name: str,
        *,
        api_key: str,
        base_url: str = "",
        key_ring: Any = None,
    ) -> None:
        self.name = name
        super().__init__(api_key=api_key, base_url=base_url, key_ring=key_ring)


class NamedResponsesProvider(ResponsesProvider):
    """具名 **Responses API** provider（D2）。

    Responses 与 chat/completions 是两套请求/响应形状，因此继承的是 `ResponsesProvider`
    而不是 `OpenAIProvider`；name 同样注入，保证 KeyRing 分区按 provider id 隔离。
    """

    def __init__(
        self,
        name: str,
        *,
        api_key: str,
        base_url: str = "",
        key_ring: Any = None,
    ) -> None:
        self.name = name
        # Responses 自带最小 HTTP 客户端，但**同样**要走 KeyRing：此前这里显式丢弃
        # key_ring，于是 keyset 托管的 responses provider 每次都用构造期那枚占位 key 打
        # 上游，稳定收到"你的 key 无效"——与 openai/anthropic 两条分支的口径必须一致。
        super().__init__(api_key=api_key, base_url=base_url, key_ring=key_ring)


class NamedAnthropicProvider(AnthropicProvider):
    """Anthropic Messages 协议的具名 provider（官方端点或自定义兼容网关）。"""

    def __init__(
        self,
        name: str,
        *,
        api_key: str,
        base_url: str = "",
        key_ring: Any = None,
    ) -> None:
        self.name = name
        super().__init__(api_key=api_key, base_url=base_url, key_ring=key_ring)
