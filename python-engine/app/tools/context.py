"""工具执行上下文 — contextvars 传播当前运行的 agent 上下文。

deepseek-harness 的工具经 exec context（ToolExecution）携带调用方信息；
chiron 的 registry.execute 只有 (name, params)，工具无法感知 session/网关。
此处用 contextvars 在 AgentRuntime.run() 内设置，工具通过 get_* 读取，
在 async 环境中自动沿任务传播（无需改 registry 签名）。
"""

from __future__ import annotations

import contextvars
from typing import Any

# default 用 None 哨兵而不是 {}：ContextVar 的默认值是**所有未设置该变量的上下文
# 共享的同一个对象**，可变默认会让某处就地修改泄漏到其它请求/任务（B039）。
_current_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "chiron_tool_context", default=None
)


def set_tool_context(**kwargs: Any) -> None:
    """在 agent 循环内设置当前上下文（session_id/user_id/tenant_id/gateway）。"""
    merged = dict(_current_context.get() or {})
    merged.update(kwargs)
    _current_context.set(merged)


def get_tool_context(key: str, default: Any = None) -> Any:
    return (_current_context.get() or {}).get(key, default)


def get_session_id() -> str:
    return str(get_tool_context("session_id", ""))


def get_user_id() -> str:
    return str(get_tool_context("user_id", ""))


def get_tenant_id() -> str:
    """当前租户 ID 的**字符串形式**（未绑定 / 无法解析时返回 ``""``）。

    为什么默认是 str：绝大多数调用点是**格式化**场景 —— 沙箱路径拼接
    （``sandbox_root() / tenant``）、审计条目的 ``tenant`` 字段、
    ``kb_access_allowed`` 的租户比较、``SkillStore`` 的目录段。
    需要整数（SQL 参数 / Pydantic 请求体）的地方用 ``get_tenant_id_int()``。

    与 Go 的契约：网关与 JWT 里的 tenant_id 已是整数，py 侧只在**边界**把它
    规约到整数域（见 ``to_tenant_int``），对外仍以字符串形式供格式化使用。
    """
    tid = get_tenant_id_int()
    return str(tid) if tid else ""


def get_tenant_id_int() -> int:
    """当前租户 ID 的**整数形式**（SQL 参数 / Pydantic 请求体用）；未绑定为 0。"""
    return to_tenant_int(get_tool_context("tenant_id", 0))


def to_tenant_int(value: Any) -> int:
    """把租户 ID 宽容解析为 int16 域内的整数；无法解析时返回 0。

    用于 SQL 参数：`tenant_id` 列已改为 smallint，asyncpg 传 str 会直接报错。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    if n < 0 or n > 32767:
        return 0
    return n


def get_project() -> str:
    """当前工作台选中的**项目**（C3）。

    记忆（L2 条目）按 `tenant + user + project` 隔离 —— **空串 = 未分组**（缺省）。
    由 runtime 在 run 开始时从 `task.workbench_context["project"]` 写入，工具侧只读。
    """
    return str(get_tool_context("project", ""))


def get_gateway() -> Any:
    """当前运行的 GatewayRouter 引用（子 agent 委派需要）。"""
    return get_tool_context("gateway", None)


def get_activated_tools() -> set[str]:
    """本会话**按需激活**的工具名集合（Token Economy 的实现点）。

    为什么返回一个**可变的 set 引用**，而不是像其它 get_* 那样返回值：

        contextvar 的值是**不可变传播**的 —— 工具内部再 `set_tool_context(...)`
        只会改当前任务的副本，**runtime 那一侧看不到**。而"按需激活"要求
        `tool_search` 工具内的一次 `add`，能被 runtime 的 `_get_core_tools` 观察到。

    所以这里把同一个 set 放进 context，工具与 runtime 共享这一个引用：
    工具 `add` → runtime 下一轮过滤时立刻可见。懒创建，缺失时自动补空集合。
    """
    ctx = _current_context.get() or {}
    acts = ctx.get("activated_tools")
    if not isinstance(acts, set):
        acts = set()
        merged = dict(ctx)
        merged["activated_tools"] = acts
        _current_context.set(merged)
    return acts


def activate_tools(names: list[str]) -> set[str]:
    """把工具加入本会话的已激活集合，返回激活后的完整集合（便于工具回报状态）。"""
    acts = get_activated_tools()
    acts.update(n for n in names if n)
    return acts


def get_all() -> dict[str, Any]:
    """完整快照当前上下文（子 agent 委派前保存、完成后恢复）。"""
    return dict(_current_context.get() or {})


def restore_context(snapshot: dict[str, Any]) -> None:
    """恢复上下文快照（子 agent 执行会改写 context，父任务需要还原）。"""
    _current_context.set(snapshot)
