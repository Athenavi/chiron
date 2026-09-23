"""核心本地工具集（Python 端）。

首波迁移：filesystem / shell / search(grep) / web。
后续可继续扩展 memory / pm / skill / workflow 等工具。
"""

from __future__ import annotations

import glob as globmod
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
    from app.tools.fs_guard import observe
    from app.tools.sandbox import safe_join

    target = safe_join(path)  # 沙箱隔离：root 参数废弃，强制 workspace（S 安全修复）
    if not target.exists():
        return {"error": f"file not found: {path}"}
    text = target.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    page = lines[offset : offset + limit]
    observe(target)  # 记录版本，供 write/edit 的 read-before-write 校验
    return {
        "path": str(target),
        "total_lines": len(lines),
        "offset": offset,
        "limit": limit,
        "content": "\n".join(page),
    }


async def read_image(
    path: str, root: str = ".", max_bytes: int = 5 * 1024 * 1024
) -> dict[str, Any]:
    """Read an image file as a base64 data-URL (for vision-capable models)."""
    import base64

    from app.tools.fs_guard import observe
    from app.tools.sandbox import safe_join

    target = safe_join(path)  # 沙箱隔离（S 安全修复）
    if not target.exists():
        return {"error": f"file not found: {path}"}
    if target.stat().st_size > max_bytes:
        return {
            "error": f"image too large ({target.stat().st_size} bytes, max {max_bytes})"
        }
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
    data = target.read_bytes()
    observe(target)
    return {
        "path": str(target),
        "media_type": media_types[ext],
        "bytes": len(data),
        "data_url": f"data:{media_types[ext]};base64,{base64.b64encode(data).decode('ascii')}",
    }


async def write_file(path: str, content: str, root: str = ".") -> dict[str, Any]:
    from app.agent import undo_stack
    from app.tools.context import get_session_id
    from app.tools.fs_guard import check_before_write
    from app.tools.sandbox import safe_join

    target = safe_join(path)  # 沙箱隔离：root 参数废弃（S 安全修复）
    conflict = check_before_write(target)
    if conflict:
        return {"error": conflict}
    # 写入前快照：`/undo` 据此**真正恢复**文件（此前 /undo 只是回显一个字符串）。
    # 快照存不下（文件过大 / 存储不可用）时**当场告知"本次不可撤销"**，而不是事后才发现。
    undo_note = await undo_stack.snapshot_before_write(get_session_id(), target, "write_file")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    result: dict[str, Any] = {"path": str(target), "bytes": len(content.encode("utf-8"))}
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
    pattern = re.compile(query)
    matches: list[dict[str, Any]] = []
    from app.tools.sandbox import workspace_dir

    base = workspace_dir()  # 沙箱隔离（S 安全修复）
    paths = globmod.glob(str(base / "**" / (glob or "*")), recursive=True)
    for p in paths:
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                for idx, line in enumerate(f, 1):
                    if pattern.search(line):
                        matches.append(
                            {"path": p, "line": idx, "text": line.rstrip("\n")}
                        )
                        if len(matches) >= max_results:
                            return {"count": len(matches), "matches": matches}
        except Exception:
            logger.debug("Cannot read file %s, skipping", p)
            continue
    return {"count": len(matches), "matches": matches}


async def search_files(
    pattern: str, root: str = ".", max_results: int = 200
) -> dict[str, Any]:
    from app.tools.sandbox import workspace_dir

    base = workspace_dir()  # 沙箱隔离（S 安全修复）
    paths = globmod.glob(str(base / "**" / (pattern or "*")), recursive=True)
    files = [p for p in paths if os.path.isfile(p)][:max_results]
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
