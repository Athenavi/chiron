"""todo_write 工具 —— 任务清单（长任务防跑偏，多 agent 共享一块看板）。

**为什么需要它**：长任务最典型的失败模式是"做着做着忘了目标"。把计划变成**可观测状态**后，
模型每轮都能从 tool_result 回显看到自己写的清单，前端也能显示进度 —— 而不是只存在于
某一轮的自然语言里。

**为什么只有一个工具**：曾经同时存在 `write_todos` 与结构化 `plan` 两套入口，模型经常
选错（该更新状态时写了一整份新计划），前端还要按两种形状各渲染一遍。现在收敛成一个
`todo_write`：参数只有 `todos` 一个键，每项**必填** `content` + `status`，每次调用给出
**完整清单**。结构化契约（`app/agent/plan.py`）仍在内部生效 —— 它是校验与投影层，
不再是第二个对外工具。

**状态存哪**：`app/agent/taskboard.py`（Redis，跨 agent 共享；无 Redis 时回落 contextvars）。
**不堆积进消息历史** —— 模型通过 tool_result 回显看到清单即可；这样也避免"每轮都把
整份清单重新喂一遍"的 token 浪费。

**多 agent**：主 agent 整份替换，子 agent 按 `id` 合并（详见 taskboard 模块文档）。
认领者由 host 从 tool context 判定后覆盖，模型自报的 `owner` 不作数。
"""

from __future__ import annotations

from typing import Any

from app.tools.registry import registry

#: 清单条目上限（防模型写一份几千行的清单把上下文撑爆）
MAX_TODOS = 50

#: 允许的状态。中文别名一并接受 —— 模型在多语言上下文中常直接写"进行中"，
#: 直接拒绝只会触发一轮无谓的重试；归一化到英文规范值即可。
STATUS_ALIASES: dict[str, str] = {
    "pending": "pending",
    "todo": "pending",
    "待处理": "pending",
    "待办": "pending",
    "未开始": "pending",
    "in_progress": "in_progress",
    "in-progress": "in_progress",
    "doing": "in_progress",
    "开始": "in_progress",
    "进行中": "in_progress",
    "completed": "completed",
    "done": "completed",
    "完成": "completed",
    "已完成": "completed",
}

VALID_STATUSES: frozenset[str] = frozenset({"pending", "in_progress", "completed"})

#: 工具名常量（runtime 靠它识别"清单写入"以产出事件、并做批内去重）
WRITE_TODOS_TOOL = "todo_write"


def normalize_status(value: Any) -> str:
    """把模型给的状态归一化为规范值；无法识别返回空串（由调用方判非法）。"""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return STATUS_ALIASES.get(text, "")


def _render(todos: list[dict[str, Any]]) -> str:
    """渲染成紧凑文本（供 tool_result 回显；不写进消息历史正文）。"""
    marks = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
    return "\n".join(f"{marks.get(t['status'], '[ ]')} {t['content']}" for t in todos)


