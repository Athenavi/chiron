"""后端注入通道（contextvars，与 `app/tools/context.py` 同一范式）。

**为什么用 contextvar 而不是给工具加参数**：工具的 `parameters` 是**暴露给 LLM 的签名**，
加一个 `backend` 参数会让模型看到一个它不该管的旋钮（且会写进 function schema）。
contextvar 是进程内注入，对模型不可见 —— 与 `get_user_id()` 的既有做法一致。
"""

from __future__ import annotations

from contextvars import ContextVar

from app.backends.local import LocalWorkspaceBackend
from app.backends.protocol import BackendProtocol

_current: ContextVar[BackendProtocol | None] = ContextVar("chiron_backend", default=None)
_default: BackendProtocol | None = None


def set_backend(backend: BackendProtocol | None) -> None:
    """为当前上下文设置后端（`None` = 恢复默认的本地工作区后端）。"""
    _current.set(backend)


def get_backend() -> BackendProtocol:
    """取当前后端；未显式设置时用默认的本地工作区后端 —— 即**今天的全部行为**。"""
    backend = _current.get()
    if backend is not None:
        return backend
    global _default  # noqa: PLW0603 — 进程内惰性单例，避免每次调用都构造
    if _default is None:
        _default = LocalWorkspaceBackend()
    return _default
