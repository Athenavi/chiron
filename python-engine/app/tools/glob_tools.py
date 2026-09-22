"""Glob 工具集 — 文件模式匹配搜索。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.tools.registry import registry


async def glob_files(pattern: str, root: str = ".") -> dict[str, Any]:
    """Find files matching a glob pattern (supports **, *, ?, []).

    Returns a list of matching file paths with their sizes.
    """
    from app.tools.sandbox import workspace_dir

    # 沙箱隔离（S 安全修复）：
    # 此前这里是 `Path(root).resolve()` —— 没有任何沙箱，等于以**进程 CWD** 为根。
    # 实测 `glob_files(pattern="README*", root=".")` 会返回仓库外的
    # X:\project\Chiron\README.md，即 LLM 可以借它列出沙箱外的文件；
    # 而同类工具 grep_files 一直是有沙箱的（见 core.py）。这里统一到 workspace_dir()。
    base = workspace_dir()
    if root and root not in (".", "./"):
        candidate = (base / root).resolve()
        try:
            candidate.relative_to(base.resolve())
        except ValueError:
            return {"error": "path escapes sandbox", "count": 0, "files": []}
        base = candidate
    # pathlib.Path.glob already supports **, *, ?, []
    matches: list[dict[str, Any]] = []
    try:
        for p in sorted(base.glob(pattern)):
            if p.is_file():
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                matches.append(
                    {
                        "path": str(p),
                        "size": size,
                    }
                )
    except (ValueError, OSError) as exc:
        return {"error": str(exc), "count": 0, "files": []}

    return {"count": len(matches), "files": matches}


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------
registry.register(
    name="glob_files",
    description="Find files matching a glob pattern (supports **, *, ?, [])",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": 'Glob pattern, e.g. "**/*.py", "src/**/*.go"',
            },
            "root": {
                "type": "string",
                "description": "Root directory to search from",
                "default": ".",
            },
        },
        "required": ["pattern"],
    },
    handler=glob_files,
)
