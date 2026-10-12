"""工作区文件系统工具：`ls`（列目录）。

**为什么单独一个模块**：`core.py`（351 行）已有 `read_file` / `write_file` /
`grep_files`，这里只加"目录列举"这一类动作，不再往 core 里堆。

**两处易错点（都有历史教训）**

* **注册 ≠ 可用**：`app/tools/__init__.py` 是**唯一**注册入口 —— 本模块必须在那里
  `import`，否则工具"写了但不存在"（`run_code` / `subagent` 都曾如此，见该文件说明）。
* **动作分级要两侧同构**：`ls` 是**只读**，必须同时登记到
  `app/agent/tool_policy.py` 的 `_READ_TOOLS` **与** `internal/api/tool_policy.go`
  的 `add(ToolLevelRead, …)` —— 只改一侧会让 `scripts/check_tool_policy_parity.py`
  失败；不登记则 fail-closed 到 `write`（多一次人工确认，见该文件的 fail-closed 说明）。

**与 `glob_files` 的分工**：`ls` 是**非递归列目录**（一次系统调用，便宜）；
`glob_files` 是**模式匹配、可递归**。deepagents 同时提供 `ls` / `glob` / `grep`，
本工具用于对齐其中的 `ls`。
"""

from __future__ import annotations

from typing import Any

from app.tools.registry import registry


async def list_dir(path: str = ".") -> dict[str, Any]:
    """列出工作区某目录下的条目（非递归）。

    `path` 经后端的 `safe_join` clamp 到工作区：越界返回 error 而不是
    泄漏宿主机路径（与 `glob_files` 同一范式）。
    """
    from app.backends.context import get_backend

    backend = get_backend()
    try:
        result = await backend.ls(path or ".")
    except ValueError:
        return {"error": "path escapes sandbox", "count": 0, "entries": []}
    if result.error:
        return {"error": result.error, "count": 0, "entries": []}
    return {"path": path or ".", "count": len(result.entries), "entries": result.entries}


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------
registry.register(
    name="ls",
    description=(
        "List entries in a workspace directory (non-recursive). "
        "Use glob_files to match patterns recursively."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path relative to the workspace root",
                "default": ".",
            }
        },
        "required": [],
    },
    handler=list_dir,
)


# ---------------------------------------------------------------------------
# delete_file（破坏性：动作分级为 delete）
# ---------------------------------------------------------------------------
async def delete_file(path: str) -> dict[str, Any]:
    """删除工作区里的一个**文件**。

    **破坏性动作**：`delete_file` 在两侧分级表里都是 `delete` 级（人工确认 + 二次校验，
    见 `app/agent/tool_policy.py` 与 `internal/api/tool_policy.go` —— 必须同构）。
    删除前经 `undo_stack` 快照**内容**，因此 `/undo` 能把它真正恢复回来。

    只删文件、不删目录（不递归）—— 递归删除是另一个更危险的动作，不做隐式扩展。
    """
    from app.agent import undo_stack
    from app.backends.context import get_backend
    from app.tools.context import get_session_id
    from app.tools.fs_guard import check_before_write
    from app.tools.sandbox import safe_join

    try:
        target = safe_join(path)
    except ValueError:
        return {"error": "path escapes sandbox", "deleted": False}

    conflict = check_before_write(target)
    if conflict:
        return {"error": conflict}

    # 删除前快照：/undo 据此**真正恢复**文件（与 write_file 同一机制）
    note = await undo_stack.snapshot_before_write(get_session_id(), target, "delete_file")

    result = await get_backend().delete(path)
    if result.error:
        return {"error": result.error, "deleted": False}
    return {
        "path": result.path,
        "deleted": result.deleted,
        "message": "deleted" if result.deleted else "file did not exist",
        "undo": note,
    }


registry.register(
    name="delete_file",
    description=(
        "Delete a file in the workspace. Only removes files (not directories); "
        "requires human approval. Prefer this over shell `rm` so the action is audited."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the workspace root"}
        },
        "required": ["path"],
    },
    handler=delete_file,
)
