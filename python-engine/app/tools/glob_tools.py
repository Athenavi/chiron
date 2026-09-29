"""Glob 工具集 — 文件模式匹配搜索。"""

from __future__ import annotations

from typing import Any

from app.tools.registry import registry


async def glob_files(pattern: str, root: str = ".") -> dict[str, Any]:
    """Find files matching a glob pattern (supports **, *, ?, []).

    Returns a list of matching file paths with their sizes.
    """
    from app.backends.context import get_backend

    # 批 B：匹配经**后端**（默认 = 本地工作区）。
    #
    # 沙箱隔离（S 安全修复）：此前这里是 `Path(root).resolve()` —— 没有任何沙箱，
    # 等于以**进程 CWD** 为根。实测 `glob_files(pattern="README*", root=".")` 会返回仓库外的
    # X:\project\Chiron\README.md，即 LLM 可以借它列出沙箱外的文件。
    # 现在由后端的 `safe_join` 统一拒绝越界路径。
    backend = get_backend()
    try:
        globbed = await backend.glob(pattern, path=root or ".")
    except ValueError:
        return {"error": "path escapes sandbox", "count": 0, "files": []}
    if globbed.error:
        return {"error": globbed.error, "count": 0, "files": []}

    matches: list[dict[str, Any]] = []
    for candidate in globbed.paths:
        info = await backend.stat(candidate)
        if info is None or not info.is_file:
            continue
        matches.append({"path": info.path, "size": info.size})

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