async def todo_write(todos: list[dict[str, Any]]) -> dict[str, Any]:
    """写入/更新任务清单。**每次给出完整清单**。

    参数形状是刻意的窄：每项只要 `content`（任务描述）与 `status`（状态）两个**必填**
    字段。历史版本允许省略 `status`（缺省成 `pending`），结果是模型写清单时省掉状态、
    之后也从不回来更新 —— 面板上所有条目永远是"待处理"，而"及时更新状态"这个唯一
    有价值的行为恰好从未发生。必填强制它每次对每一项表态。

    可选字段（不填不影响）：
    ``files`` / ``verified``（候选与已核实文件）、``owner``（认领者，host 覆盖）、
    ``depends_on``（前序步骤 id，必须真实存在且不成环）。

    Args:
        todos: 清单条目列表，每项形如
            ``{"content": "…", "status": "pending"}``。
            ``status`` ∈ ``pending`` / ``in_progress`` / ``completed``（也接受
            "待处理/进行中/已完成"等别名）。
    """
    if not isinstance(todos, list) or not todos:
        # 空列表：**拒绝**。清空清单应该是"换个新的完整列表"，而不是"提交一个空列表"
        # （后者会让前端拿到一份渲染不出来的清单，且掩盖"忘了写条目"这种真错）。
        return {"error": "todos must be a non-empty list"}

    from app.agent import taskboard

    # ── 第一道：形状与状态的**显式**校验 ──
    #
    # 放在 normalize 之前，是因为 normalize 是"宽进严出"的宽容层（它接受缺省 status
    # 以便别处复用）。工具层必须更严：两个必填字段缺一不可。
    normalized_input: list[dict[str, Any]] = []
    missing: list[str] = []
    for index, item in enumerate(todos):
        if not isinstance(item, dict):
            missing.append(f"todos[{index}] must be an object")
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            missing.append(f"todos[{index}] is missing required field 'content'")
        if "status" not in item or not str(item.get("status", "")).strip():
            missing.append(f"todos[{index}] is missing required field 'status'")
            continue
        status = normalize_status(item.get("status"))
        if not status:
            missing.append(
                f"todos[{index}] has invalid status {item.get('status')!r} "
                f"(allowed: {sorted(VALID_STATUSES)})"
            )
            continue
        normalized_input.append({**item, "content": content, "status": status})
    if missing:
        # 整份拒绝而不是部分接受 —— 半份清单比没有清单更误导。
        return {"error": "invalid todos: " + "; ".join(missing)}

    from app.agent.plan import normalize, render, to_todos

    plan, errors = normalize(normalized_input)
    if errors:
        return {"error": "invalid todos: " + "; ".join(errors)}

    # ── 第二道：认领者由 host 覆盖（模型自报的不作数）──
    #
    # 让模型自己填 owner 等于让它给自己发身份：子 agent 可以声称自己是 main，
    # 从而把主 agent 的条目"认领"走。真实身份只有 tool context 知道。
    from app.tools.context import get_tool_context

    depth_raw = get_tool_context("subagent_depth", 0)
    try:
        depth = int(depth_raw or 0)
    except (TypeError, ValueError):
        depth = 0
    host_owner = str(get_tool_context("subagent_run_id", "") or "")
    if not host_owner:
        host_owner = "main" if depth == 0 else "subagent"
    for step in plan.steps:
        # frozen dataclass：用 object.__setattr__ 之外的方式重建，保持不可变性可读
        step_owner = host_owner if depth > 0 else (step.owner or host_owner)
        if depth > 0 and step.owner != step_owner:
            # 子 agent 写下的条目一律归它自己 —— 它只可能在做自己派到的活。
            object.__setattr__(step, "owner", step_owner)

    projection = to_todos(plan)
    payload = await taskboard.write(projection)
    stored = payload.get("todos") if isinstance(payload.get("todos"), list) else projection

    return {
        "output": render(plan),
        "todos": stored,
        "count": len(stored),
        "summary": taskboard.summarize(stored),
        # revision 让前端能丢弃乱序到达的旧事件，避免面板倒退。
        "revision": int(payload.get("revision") or 0),
    }


def current_todos() -> list[dict[str, Any]]:
    """**同步**读回进程内最新清单（供测试与同进程内协同）。

    跨进程/跨 agent 的真实来源是 `app.agent.taskboard.load()`（异步）。这里读的是
    taskboard 在 contextvars 里同步维护的那份副本 —— 它每次写入都会更新，所以对
    同进程调用方是最新的；若本进程从不写清单，则返回空。
    """
    from app.tools.context import get_tool_context

    stored = get_tool_context("todos", {})
    if isinstance(stored, dict):
        todos = stored.get("todos")
        return list(todos) if isinstance(todos, list) else []
    return list(stored) if isinstance(stored, list) else []


registry.register(
    name=WRITE_TODOS_TOOL,
    description=(
        "Create or update your task list. Pass the FULL list every time — it replaces the "
        "previous one. Every item MUST have both `content` and `status`. "
        "Use it for multi-step work: mark an item `in_progress` when you START it and switch "
        "it to `completed` IMMEDIATELY when you finish it — do not batch several completions "
        "into one late update. When delegated to a subagent, you may send only your own items "
        "with their `id`s; they are merged into the shared board instead of replacing it. "
        "Optional: `owner` (assigned by the host), `depends_on` (ids of steps that must finish "
        "first), `files`/`verified` (candidate and actually-verified files). "
        "Do not call it more than once per message — parallel writes would just overwrite each other."
    ),
    parameters={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "Full task list (replaces the previous one for the main agent)",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "What to do"},
                        "status": {
                            "type": "string",
                            "enum": sorted(VALID_STATUSES),
                            "description": "pending / in_progress / completed",
                        },
                        "id": {
                            "type": "string",
                            "description": (
                                "Host-assigned stable id (t1, t2…). Pass it back to update an "
                                "existing item; omit for a new one."
                            ),
                        },
                        "owner": {
                            "type": "string",
                            "description": "Claimant (assigned by the host; do not set it yourself)",
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Ids of steps that must be completed first",
                        },
                        "files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Files this step is expected to touch",
                        },
                        "verified": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Files actually read/edited (must be a subset of files)",
                        },
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    },
    handler=todo_write,
)
