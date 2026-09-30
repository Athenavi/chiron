"""edit_file 工具 — Claude Code 风格的精确编辑。

支持：
1. 精确字符串替换 (old_string → new_string)
2. 行范围编辑 (start_line, end_line → new_content)
3. 统一 diff 输出
"""

from __future__ import annotations

import difflib
from typing import Any

from app.tools.registry import registry


async def edit_file(
    path: str,
    old_string: str | None = None,
    new_string: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    new_content: str | None = None,
) -> dict[str, Any]:
    """Edit a file and return a unified diff of the changes.

    Modes:
      - Exact replacement: provide *old_string* and *new_string*.
        *old_string* must occur exactly once in the file.
      - Line-range replacement: provide *start_line*, *end_line* (1-based,
        inclusive) and *new_content*.
    """
    from app.backends.context import get_backend
    from app.tools.sandbox import safe_join

    backend = get_backend()
    # 沙箱隔离：路径一律 clamp 到工作区（此前有个 `root` 参数被传进来又被忽略 ——
    # schema 说它能改"path safety 的根"，实现根本不看它。已移除，免得模型以为能改根）
    target = safe_join(path)

    # 批 B：读**全文原文**经后端（`limit=0` 的语义见 `ReadResult` 的文档）——
    # 这里不能用默认分页：唯一性校验与 diff 都要求完整且逐字节精确的内容。
    read = await backend.read(path, limit=0)
    if read.error:
        return {"error": read.error}

    original = read.content
    original_lines = original.splitlines(keepends=True)
    new_lines: list[str] | None = None

    # ── Mode 1: exact string replacement ──────────────────────────
    if old_string is not None:
        if new_string is None:
            return {"error": "new_string is required when old_string is provided"}
        count = original.count(old_string)
        if count == 0:
            return {"error": "old_string not found in file"}
        if count > 1:
            return {"error": f"old_string occurs {count} times; it must be unique"}
        modified = original.replace(old_string, new_string, 1)

    # ── Mode 2: line-range replacement ────────────────────────────
    elif start_line is not None and end_line is not None:
        if new_content is None:
            return {"error": "new_content is required for line-range editing"}
        total = len(original_lines)
        if start_line < 1 or end_line < start_line or start_line > total:
            return {
                "error": f"invalid range ({start_line}, {end_line}) for file with {total} lines"
            }
        # Clamp end_line to file length
        end_line = min(end_line, total)
        before = original_lines[: start_line - 1]
        after = original_lines[end_line:]
        # Ensure the replacement ends with a newline if it doesn't already
        if new_content and not new_content.endswith("\n"):
            new_content += "\n"
        new_lines = before + [new_content] + after
        modified = "".join(new_lines)

    else:
        return {
            "error": "provide (old_string, new_string) or (start_line, end_line, new_content)"
        }

    # ── read-before-write：**两种模式都要过** ──
    #
    # 此前只有精确替换模式做了这项检查，行范围模式能绕开 —— 同一个工具里两套口径，
    # 等于给"基于过期视图编辑"留了个后门。检查点放在两个分支汇合之后，形态上也更稳
    # （新增编辑模式时不会再漏）。
    from app.tools.fs_guard import check_before_write

    conflict = check_before_write(target)
    if conflict:
        return {"error": conflict}

    # ── Write & diff ──────────────────────────────────────────────
    # 写入前快照：`/undo` 据此真正恢复（此前 /undo 只回显字符串，文件没有任何变化）。
    from app.agent import undo_stack
    from app.tools.context import get_session_id

    undo_note = await undo_stack.snapshot_before_write(get_session_id(), target, "edit_file")
    # 批 B：写入经后端（默认 = 本地工作区）
    written = await backend.write(path, modified)
    if written.error:
        return {"error": written.error}

    diff_lines = difflib.unified_diff(
        original.splitlines(keepends=True),
        modified.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    diff_text = "".join(diff_lines)

    payload: dict[str, Any] = {
        "path": str(target),
        "success": True,
        "diff": diff_text,
    }
    if undo_note:
        payload["undo_warning"] = undo_note
    return payload


# ── Register ─────────────────────────────────────────────────────
registry.register(
    name="edit_file",
    description="Edit a file with exact string replacement or line-range replacement; returns a unified diff",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to edit"},
            "old_string": {
                "type": "string",
                "description": "Exact string to find (must occur once)",
            },
            "new_string": {"type": "string", "description": "Replacement string"},
            "start_line": {
                "type": "integer",
                "description": "1-based start line for range edit",
            },
            "end_line": {
                "type": "integer",
                "description": "1-based inclusive end line for range edit",
            },
            "new_content": {
                "type": "string",
                "description": "Replacement content for line-range edit",
            },
        },
        "required": ["path"],
    },
    handler=edit_file,
)
