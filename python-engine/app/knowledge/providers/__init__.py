"""知识库 provider 实现与注册表。

* ``base``       —— 接口与数据类（对外只依赖这里）
* ``selfhosted`` —— PageIndex local 模式（每个知识库一个索引目录）
* ``cloud``      —— PageIndex Cloud（用户自带 API key）
* ``registry``   —— 名字 → 实例；按用户解析可用 provider
"""

from __future__ import annotations

from app.knowledge.providers import base
from app.knowledge.providers.base import (
    KNOWN_PROVIDERS,
    PROVIDER_CLOUD,
    PROVIDER_SELFHOSTED,
    STATUS_DISABLED,
    STATUS_ERROR,
    STATUS_READY,
    AnswerResult,
    Citation,
    HealthResult,
    KnowledgeProvider,
    ProviderContext,
    SubmitResult,
    is_known_provider,
)

__all__ = [
    "AnswerResult",
    "Citation",
    "HealthResult",
    "KNOWN_PROVIDERS",
    "KnowledgeProvider",
    "PROVIDER_CLOUD",
    "PROVIDER_SELFHOSTED",
    "ProviderContext",
    "STATUS_DISABLED",
    "STATUS_ERROR",
    "STATUS_READY",
    "SubmitResult",
    "base",
    "is_known_provider",
]
