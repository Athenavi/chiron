"""核心本地工具集（Python 端）。

首波迁移：filesystem / shell / search(grep) / web。
后续可继续扩展 memory / pm / skill / workflow 等工具。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from app.tools.registry import registry

logger = logging.getLogger(__name__)


def _safe_path(path: str, root: str) -> Path:
    """防止路径穿越，返回绝对路径。"""
    base = Path(root).resolve()
    target = (base / path).resolve()
    # 必须加 trailing separator，否则 /app 会误匹配 /appdata
    if target != base and not str(target).startswith(str(base) + os.sep):
        raise ValueError("path escapes root")
    return target


async def read_file(path: str, offset: int = 0, limit: int = 200) -> dict[str, Any]:
    """读文件（分页）。

    路径一律 clamp 到当前工作区 —— 此前签名里有个 `root` 参数，模型以为能改根目录，
    实现根本不看它（已移除）。
    """
    from app.backends.context import get_backend
    from app.tools.fs_guard import observe
    from app.tools.sandbox import safe_join

    # 批 B：文件读经**后端**（默认 = 本地工作区，行为与迁移前一致）
    result = await get_backend().read(path, offset=offset, limit=limit)
    if result.error:
        return {"error": result.error}
    # 横切逻辑留在工具层：read-before-write 观测（写入校验依赖它）
    observe(safe_join(path))  # 记录版本，供 write/edit 的 read-before-write 校验
    return {
        "path": result.path,
        "total_lines": result.total_lines,
        "offset": result.offset,
        "limit": result.limit,
        "content": result.content,
    }


async def read_image(
    path: str, max_bytes: int = 5 * 1024 * 1024
) -> dict[str, Any]:
    """Read an image file as a base64 data-URL (for vision-capable models).

    路径同样 clamp 到工作区（`root` 参数已移除，理由与 `read_file` 相同）。
    """
    import base64

    from app.backends.context import get_backend
    from app.tools.fs_guard import observe
    from app.tools.sandbox import safe_join

    backend = get_backend()
    target = safe_join(path)  # 沙箱隔离（S 安全修复）

    # 批 B：大小与字节都经后端（默认 = 本地工作区）
    info = await backend.stat(path)
    if info is None:
        return {"error": f"file not found: {path}"}
    if info.size > max_bytes:
        return {"error": f"image too large ({info.size} bytes, max {max_bytes})"}
    ext = target.suffix.lower()
    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    if ext not in media_types:
        return {
            "error": f"unsupported image type: {ext or 'unknown'}; supported: png/jpg/webp/gif"
        }
    blob = await backend.read_bytes(path)
    if blob.error:
        return {"error": blob.error}
    data = blob.data
    observe(target)
    return {
        "path": str(target),
        "media_type": media_types[ext],
        "bytes": len(data),
        "data_url": f"data:{media_types[ext]};base64,{base64.b64encode(data).decode('ascii')}",
    }


async def write_file(path: str, content: str) -> dict[str, Any]:
    """写文件（会建父目录；写入前自动快照供 /undo 回滚）。

    路径 clamp 到工作区（`root` 参数已移除，理由与 `read_file` 相同）。
    """
    from app.agent import undo_stack
    from app.backends.context import get_backend
    from app.tools.context import get_session_id
    from app.tools.fs_guard import check_before_write
    from app.tools.sandbox import safe_join

    target = safe_join(path)  # 沙箱隔离
    # 横切逻辑留在工具层，且**顺序不变**：冲突检查 → undo 快照 → 写入
    conflict = check_before_write(target)
    if conflict:
        return {"error": conflict}
    # 写入前快照：`/undo` 据此**真正恢复**文件（此前 /undo 只是回显一个字符串）。
    # 快照存不下（文件过大 / 存储不可用）时**当场告知"本次不可撤销"**，而不是事后才发现。
    undo_note = await undo_stack.snapshot_before_write(get_session_id(), target, "write_file")
    # 批 B：写入经**后端**（默认 = 本地工作区；建父目录也由后端负责）
    written = await get_backend().write(path, content)
    if written.error:
        return {"error": written.error}
    result: dict[str, Any] = {"path": written.path, "bytes": written.bytes_written}
    if undo_note:
        result["undo_warning"] = undo_note
    return result


async def execute_command(command: str, timeout: int = 30) -> dict[str, Any]:
    """在沙箱内执行一条 shell 命令。

    工作目录固定为当前用户的工作区：此前签名里有 `cwd`，schema 说它能改目录，
    实现却忽略它 —— 模型据此重试过多次（已移除）。
    """
    from app.tools.sandbox import run_in_sandbox

    return await run_in_sandbox(command, timeout=timeout)


async def grep_files(
    query: str, root: str = ".", glob: str = "", max_results: int = 200
) -> dict[str, Any]:
    """按正则搜索文件内容（批 B：匹配与读取都经后端）。

    行为与迁移前一致：候选文件由 `**/{glob}` 限定 → 逐个读全文 → 逐行匹配 → 命中达上限即返回。
    因此保留"先拿文件列表、再逐文件读"的两步形态（而不是一次目录级 grep），
    这样 `max_results` 的截断点与扫描顺序都不变。
    """
    from app.backends.context import get_backend

    try:
        pattern = re.compile(query)
    except re.error as exc:
        return {"error": f"invalid pattern: {exc}", "count": 0, "matches": []}

    backend = get_backend()
    globbed = await backend.glob(f"**/{glob or '*'}", path=root or ".")
    if globbed.error:
        return {"error": globbed.error, "count": 0, "matches": []}

    matches: list[dict[str, Any]] = []
    for candidate in globbed.paths:
        info = await backend.stat(candidate)
        if info is None or not info.is_file:
            continue
        read = await backend.read(candidate, limit=0)  # 全文：逐行匹配需要完整内容
        if read.error:
            logger.debug("Cannot read file %s, skipping", candidate)
            continue
        for idx, line in enumerate(read.content.splitlines(), start=1):
            if pattern.search(line):
                matches.append({"path": candidate, "line": idx, "text": line})
                if len(matches) >= max_results:
                    return {"count": len(matches), "matches": matches}
    return {"count": len(matches), "matches": matches}


async def search_files(
    pattern: str, root: str = ".", max_results: int = 200
) -> dict[str, Any]:
    """按文件名模式搜索（批 B：匹配经后端）。

    与 `glob_files` 的区别是**自动递归**（`**/` 前缀）—— 这是既有行为差异，保留。
    """
    from app.backends.context import get_backend

    backend = get_backend()
    globbed = await backend.glob(f"**/{pattern or '*'}", path=root or ".")
    if globbed.error:
        return {"error": globbed.error, "count": 0, "files": []}

    files: list[str] = []
    for candidate in globbed.paths:
        info = await backend.stat(candidate)
        if info is not None and info.is_file:
            files.append(candidate)
        if len(files) >= max_results:
            break
    return {"count": len(files), "files": files}


async def execute_python(code: str, timeout: int = 30) -> dict[str, Any]:
    """执行一段 Python 代码（沙箱内）。

    为什么不是 `python -c <代码>`：那要把源码**拼进 shell 命令行**，于是必须自己处理引号、
    换行、反斜杠的转义 —— `json.dumps` 只解决了一部分（POSIX 下可行，Windows 的 cmd.exe
    规则不同，含双引号/换行的代码会直接执行失败或截断）。

    这里改成 **base64 传递**：`-c` 的参数整体用双引号包住，内部只有 base64 字母表
    （`A-Za-z0-9+/=`，不含引号与空白），因此两种 shell 下的引号解析都无歧义。
    代价是命令行长一点（约 4/3 倍），换来的是**跨平台确定性**。
    """
    if not code:
        return {"error": "code is required"}
    import base64

    payload = base64.b64encode(code.encode("utf-8")).decode("ascii")
    command = (
        'python -c "import base64;'
        f"exec(base64.b64decode('{payload}').decode('utf-8'))\""
    )
    return await execute_command(command=command, timeout=timeout)


# ── 已删除：core 里的 web_fetch ──
#
# 这里曾有一份 `web_fetch` 实现（httpx + assert_safe_url），但它**从未注册**，
# 而 `app/tools/web.py` 里有一份更完整的同名实现（HTML→Markdown、字符上限、错误语义）。
# 两份实现并存 = 必然漂移（规划 §5.3 的"两份白名单必然漂移"是同一类错）。
# 只保留 web.py 的那一份；本模块不再有出网能力。


# 注册工具（schema 与 Go 侧保持兼容）
registry.register(
    name="read_file",
    description="Read a text file from the workspace with pagination.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the workspace root"},
            "offset": {"type": "integer", "default": 0, "description": "Line to start from (0-based)"},
            "limit": {"type": "integer", "default": 200, "description": "Max lines to return"},
        },
        "required": ["path"],
    },
    handler=read_file,
)

registry.register(
    name="write_file",
    description=(
        "Write text to a file in the workspace (parent directories are created). "
        "This is for work-in-progress files; to hand the user a file they can open or "
        "download, use media_create instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the workspace root"},
            "content": {"type": "string", "description": "Full content to write"},
        },
        "required": ["path", "content"],
    },
    handler=write_file,
)

registry.register(
    name="shell_exec",
    description=(
        "Run a shell command in the sandbox. The working directory is always the workspace "
        "(it cannot be changed). Returns stdout/stderr/exit_code."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "timeout": {"type": "integer", "default": 30, "description": "Timeout in seconds"},
        },
        "required": ["command"],
    },
    handler=execute_command,
)

registry.register(
    name="grep_files",
    description="Regex-search the contents of files under a workspace directory.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Regular expression to match against each line"},
            "root": {"type": "string", "default": ".", "description": "Directory to search under (relative to the workspace)"},
            "glob": {"type": "string", "default": "", "description": "Only scan files matching this glob (e.g. '*.py')"},
            "max_results": {"type": "integer", "default": 200, "description": "Stop after this many matches"},
        },
        "required": ["query"],
    },
    handler=grep_files,
)

registry.register(
    name="search_files",
    description="Search files by glob pattern",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "root": {"type": "string", "default": "."},
            "max_results": {"type": "integer", "default": 200},
        },
        "required": ["pattern"],
    },
    handler=search_files,
)

registry.register(
    name="execute_python",
    description=(
        "Run a short Python snippet in the sandbox. The code is passed as base64 so quoting "
        "works identically on every platform. For multi-step work that should call several "
        "tools in one shot, prefer run_code."
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source to execute"},
            "timeout": {"type": "integer", "default": 30, "description": "Timeout in seconds"},
        },
        "required": ["code"],
    },
    handler=execute_python,
)

registry.register(
    name="read_image",
    description="Read an image file (PNG/JPEG/WebP/GIF) as a base64 data-URL for vision-capable models.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Image path relative to the workspace root"},
            "max_bytes": {
                "type": "integer",
                "default": 5242880,
                "description": "Max image bytes (default 5 MB)",
            },
        },
        "required": ["path"],
    },
    handler=read_image,
)
