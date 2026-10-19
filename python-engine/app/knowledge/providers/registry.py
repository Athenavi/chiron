"""provider 注册表：名字 → 实例，以及"按用户解析出可用 provider"。

新增一种检索引擎 = 写一个实现 ``KnowledgeProvider`` 的类 + 在 ``_BUILDERS`` 里登记，
不需要改 API 层。取值集合刻意保持开放（见 ``base.py`` 的说明）。

解析失败**不抛异常**：调用方（API / 工具）拿到的是 ``(provider, settings, health)``，
据此决定返回 503 还是降级 —— SaaS 下"某个用户的检索配置有问题"不该变成 500。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.config import settings
from app.knowledge.errors import KnowledgeError
from app.knowledge.providers.base import (
    PROVIDER_CLOUD,
    PROVIDER_SELFHOSTED,
    STATUS_DISABLED,
    STATUS_ERROR,
    HealthResult,
    KnowledgeProvider,
)
from app.knowledge.providers.cloud import CloudProvider
from app.knowledge.providers.selfhosted import SelfHostedProvider
from app.knowledge.settings_store import UserKnowledgeSettings
from app.knowledge import settings_store

#: provider 名 → 构造函数。构造参数都用关键字，新增 provider 时按需扩展。
#: ``user_folder_id`` 是 PageIndex Cloud 的**用户级** folder（其余 provider 忽略）。
_BUILDERS: dict[str, Callable[..., KnowledgeProvider]] = {
    PROVIDER_SELFHOSTED: lambda *, cloud_api_key="", folder_id="", user_folder_id="": SelfHostedProvider(
        root=settings.knowledge_selfhosted_root or _default_root(),
        index_model=settings.knowledge_selfhosted_index_model,
        chat_model=settings.knowledge_selfhosted_chat_model,
    ),
    PROVIDER_CLOUD: lambda *, cloud_api_key="", folder_id="", user_folder_id="": CloudProvider(
        api_key=cloud_api_key,
        chat_model=settings.knowledge_cloud_chat_model,
        folder_id=folder_id,
        user_folder_id=user_folder_id,
    ),
}


def _default_root() -> str:
    """未显式配置时的索引根目录（开发/单机）。生产必须显式指向共享存储。"""
    import os

    return os.path.join("data", "knowledge_index")


def build_provider(
    name: str, *, cloud_api_key: str = "", folder_id: str = "", user_folder_id: str = ""
) -> KnowledgeProvider:
    builder = _BUILDERS.get(name)
    if builder is None:
        raise KnowledgeError(f"unknown knowledge provider: {name!r}")
    return builder(
        cloud_api_key=cloud_api_key, folder_id=folder_id, user_folder_id=user_folder_id
    )


async def resolve_for_user(
    user_id: str, *, folder_id: str = ""
) -> tuple[KnowledgeProvider | None, UserKnowledgeSettings | None, HealthResult]:
    """按用户设置构造 provider 并探测可用性。

    返回三元组而不是抛异常：``provider is None`` 表示"这个用户还没选/选不出 provider"，
    调用方据此给出 503 + 可读原因。
    """
    if not user_id:
        return None, None, HealthResult(False, STATUS_DISABLED, "缺少用户身份")

    user_settings = await settings_store.load(user_id)
    if user_settings is None:
        return None, None, HealthResult(False, STATUS_DISABLED, "尚未选择知识库 provider")
    if not user_settings.provider:
        return None, user_settings, HealthResult(False, STATUS_DISABLED, "知识库 provider 为空")

    try:
        provider = build_provider(
            user_settings.provider,
            cloud_api_key=user_settings.cloud_api_key,
            folder_id=folder_id,
            user_folder_id=user_settings.cloud_folder_id,
        )
    except Exception as exc:  # noqa: BLE001 — 构造失败按"不可用"上报，不 500
        return None, user_settings, HealthResult(False, STATUS_ERROR, str(exc))

    try:
        health = provider.health()
    except Exception as exc:  # noqa: BLE001 — health 约定不抛，这里兜底
        health = HealthResult(False, STATUS_ERROR, f"健康检查异常: {exc}")
    return provider, user_settings, health


def selfhosted_health() -> HealthResult:
    """系统级自托管状态（不依赖任何用户）。

    启动路径上只调它一次：未配置/未安装时返回 ok=False，服务照常起（用户维度各有状态）。
    """
    try:
        provider: Any = build_provider(PROVIDER_SELFHOSTED)
    except Exception as exc:  # noqa: BLE001
        return HealthResult(False, STATUS_ERROR, str(exc))
    return provider.health()
