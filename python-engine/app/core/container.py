"""
DI 容器 — FastAPI Depends + 全局容器（非 HTTP 场景）
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


class GlobalContainer:
    """
    全局容器 — 用于非 HTTP 场景（队列消费者、后台任务等）

    使用示例：
        container = GlobalContainer()
        container.register(LLMProvider, lambda c: GatewayLLMProvider(...))
        llm = container.resolve(LLMProvider)
    """

    def __init__(self) -> None:
        self._factories: dict[type, tuple[Callable[..., Any], bool]] = {}
        self._singletons: dict[type, Any] = {}

    def register(
        self,
        interface: type[T],
        factory: Callable[[GlobalContainer], T],
        singleton: bool = True,
    ) -> None:
        """注册工厂函数"""
        self._factories[interface] = (factory, singleton)

    def resolve(self, interface: type[T]) -> T:
        """解析依赖"""
        if interface not in self._factories:
            raise KeyError(f"No factory registered for {interface.__name__}")

        factory, singleton = self._factories[interface]

        if singleton:
            if interface not in self._singletons:
                self._singletons[interface] = factory(self)
            cached: T = self._singletons[interface]
            return cached

        created: T = factory(self)
        return created

    def reset(self) -> None:
        """重置容器（用于测试）"""
        self._factories.clear()
        self._singletons.clear()


# 全局容器实例
_container: GlobalContainer | None = None


def get_container() -> GlobalContainer:
    """获取全局容器"""
    global _container
    if _container is None:
        _container = GlobalContainer()
    return _container


def set_container(container: GlobalContainer) -> None:
    """设置全局容器（用于测试或自定义配置）"""
    global _container
    _container = container


def reset_container() -> None:
    """重置全局容器"""
    global _container
    if _container:
        _container.reset()
    _container = None
