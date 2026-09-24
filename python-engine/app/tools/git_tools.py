"""Git 工具集 — 通过 subprocess 执行常用 git 命令。"""

from __future__ import annotations

import asyncio
from typing import Any

from app.tools.registry import registry


async def _run_git(*args: str, cwd: str = ".", timeout: int = 30) -> dict[str, Any]:
    """Run a git command and return stdout/stderr/exit_code.

    使用 sandboxed_env 清理环境变量，防止宿主密钥泄露给 git 子进程。
    超时控制防止 git 命令卡住。
    """
    from app.tools.sandbox import sandboxed_env, workspace_dir

    cmd = ["git", *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(workspace_dir()),
        env=sandboxed_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return {"error": "timeout", "timeout": timeout}
    return {
        "exit_code": proc.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


async def _ensure_sandbox_repo() -> dict[str, Any] | None:
    """校验当前 workspace 的 git repo 根未逃逸出沙箱（S 安全修复）。

    sandbox 若位于宿主 git 仓库内，git 命令会向上穿透到宿主 .git——
    这里用 rev-parse --show-toplevel 检查 repo 根必须在 workspace 内。
    返回 None 表示通过；否则返回错误 dict。
    """
    import os

    from app.tools.sandbox import workspace_dir

    ws = os.path.normpath(str(workspace_dir()))
    check = await _run_git("rev-parse", "--show-toplevel")
    if "error" in check:
        # 非 git 仓库（或命令失败）→ 无穿透风险
        return None
    top = os.path.normpath(check["stdout"].strip())
    if not top:
        return None
    if top != ws and not top.startswith(ws + os.sep):
        return {"error": f"git repository root escapes sandbox: {top}"}
    return None


async def git_status(root: str = ".") -> dict[str, Any]:
    """Return git status (modified, added, deleted files)."""
    guard = await _ensure_sandbox_repo()
    if guard:
        return guard
    result = await _run_git("status", "--porcelain", cwd=root)
    if "error" in result:
        return result
    # NOTE: do NOT .strip() the whole stdout — that eats the leading space of the
    # first porcelain line (e.g. " M file" becomes "M file", breaking the offset).
    lines = [ln.rstrip("\r") for ln in result["stdout"].splitlines() if ln.strip()]
    files: list[dict[str, str]] = []
    for line in lines:
        # Porcelain format: XY PATH  (XY = 2-char status, separated by a space from the path)
        if len(line) < 4:
            continue
        status = line[:2].strip()
        path = line[3:]
        files.append({"status": status, "path": path})
    return {
        "exit_code": result["exit_code"],
        "count": len(files),
        "files": files,
        "raw": result["stdout"].strip(),
    }


async def git_diff(root: str = ".", staged: bool = False) -> dict[str, Any]:
    """Return diff of changes. If staged=True, shows staged changes."""
    args = ["diff"]
    if staged:
        args.append("--cached")
    result = await _run_git(*args, cwd=root)
    if "error" in result:
        return result
    return {
        "exit_code": result["exit_code"],
        "diff": result["stdout"],
        "staged": staged,
    }


async def git_log(root: str = ".", limit: int = 10) -> dict[str, Any]:
    """Return recent commits."""
    result = await _run_git("log", "--oneline", f"-{limit}", cwd=root)
    if "error" in result:
        return result
    lines = [ln for ln in result["stdout"].strip().splitlines() if ln]
    commits: list[dict[str, str]] = []
    for line in lines:
        parts = line.split(" ", 1)
        commits.append(
            {"hash": parts[0], "message": parts[1] if len(parts) > 1 else ""}
        )
    return {"exit_code": result["exit_code"], "count": len(commits), "commits": commits}


async def git_commit(message: str, root: str = ".") -> dict[str, Any]:
    """Stage all changes and commit with the given message."""
    # Stage all
    stage = await _run_git("add", "-A", cwd=root)
    if "error" in stage:
        return stage
    if stage["exit_code"] != 0:
        return {"error": stage["stderr"].strip(), "exit_code": stage["exit_code"]}

    # Commit
    result = await _run_git("commit", "-m", message, cwd=root)
    if "error" in result:
        return result
    return {
        "exit_code": result["exit_code"],
        "message": message,
        "output": (result["stdout"] + result["stderr"]).strip(),
    }


async def git_branch(root: str = ".") -> dict[str, Any]:
    """List branches and show current branch."""
    result = await _run_git("branch", cwd=root)
    if "error" in result:
        return result
    lines = [ln for ln in result["stdout"].strip().splitlines() if ln]
    current = ""
    branches: list[str] = []
    for line in lines:
        name = line.strip()
        if name.startswith("* "):
            name = name[2:]
            current = name
        branches.append(name)
    return {"exit_code": result["exit_code"], "current": current, "branches": branches}


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------
registry.register(
    name="git_status",
    description="Return git status (modified, added, deleted files)",
    parameters={
        "type": "object",
        "properties": {
            "root": {
                "type": "string",
                "description": "Repository root",
                "default": ".",
            },
        },
        "required": [],
    },
    handler=git_status,
)

registry.register(
    name="git_diff",
    description="Return diff of changes (optionally staged)",
    parameters={
        "type": "object",
        "properties": {
            "root": {
                "type": "string",
                "description": "Repository root",
                "default": ".",
            },
            "staged": {
                "type": "boolean",
                "description": "Show staged changes",
                "default": False,
            },
        },
        "required": [],
    },
    handler=git_diff,
)

registry.register(
    name="git_log",
    description="Return recent commits",
    parameters={
        "type": "object",
        "properties": {
            "root": {
                "type": "string",
                "description": "Repository root",
                "default": ".",
            },
            "limit": {
                "type": "integer",
                "description": "Max commits to return",
                "default": 10,
            },
        },
        "required": [],
    },
    handler=git_log,
)

registry.register(
    name="git_commit",
    description="Stage all changes and commit",
    parameters={
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Commit message"},
            "root": {
                "type": "string",
                "description": "Repository root",
                "default": ".",
            },
        },
        "required": ["message"],
    },
    handler=git_commit,
)

registry.register(
    name="git_branch",
    description="List branches and show current branch",
    parameters={
        "type": "object",
        "properties": {
            "root": {
                "type": "string",
                "description": "Repository root",
                "default": ".",
            },
        },
        "required": [],
    },
    handler=git_branch,
)
