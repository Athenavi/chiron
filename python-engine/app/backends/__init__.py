"""文件 / 执行后端抽象（批 B）—— 设计说明见 `protocol.py` 的模块文档。"""

from app.backends.composite import CompositeBackend
from app.backends.context import get_backend, set_backend
from app.backends.local import LocalWorkspaceBackend
from app.backends.protocol import (
    BackendProtocol,
    EditResult,
    ExecuteResult,
    ExecutingBackendProtocol,
    FileStat,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadBytesResult,
    ReadResult,
    WriteResult,
)

__all__ = [
    "BackendProtocol",
    "CompositeBackend",
    "EditResult",
    "ExecuteResult",
    "ExecutingBackendProtocol",
    "FileStat",
    "GlobResult",
    "GrepMatch",
    "GrepResult",
    "LocalWorkspaceBackend",
    "LsResult",
    "ReadBytesResult",
    "ReadResult",
    "WriteResult",
    "get_backend",
    "set_backend",
]
