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

#: `edit_file` 愿意处理的文件大小上限。
#:
#: 编辑**必然**要全文精确内容（唯一性校验与替换都以它为准），这一步省不掉；
#: 但可以省掉"把它读进内存然后 OOM"。超过上限时明确拒绝并指路，而不是
#: 让进程在分配几 GB 字符串时被 OOM killer 干掉 —— 那种失败连错误信息都留不下。
#:
#: 8MB 的依据：文本源码/配置极少超过它；超过的多半是生成物、日志、数据文件，
#: 那些本就该用脚本改或用 `write_file` 分块重写。
EDIT_FILE_MAX_BYTES = 8 * 1024 * 1024

#: 返回给模型的 diff 最多多少行。
#:
#: `difflib` 对"整体重写型"改动（如行区间替换整段）会输出全文 diff，
#: 一个 5000 行文件的改动能让单条工具结果吃掉几十万字符 —— 恰好是最该省的地方。
#: 超出时保留**头尾各一半**并明确标注省略量：改动开头与结尾通常正是判断
#: 「改对没有」所需的证据。
EDIT_DIFF_MAX_LINES = 400


def _clip_diff(diff_text: str, max_lines: int = EDIT_DIFF_MAX_LINES) -> tuple[str, bool, int]:
    """把超长 diff 折成头尾两段；返回 `(文本, 是否截断, 原始行数)`。"""
    lines = diff_text.splitlines()
    total = len(lines)
    if total <= max_lines:
        return diff_text, False, total
    head_n = max_lines // 2
    tail_n = max_lines - head_n
    omitted = total - head_n - tail_n
    clipped = [
        *lines[:head_n],
        f"… [{omitted} diff lines omitted — 文件已正确写入，这里只折叠了回显] …",
        *lines[-tail_n:],
    ]
    return "\n".join(clipped), True, total


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

    # ── 大小闸门：**先看大小再读**，别把几百 MB 读进内存才拒绝 ──
    # 编辑要全文精确内容，这步省不掉；但"读进来才发现该拒绝"会让进程在分配
    # 巨型字符串时被 OOM 干掉 —— 那种失败连错误信息都留不下。
    try:
        info = await backend.stat(path)
    except Exception:  # noqa: BLE001 — 后端不支持 stat 时退化为读后检查
        info = None
    if info is not None and getattr(info, "size", 0) > EDIT_FILE_MAX_BYTES:
        return {
            "error": (
                f"文件过大（{info.size} 字节 > {EDIT_FILE_MAX_BYTES}），edit_file 不做全文编辑。"
                "改用 write_file 分块重写、或先用脚本/命令处理；"
                "若只是想看某段内容，用 read_file 的 offset/limit 或 grep_files 定位。"
            ),
            "size_bytes": info.size,
            "max_bytes": EDIT_FILE_MAX_BYTES,
        }

    # 批 B：读**全文原文**经后端（`limit=0` 的语义见 `ReadResult` 的文档）——
    # 这里不能用默认分页：唯一性校验与 diff 都要求完整且逐字节精确的内容。
    read = await backend.read(path, limit=0)
    if read.error:
        return {"error": read.error}

    original = read.content
    # 兜底：后端可能没有 stat（或报了 -1），读到超限同样拒绝，而不是继续做全文操作。
    if len(original.encode("utf-8", errors="replace")) > EDIT_FILE_MAX_BYTES:
        return {
            "error": (
                f"文件过大（超过 {EDIT_FILE_MAX_BYTES} 字节），edit_file 不做全文编辑。"
                "改用 write_file 分块重写、或先用脚本/命令处理。"
            ),
            "max_bytes": EDIT_FILE_MAX_BYTES,
        }
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
    # 与 `write_file` 同一理由：写成功后刷新读取签名。连续编辑同一文件（改一处 → 再改一处）
    # 是长任务的常态，若不刷新，第二次编辑会被 read-before-write 判成"文件已变"而拒绝。
    from app.tools.fs_guard import observe as _observe

    _observe(target)

    diff_lines = difflib.unified_diff(
        original.splitlines(keepends=True),
        modified.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    diff_text, diff_clipped, diff_total = _clip_diff("".join(diff_lines))

    payload: dict[str, Any] = {
        "path": str(target),
        "success": True,
        "diff": diff_text,
    }
    if diff_clipped:
        # 明确区分"改动就这么多"与"回显被折叠"：文件已经**完整写入**，
        # 折叠的只是回显。不说清楚，模型会以为自己的改动没生效而重写一遍。
        payload["diff_truncated"] = True
        payload["diff_total_lines"] = diff_total
        payload["note"] = (
            f"文件已完整写入；这里只折叠了回显（原 diff 共 {diff_total} 行，"
            f"显示头尾各 {EDIT_DIFF_MAX_LINES // 2} 行）。"
        )
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
