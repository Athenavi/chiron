"""引擎事件 → ACP 更新语义（**纯函数，不依赖 ACP SDK**）。

适配层的价值有一半在这里：把引擎的 SSE 事件翻译成"客户端该显示什么"。把它做成不依赖 SDK 的纯函数
有两个好处 —— 它能在**联调之前**被完整单测；SDK 换版时改动只落在薄薄的接线层（`agent.py`）。

这里只产出**我们自己的中立结构**，不碰 wire 格式（那是 SDK 的事，见 `agent.py`）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

_THINKING_OPEN = "[thinking]"
_THINKING_CLOSE = "[/thinking]"


@dataclass
class TextDelta:
    """助手可见文本增量。"""

    text: str


@dataclass
class ThoughtDelta:
    """思考增量。

    引擎把思考内联在 `text` 事件里（`[thinking]…[/thinking]`），而 ACP 有独立的 thought 通道 ——
    所以这里**切成两类增量**，而不是丢掉思考：编辑器里能看到"它在想什么"，但不把它当回答。
    """

    text: str


@dataclass
class ToolStarted:
    """工具调用开始（ACP 的 tool call start）。"""

    call_id: str
    name: str
    arguments: str = ""


@dataclass
class ToolFinished:
    """工具调用结束。`ok=False` 时 `detail` 是原因（含"被拒"——那是护栏证据）。"""

    call_id: str
    ok: bool = True
    detail: str = ""


@dataclass
class PermissionAsk:
    """需要用户裁决的工具调用（ACP 的 permission 请求）。"""

    call_id: str
    tool: str
    arguments: str = ""
    #: 供客户端渲染的选项。ACP 侧据此给出批准/拒绝按钮。
    options: list[str] = field(default_factory=lambda: ["approve", "reject"])


@dataclass
class Notice:
    """不属于上面几类的提示（护栏拦截、计划、需要额外输入等）。

    刻意保留成"提示"而不是硬塞进某个 ACP 概念：ACP 没有与 `ask_user` / 待办清单一一对应的通道，
    硬塞会让两边语义都变形。`kind` 供客户端决定怎么显示。
    """

    kind: str
    detail: str = ""


@dataclass
class RunFinished:
    """本轮结束。`reason` ∈ `done` / `cancelled` / `error` / `blocked`。"""

    reason: str
    detail: str = ""


Update = (
    TextDelta | ThoughtDelta | ToolStarted | ToolFinished | PermissionAsk | Notice | RunFinished
)


def split_thinking(text: str) -> list[TextDelta | ThoughtDelta]:
    """把内联的思考标记切成"思考增量 + 正文增量"两类。

    与评测侧（`evals/observe.py`）的做法不同：那里是**剥掉**思考（评测只关心最终回答），
    这里要**保留**它 —— 编辑器里看到思考是有价值的。同样的输入，两种用途两种处理。
    """
    if _THINKING_OPEN not in text:
        return [TextDelta(text)] if text else []

    out: list[TextDelta | ThoughtDelta] = []
    depth = 0
    buffer: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith(_THINKING_OPEN, index):
            if buffer:
                out.append(
                    (ThoughtDelta if depth else TextDelta)("".join(buffer))
                )
                buffer = []
            depth += 1
            index += len(_THINKING_OPEN)
            continue
        if text.startswith(_THINKING_CLOSE, index):
            if buffer:
                out.append(
                    (ThoughtDelta if depth else TextDelta)("".join(buffer))
                )
                buffer = []
            depth = max(0, depth - 1)
            index += len(_THINKING_CLOSE)
            continue
        buffer.append(text[index])
        index += 1

    if buffer:
        out.append((ThoughtDelta if depth else TextDelta)("".join(buffer)))
    return out


def _error_of(raw: Any) -> str:
    """从 `tool_result.content`（JSON 字符串）里取 `error`；取不到返回空串。"""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if isinstance(payload, dict):
        return str(payload.get("error") or "")
    return ""


def translate(event: dict[str, Any]) -> list[Update]:
    """把一个引擎事件翻译成零个或多个 ACP 更新。

    **未知类型一律忽略**（返回空列表）：引擎会加新事件，适配层不该因为不认识就崩 —— 但这也不
    等于"接了就完事"：新事件要不要面向用户，是每次加事件时该想一下的事。
    """
    etype = str(event.get("type", ""))

    if etype == "text":
        return list(split_thinking(str(event.get("content", ""))))

    if etype == "thinking":
        # A1（方案 04）：native reasoning 走独立事件 —— **不再需要从 text 里猜**。
        # 注意 `text` 分支的 `split_thinking` **保留**：模型自产的 `[thinking]…[/thinking]`
        # 标记（prompt 教的）仍然出现在正文里，那是另一条通道。
        return [ThoughtDelta(str(event.get("content", "")))]

    if etype == "tool_call":
        return [
            ToolStarted(
                call_id=str(event.get("tool_call_id", "")),
                name=str(event.get("tool_name", "")),
                arguments=str(event.get("tool_arguments", "")),
            )
        ]

    if etype == "tool_result":
        error = _error_of(event.get("content"))
        return [
            ToolFinished(
                call_id=str(event.get("tool_call_id", "")),
                ok=not error,
                detail=error,
            )
        ]

    if etype == "approval":
        return [
            PermissionAsk(
                call_id=str(event.get("tool_call_id", "")),
                tool=str(event.get("tool_name", "")),
                arguments=str(event.get("tool_arguments", "")),
            )
        ]

    if etype == "guardrail_blocked":
        return [Notice(kind="guardrail", detail=str(event.get("content", "")))]

    if etype == "todo_updated":
        return [Notice(kind="plan", detail=str(event.get("content", "")))]

    if etype == "ask":
        # ACP 没有与 ask_user 一一对应的通道（只有 permission 请求）。把它降级成一条提示，
        # 而不是硬塞进 permission —— 那会让客户端把"回答问题"显示成"批准危险操作"。
        return [Notice(kind="ask", detail=str(event.get("content", "")))]

    if etype == "cancelled":
        return [RunFinished("cancelled", str(event.get("content", "")))]

    if etype == "done":
        return [RunFinished("done")]

    if etype == "error":
        return [RunFinished("error", str(event.get("error", "")))]

    # trace_span / compaction / rubric 等：面向运维与评测，不面向编辑器用户
    return []
