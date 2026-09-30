"""Git 工具集 — 通过 subprocess 执行常用 git 命令。"""

from __future__ import annotations

import asyncio
from typing import Any

from app.tools.registry import registry


async def _run_git(*args: str, timeout: int = 30) -> dict[str, Any]:
    """Run a git command in the sandbox workspace and return stdout/stderr/exit_code.

    使用 sandboxed_env 清理环境变量，防止宿主密钥泄露给 git 子进程。
    超时控制防止 git 命令卡住。

    **cwd 刻意不可传**：git 命令一律在工作区内执行（此前有个 `cwd` 参数被传进来又被忽略，
    是"参数说谎"的一种）。逃逸检查见 :func:`_run_git_guarded`。
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


async def _run_git_guarded(*args: str, timeout: int = 30) -> dict[str, Any]:
    """**所有** git 工具的统一入口：先过逃逸检查，再执行。

    为什么必须统一：此前只有 `git_status` 调了 `_ensure_sandbox_repo`，而
    `git_diff` / `git_log` / `git_commit` / `git_branch` 直接执行 —— 工作区位于宿主
    仓库内时，它们会**向上穿透**：`git_diff` 泄露宿主改动、`git_commit` 直接在宿主仓库
    提交。检查放在入口而不是逐个工具里，是为了"新增 git 工具时不会再漏"。
    """
    guard = await _ensure_sandbox_repo()
    if guard:
        return guard
    return await _run_git(*args, timeout=timeout)


async def git_status() -> dict[str, Any]:
    """Return git status (modified, added, deleted files)."""
    result = await _run_git_guarded("status", "--porcelain")
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


async def git_diff(staged: bool = False) -> dict[str, Any]:
    """Return diff of changes. If staged=True, shows staged changes."""
    args = ["diff"]
    if staged:
        args.append("--cached")
    result = await _run_git_guarded(*args)
    if "error" in result:
        return result
    return {
        "exit_code": result["exit_code"],
        "diff": result["stdout"],
        "staged": staged,
    }


async def git_log(limit: int = 10) -> dict[str, Any]:
    """Return recent commits."""
    result = await _run_git_guarded("log", "--oneline", f"-{limit}")
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


async def git_commit(message: str) -> dict[str, Any]:
    """Stage all changes and commit with the given message."""
    # Stage all
    stage = await _run_git_guarded("add", "-A")
    if "error" in stage:
        return stage
    if stage["exit_code"] != 0:
        return {"error": stage["stderr"].strip(), "exit_code": stage["exit_code"]}

    # Commit
    result = await _run_git_guarded("commit", "-m", message)
    if "error" in result:
        return result
    return {
        "exit_code": result["exit_code"],
        "message": message,
        "output": (result["stdout"] + result["stderr"]).strip(),
    }


async def git_branch() -> dict[str, Any]:
    """List branches and show current branch."""
    result = await _run_git_guarded("branch")
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
    description="Show working-tree status inside the sandbox workspace (modified/added/deleted files).",
    parameters={"type": "object", "properties": {}, "required": []},
    handler=git_status,
)

registry.register(
    name="git_diff",
    description="Show the diff of uncommitted changes in the sandbox workspace.",
    parameters={
        "type": "object",
        "properties": {
            "staged": {
                "type": "boolean",
                "description": "Show staged (--cached) changes instead of the working tree",
                "default": False,
            },
        },
        "required": [],
    },
    handler=git_diff,
)

registry.register(
    name="git_log",
    description="List recent commits in the sandbox workspace.",
    parameters={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max commits to return (default 10)",
                "default": 10,
            },
        },
        "required": [],
    },
    handler=git_log,
)

registry.register(
    name="git_commit",
    description=(
        "Stage all changes in the sandbox workspace and commit them. "
        "Only the sandbox repository is touched — a workspace nested inside a host repo "
        "is refused rather than committing to the host."
    ),
    parameters={
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Commit message"},
        },
        "required": ["message"],
    },
    handler=git_commit,
)

registry.register(
    name="git_branch",
    description="List branches and show the current branch in the sandbox workspace.",
    parameters={"type": "object", "properties": {}, "required": []},
    handler=git_branch,
)
