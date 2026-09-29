"""文件 / 执行后端的统一协议（批 B，见方案 02 §3.1）。

**为什么需要它**：现在 `read_file` / `write_file` / `glob_files` 等工具**直接操作本地目录**
（`sandbox.safe_join` + `Path.read_text`），于是"文件到底放在哪里"这件事散落在每个工具里 ——
想接对象存储 / 共享卷 / 远端沙箱就得逐个改工具，而多副本下会"间歇性失忆"
（`app/tools/sandbox.py` 的注释已自陈该风险）。

抽象之后：工具只依赖协议，**换后端 = 换一个实现类**。

两条硬约束（方案 02 §3.1，都来自 deepagents 的踩坑）：

1. **`execute` 与 `read`/`write` 必须解析到同一文件系统** —— 否则"工具结果落盘"与"命令能看到
   的文件"不一致，大输出卸载（capture/offload）会路由到错的地方。
2. **`truncated` 必须由产生截断的那一层给出**，不能靠调用方扫文本猜测 ——
   命令输出里可能正好含一段长得像截断标记的文本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ReadResult:
    """读文件的结果。

    约定：`limit <= 0` 表示**读全文**（不截断）—— `edit_file` 这类需要完整内容的调用方
    用它，而不是"先猜一个够大的 limit"。
    """

    path: str
    content: str = ""
    total_lines: int = 0
    offset: int = 0
    limit: int = 0
    truncated: bool = False
    error: str | None = None


@dataclass(frozen=True)
class ReadBytesResult:
    """读原始字节的结果（图片等二进制资源用）。"""

    path: str
    data: bytes = b""
    error: str | None = None


@dataclass(frozen=True)
class FileStat:
    """文件元信息（`glob` 结果只给路径，大小需要单独问）。"""

    path: str
    size: int = 0
    is_file: bool = True


@dataclass(frozen=True)
class WriteResult:
    """写文件的结果。"""

    path: str
    bytes_written: int = 0
    error: str | None = None


@dataclass(frozen=True)
class EditResult:
    """替换式编辑的结果（`occurrences` 便于调用方判断是否唯一命中）。"""

    path: str
    occurrences: int = 0
    error: str | None = None


@dataclass(frozen=True)
class LsResult:
    """列目录的结果。"""

    entries: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class GlobResult:
    """按模式匹配路径的结果。"""

    paths: list[str] = field(default_factory=list)
    truncated: bool = False
    #: 截断原因：`budget`（命中数上限）/ `unreadable` / `transport`
    reason: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class GrepMatch:
    """一条搜索命中。"""

    path: str
    line_number: int
    line: str


@dataclass(frozen=True)
class GrepResult:
    """搜索的结果。"""

    matches: list[GrepMatch] = field(default_factory=list)
    truncated: bool = False
    reason: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ExecuteResult:
    """执行命令的结果。

    注意：`exit_code` 非 0 **不代表**调用失败 —— 命令跑起来了就是"成功执行"，
    是否算失败由调用方读输出决定（对位 deepagents 的 `ExecuteArtifact` 注释）。
    """

    output: str = ""
    exit_code: int | None = None
    truncated: bool = False
    error: str | None = None


@runtime_checkable
class BackendProtocol(Protocol):
    """文件操作后端。"""

    async def read(self, path: str, *, offset: int = 0, limit: int = 200) -> ReadResult:
        """读文本。`limit <= 0` = 读全文。"""
        ...

    async def read_bytes(self, path: str) -> ReadBytesResult:
        """读原始字节（图片 / 二进制）。"""
        ...

    async def stat(self, path: str) -> FileStat | None:
        """取文件元信息；不存在返回 `None`。"""
        ...

    async def write(self, path: str, content: str) -> WriteResult: ...

    async def edit(self, path: str, old: str, new: str) -> EditResult: ...

    async def ls(self, path: str = ".") -> LsResult: ...

    async def glob(self, pattern: str, *, path: str = ".") -> GlobResult: ...

    async def grep(
        self, pattern: str, *, path: str = ".", max_results: int = 100
    ) -> GrepResult: ...

    def supports_execution(self) -> bool:
        """该后端能否执行命令（不能时调用方应把执行类工具从工具面剔除）。"""
        ...


@runtime_checkable
class ExecutingBackendProtocol(BackendProtocol, Protocol):
    """带执行能力的后端（容器 / 远端沙箱 / 独立 sandbox 服务都实现它）。"""

    async def execute(self, command: str, *, timeout: float | None = None) -> ExecuteResult: ...
