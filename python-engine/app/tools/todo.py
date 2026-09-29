"""write_todos 工具 —— 计划状态（S6a，方案 01 §4.6）。

**为什么需要它**：长任务最典型的失败模式是"做着做着忘了目标"。把计划变成**可观测状态**后，
模型每轮都能看到自己写的计划，前端也能显示进度 —— 而不是只存在于某一轮的自然语言里。

**与 deepagents 的对应**：它的 `TodoListMiddleware`（由 langchain 提供）要求"同一条消息内
只允许一次 `write_todos`" —— 并发写 todo 没有意义，只会互相覆盖。这里沿用该约束，
但把检查放在 **runtime 的批处理**里（只有那一层知道"哪些调用属于同一批"）。

**状态存哪**：会话级 contextvars（`app/tools/context.py`）。**不堆积进消息历史** ——
模型通过 tool_result 回显看到计划即可；这样做也避免"每轮都把整份计划重新喂一遍"的 token 浪费。
"""

from __future__ import annotations

from typing import Any

from app.tools.registry import registry

#: 计划条目上限（防模型写一份几千行的计划把上下文撑爆）
MAX_TODOS = 50
#: 允许的状态（与 deepagents 的 todo 状态一致）
VALID_STATUSES: frozenset[str] = frozenset({"pending", "in_progress", "completed"})

#: 工具名常量（runtime 靠它识别"计划写入"以产出事件、并做批内去重）
WRITE_TODOS_TOOL = "write_todos"


def _render(todos: list[dict[str, Any]]) -> str:
    """渲染成紧凑文本（供 tool_result 回显；不写进消息历史正文）。"""
    marks = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
    return "\n".join(f"{marks.get(t['status'], '[ ]')} {t['content']}" for t in todos)


async def write_todos(todos: list[dict[str, Any]]) -> dict[str, Any]:
    """写入/更新计划（**整份替换**，与 deepagents 的语义一致：一次给全量列表）。

    Args:
        todos: 计划条目列表，每项形如 `{"content": "…", "status": "pending"}`。
            `status` ∈ `pending` / `in_progress` / `completed`；缺省 `pending`。
    """
    if not isinstance(todos, list) or not todos:
        return {"error": "todos must be a non-empty list"}
    if len(todos) > MAX_TODOS:
        return {"error": f"too many todos ({len(todos)} > {MAX_TODOS})"}

    normalized: list[dict[str, str]] = []
    for index, item in enumerate(todos):
        if not isinstance(item, dict):
            return {"error": f"todo[{index}] must be an object"}
        content = str(item.get("content", "")).strip()
        if not content:
            return {"error": f"todo[{index}] is missing 'content'"}
        status = str(item.get("status", "pending"))
        if status not in VALID_STATUSES:
            return {
                "error": (
                    f"todo[{index}] has invalid status {status!r} "
                    f"(allowed: {sorted(VALID_STATUSES)})"
                )
            }
        normalized.append(
            {"id": str(item.get("id") or f"t{index + 1}"), "content": content, "status": status}
        )

    from app.tools.context import set_tool_context

    set_tool_context(todos=normalized)

    counts = {status: sum(1 for t in normalized if t["status"] == status) for status in sorted(VALID_STATUSES)}
    return {
        "output": _render(normalized),
        "todos": normalized,
        "count": len(normalized),
        "summary": counts,
    }


def current_todos() -> list[dict[str, Any]]:
    """读回当前计划（供 runtime 产出事件、或其它工具协同）。"""
    from app.tools.context import get_tool_context

    stored = get_tool_context("todos", [])
    return list(stored) if isinstance(stored, list) else []


registry.register(
    name=WRITE_TODOS_TOOL,
    description=(
        "Create or update your task plan as a todo list. Pass the FULL list every time "
        "(it replaces the previous one). Use it for multi-step work: mark exactly one item "
        "`in_progress` while you work on it and switch it to `completed` when done. "
        "Do not call it more than once per message — parallel writes would just overwrite each other."
    ),
    parameters={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "Full todo list (replaces the previous one)",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "What to do"},
                        "status": {
                            "type": "string",
                            "enum": sorted(VALID_STATUSES),
                            "description": "pending / in_progress / completed",
                        },
                        "id": {"type": "string", "description": "Optional stable id"},
                    },
                    "required": ["content"],
                },
            }
        },
        "required": ["todos"],
    },
    handler=write_todos,
)
