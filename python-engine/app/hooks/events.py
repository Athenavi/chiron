"""批 G 生命周期 hook 的事件集与上下文契约（方案 03 §3.1）。

只保留**引擎侧有真实对应点**的 6 个事件。deepagents 的 client 侧事件
（`UserPromptSubmit` / `PermissionRequest` / `Notification`）在 Chiron 无对应
进程 —— 硬造会变成"事件发了但没人用"的死面，故不取。
"""

from __future__ import annotations

from typing import Any

#: 会话开始（一次 run 进入主循环之前）。
SESSION_START = "SessionStart"
#: 工具执行前 —— **唯一可阻断**的事件。
PRE_TOOL_USE = "PreToolUse"
#: 工具执行成功之后。
POST_TOOL_USE = "PostToolUse"
#: 工具执行失败之后（结果里带 `error`）。
POST_TOOL_USE_FAILURE = "PostToolUseFailure"
#: 一次 run 结束（含异常/中断退出路径）。
STOP = "Stop"
#: 子 agent 委派开始 / 结束。
SUBAGENT_START = "SubagentStart"
SUBAGENT_STOP = "SubagentStop"

#: 全部合法事件。注册时据此校验，防止事件名拼写错误后静默失效。
ALL_EVENTS: frozenset[str] = frozenset(
    {
        SESSION_START,
        PRE_TOOL_USE,
        POST_TOOL_USE,
        POST_TOOL_USE_FAILURE,
        STOP,
        SUBAGENT_START,
        SUBAGENT_STOP,
    }
)

#: 可阻断主流程的事件集合（方案 03 §3.2）。**只有** `PreToolUse` 在内 ——
#: 让 hook 只能收紧、不能放宽工具策略，否则它就成了绕过 `tool_policy`
#: 分级判定的新通道。
BLOCKING_EVENTS: frozenset[str] = frozenset({PRE_TOOL_USE})


def build_context(
    event: str,
    *,
    session_id: str = "",
    tenant_id: str = "",
    user_id: str = "",
    tool_name: str = "",
    tool_arguments: Any = None,
    tool_result: Any = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造传给 hook 脚本的事件上下文（保证可 JSON 序列化）。

    显式列字段而不是把整个 `task` 丢过去：这是 hook 能看到的**唯一**输入面，
    既能满足判定需要，又避免把不该外泄的内部结构透给用户自定义 hook。
    """
    context: dict[str, Any] = {
        "event": event,
        "session_id": session_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
    }
    if tool_name:
        context["tool_name"] = tool_name
    if tool_arguments is not None:
        context["tool_arguments"] = tool_arguments
    if tool_result is not None:
        context["tool_result"] = tool_result
        # 结果是否成功由 hook 自行判定；这里顺带给一个便捷布尔，省得它重复探查结构。
        context["ok"] = not (isinstance(tool_result, dict) and bool(tool_result.get("error")))
    if extra:
        context.update(extra)
    return context
