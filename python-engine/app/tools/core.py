"""核心本地工具集（Python 端）。

首波迁移：filesystem / shell / search(grep) / web。
后续可继续扩展 memory / pm / skill / workflow 等工具。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

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


async def read_file(
    path: str, root: str = ".", offset: int = 0, limit: int = 200
) -> dict[str, Any]:
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
    path: str, root: str = ".", max_bytes: int = 5 * 1024 * 1024
) -> dict[str, Any]:
    """Read an image file as a base64 data-URL (for vision-capable models)."""
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


async def write_file(path: str, content: str, root: str = ".") -> dict[str, Any]:
    from app.agent import undo_stack
    from app.backends.context import get_backend
    from app.tools.context import get_session_id
    from app.tools.fs_guard import check_before_write
    from app.tools.sandbox import safe_join

    target = safe_join(path)  # 沙箱隔离：root 参数废弃（S 安全修复）
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


async def execute_command(
    command: str, cwd: str = ".", timeout: int = 30
) -> dict[str, Any]:
    from app.tools.sandbox import run_in_sandbox

    # 沙箱隔离：cwd 参数废弃，命令在 per-user workspace 内以清理环境执行（S 安全修复）
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
    if not code:
        return {"error": "code is required"}
    return await execute_command(
        command=f"python -c {json.dumps(code)}", timeout=timeout
    )


async def web_fetch(
    url: str, max_chars: int = 12000, follow_redirects: bool = True
) -> dict[str, Any]:
    from app.config import settings
    from app.tools.ssrf import assert_safe_url, fetch_url_safe

    assert_safe_url(url)
    async with httpx.AsyncClient(timeout=settings.http_timeout_web) as client:
        resp = await fetch_url_safe(client, url)
        text = resp.text[:max_chars]
        return {
            "url": str(resp.url),
            "status_code": resp.status_code,
            "content_type": resp.headers.get("content-type", ""),
            "content": text,
        }


# 注册工具（schema 与 Go 侧保持兼容）
registry.register(
    name="read_file",
    description="Read a file with pagination",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "default": 0},
            "limit": {"type": "integer", "default": 2000},
        },
        "required": ["path"],
    },
    handler=read_file,
)

registry.register(
    name="write_file",
    description="Write content to a file",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    handler=write_file,
)

registry.register(
    name="shell_exec",
    description="Execute a shell command with timeout",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "cwd": {"type": "string", "default": "."},
            "timeout": {"type": "integer", "default": 30},
        },
        "required": ["command"],
    },
    handler=execute_command,
)

registry.register(
    name="grep_files",
    description="Regex search across files",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "root": {"type": "string", "default": "."},
            "glob": {"type": "string", "default": ""},
            "max_results": {"type": "integer", "default": 200},
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
    description="Execute a Python code snippet",
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "timeout": {"type": "integer", "default": 30},
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
            "path": {"type": "string"},
            "root": {"type": "string", "default": "."},
            "max_bytes": {
                "type": "integer",
                "default": 5242880,
                "description": "Max image bytes",
            },
        },
        "required": ["path"],
    },
    handler=read_image,
)
