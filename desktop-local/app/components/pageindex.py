"""PageIndex 组件：元数据 + 薄封装。

进程管理与安装逻辑都在 :mod:`app.components.manager`；本模块只回答"这个组件是什么"，
并给调用方一组不用拼字符串的方法（``pageindex.start(manager)`` 而不是
``manager.start("pageindex")``）。
"""

from __future__ import annotations

from typing import Any

from app.components.manager import ComponentManager, ComponentSpec

#: 组件名（与 Electron 侧 ``COMPONENT_NAMES``、契约 §2 ``ComponentInfo.name`` 一致）
NAME = "pageindex"

COMPONENT = ComponentSpec(
    name=NAME,
    title="PageIndex",
    description="把长文档切成层级树并给出可追溯的章节索引，供知识库构建与引用定位使用",
)


def spec() -> ComponentSpec:
    return COMPONENT


def install(manager: ComponentManager) -> dict[str, Any]:
    """从组件源安装（未配置源时 manager 抛 ``SourceUnconfiguredError``）。"""
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
