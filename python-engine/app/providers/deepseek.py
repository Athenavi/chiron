# DeepSeek Provider — OpenAI 兼容 API
from __future__ import annotations

from app.providers.openai import OpenAIProvider


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek 使用 OpenAI 兼容接口，直接继承 OpenAIProvider。

    注意：引擎现在按服务提供商目录注册 provider（见 app/providers/catalog.py 与
    app/providers/named.py 的 ``NamedOpenAIProvider("deepseek", ...)``），本类仅保留
    供外部按名导入的兼容入口；新增 DeepSeek 类端点请改目录，不要在此扩展。
    """

    name = "deepseek"

    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com", *, key_ring=None):
        super().__init__(api_key=api_key, base_url=base_url, key_ring=key_ring)
