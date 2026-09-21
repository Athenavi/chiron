"""把「当前会话标识」暴露给 provider 层（contextvar）。

**为什么需要它**：OpenCode（Zen / Go）要求请求携带 `x-opencode-session`，否则返回

    400 MissingSessionID: Request is missing x-opencode-session and cannot be routed
    efficiently. Please see https://opencode.ai/docs/go/#where-can-i-use-it

这个头只影响对方的路由与 prompt 缓存（不影响可用性，但缺失就直接 400），
所以用**稳定的会话 ID** 最合适：同一对话的请求走同一路由，prompt 缓存命中更好。

用 contextvar 而不是改调用链签名：provider 是长生命周期对象、每次请求复用，
而会话 ID 是**每次请求**的属性 —— 与 app/tools/context.py 的 set_tool_context 同一模式。
"""

from __future__ import annotations

from contextvars import ContextVar

#: 当前请求所属的会话标识（空串 = 未知，此时不注入该头，行为与不带头的旧版本一致）
_llm_session_id: ContextVar[str] = ContextVar("chiron_llm_session_id", default="")


def set_llm_session_id(value: str) -> None:
    """由 AgentRuntime 在开始一次运行前设置（task.session_id 优先，缺省用 task.id）。"""
    _llm_session_id.set(str(value or ""))


def get_llm_session_id() -> str:
    return _llm_session_id.get() or ""
