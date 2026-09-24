"""
__init__.py — 核心基础设施层
"""

from app.core.container import GlobalContainer, get_container, reset_container, set_container

__all__ = [
    "GlobalContainer",
    "get_container",
    "set_container",
    "reset_container",
]
