"""具名 provider —— 目录驱动注册时把 provider id 注入实例 name。

``LLMProvider.name`` 决定 KeyRing 的 keyset 分区（``llm:keys:{name}``）以及路由
加权/熔断索引，因此目录里每个 provider id 都必须落到 **同名** 实例上，不能共用
基类的 ``"openai"`` / ``"anthropic"`` 默认值（否则所有 OpenAI 兼容 provider 会
抢同一份 key 池）。
"""
from __future__ import annotations

from app.providers.anthropic import AnthropicProvider
from app.providers.openai import OpenAIProvider


class NamedOpenAIProvider(OpenAIProvider):
    """OpenAI 兼容协议的具名 provider（DeepSeek / Kimi / GLM / Ollama 等）。"""

    def __init__(self, name: str, *, api_key: str, base_url: str = "", key_ring=None):
        self.name = name
        super().__init__(api_key=api_key, base_url=base_url, key_ring=key_ring)


class NamedAnthropicProvider(AnthropicProvider):
    """Anthropic Messages 协议的具名 provider（官方端点或自定义兼容网关）。"""

    def __init__(self, name: str, *, api_key: str, base_url: str = "", key_ring=None):
        self.name = name
        super().__init__(api_key=api_key, base_url=base_url, key_ring=key_ring)
