"""知识库 provider 实现与注册表。

* ``base``       —— 接口与数据类（对外只依赖这里）
* ``remote``     —— 引擎侧唯一实现（内网 HTTP 调 pageindex-search）
  （进程内实现已删除：``pageindex → litellm → openai<3`` 与引擎的 ``openai 3.x`` 无法共存）
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
