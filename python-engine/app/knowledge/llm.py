"""自托管 provider 的 LLM 接入：模型名 → PageIndex（litellm）需要的 backend。

PageIndex 的 ``index`` 与 ``chat`` **都跑"你自己的模型"**（不是平台自带的）：local 模式下
建树用 ``index`` 模型做摘要精炼、检索用 ``chat`` 模型在树上推理。所以"自托管"不是零依赖，
它依赖部署方已经配置好的 LLM。

这里的职责只有一件事：把 Chiron 已经解析好的 provider 配置（端点、key）翻译成 PageIndex
的 ``index_backend`` / ``chat_backend``。**不重新实现 key 解析** —— 管理端 DB 覆盖、
``{PROVIDER}_BASE_URL``/``_API_KEY`` env、Settings 字段这套优先级是 ``app/providers/catalog``
的既有语义，重复一份必然漂移。

模型名按 [LiteLLM 命名](https://docs.litellm.ai/docs/providers) 归一化：``openai/<model>``、
``anthropic/<model>``，配合 ``base_url`` 即可打到任意 OpenAI 兼容网关（含自建的 vLLM/Ollama）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMTarget:
    """一次 LLM 调用的目标：归一化模型名 + 连接覆盖。"""

    model: str
    api_key: str = ""
    base_url: str = ""

    def backend(self) -> dict[str, Any]:
        """PageIndex 的 ``backend`` 字段（透传给底层 litellm 客户端）。"""
        out: dict[str, Any] = {}
        if self.api_key:
            out["api_key"] = self.api_key
        if self.base_url:
            out["base_url"] = self.base_url
        return out


def _match_preset(model: str) -> tuple[dict[str, Any] | None, str]:
    """按 ``provider/model`` 前缀或 ``model_prefixes`` 匹配目录项，返回 (preset, 裸模型名)。"""
    from app.providers.catalog import provider_catalog

    raw = (model or "").strip()
    if not raw:
        return None, ""
    catalog = provider_catalog()

    if "/" in raw:
        prefix, _, bare = raw.partition("/")
        for preset in catalog:
            if str(preset.get("id") or "") == prefix:
                return preset, bare
        # ``openai/gpt-4o`` 这类按 kind 写的前缀也认（litellm 风格）
        for preset in catalog:
            if str(preset.get("kind") or "") == prefix and bare:
                return preset, bare

    for preset in catalog:
        for mp in preset.get("model_prefixes") or []:
            if raw.startswith(str(mp)):
                return preset, raw
    return None, raw


def resolve_target(model: str) -> LLMTarget:
    """把模型名解析成 litellm 可用的目标。

    匹配不到目录项时**原样返回**（不做猜测）：litellm 自带的默认端点/环境变量仍可能可用，
    而硬塞一个 base_url 只会把错误引向错误的方向。
    """
    from app.providers.catalog import (
        provider_api_key,
        provider_base_url,
        provider_kind,
    )

    preset, bare = _match_preset(model)
    if preset is None:
        logger.debug("knowledge llm: model=%r not in provider catalog, using as-is", model)
        return LLMTarget(model=(model or "").strip())

    kind = provider_kind(preset)
    return LLMTarget(
        model=f"{kind}/{bare}" if bare else str(model),
        api_key=provider_api_key(preset),
        base_url=provider_base_url(preset),
    )
