"""provider 注册表：名字 → 实例，以及"按用户解析出可用 provider"。

S4 之后引擎侧只剩**一种实现**：``RemoteProvider``（内网 HTTP 调 ``pageindex-search``）。
进程内的 PageIndex 实现（``selfhosted.py`` / ``cloud.py``）已删除 —— 它要求引擎装
PageIndex SDK，而 ``pageindex → litellm → openai<3`` 与引擎固定的 ``openai 3.x``
在同一个环境里无法共存（pip ResolutionImpossible）。

provider 名（``selfhosted`` / ``cloud``）**保留**：它们是用户可见的选择、也是数据里的取值，
只是现在决定"让服务用哪种模式"。

解析失败**不抛异常**：调用方拿到 ``(provider, settings, health)`` 自行决定返回 503 还是降级 ——
SaaS 下"某个用户的检索配置有问题"不该变成 500。
"""

from __future__ import annotations

from app.knowledge import settings_store
from app.knowledge.errors import KnowledgeError
from app.knowledge.providers.base import (
    STATUS_DISABLED,
    STATUS_ERROR,
    HealthResult,
    KnowledgeProvider,
    is_known_provider,
)
from app.knowledge.remote import RemoteProvider
from app.knowledge.settings_store import UserKnowledgeSettings


def build_provider(
    name: str, *, cloud_api_key: str = "", folder_id: str = "", user_folder_id: str = ""
) -> KnowledgeProvider:
    if not is_known_provider(name):
        raise KnowledgeError(f"unknown knowledge provider: {name!r}")
    return RemoteProvider(
        mode=name,
        api_key=cloud_api_key,
        folder_id=folder_id,
        user_folder_id=user_folder_id,
    )


async def resolve_for_user(
    user_id: str, *, folder_id: str = ""
) -> tuple[KnowledgeProvider | None, UserKnowledgeSettings | None, HealthResult]:
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
    """系统级自托管状态（不依赖任何用户、不触网）。

    启动路径上只调它：未配置时返回 ok=False，服务照常起（用户维度各有状态）。
    """
    try:
        provider: KnowledgeProvider = build_provider("selfhosted")
    except Exception as exc:  # noqa: BLE001
        return HealthResult(False, STATUS_ERROR, str(exc))
    return provider.health()
