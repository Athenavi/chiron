"""LangExtract 组件：元数据 + 薄封装。

与 :mod:`app.components.pageindex` 对称；安装 / 启停同样委托给
:class:`app.components.manager.ComponentManager`，这里不重复任何进程逻辑。
"""

from __future__ import annotations

from typing import Any

from app.components.manager import ComponentManager, ComponentSpec

#: 组件名（与 Electron 侧 ``COMPONENT_NAMES``、契约 §2 ``ComponentInfo.name`` 一致）
NAME = "langextract"

COMPONENT = ComponentSpec(
    name=NAME,
    title="LangExtract",
    description="结构化字段抽取（实体 / 关系 / 自定义 schema），供本地抽取流水线调用",
)


def spec() -> ComponentSpec:
    return COMPONENT


def install(manager: ComponentManager) -> dict[str, Any]:
    """从组件源安装（未配置源时 manager 抛 ``SourceUnconfigured``）。"""
    return manager.install(NAME)


def start(manager: ComponentManager, *, health_timeout: float | None = None) -> dict[str, Any]:
    return manager.start(NAME, health_timeout=health_timeout)


def stop(manager: ComponentManager) -> dict[str, Any]:
    return manager.stop(NAME)


def remove(manager: ComponentManager) -> dict[str, Any]:
    return manager.remove(NAME)


def status(manager: ComponentManager) -> dict[str, Any]:
    return manager.status(NAME)


__all__ = ["COMPONENT", "NAME", "install", "remove", "spec", "start", "status", "stop"]
