"""工具分级表只能引用「真实注册的工具」或**点名的**历史兜底名。

为什么需要这条断言
------------------
`app/agent/tool_policy.py` / `internal/api/tool_policy.go` 用固定名单表达策略。固定名单
必然漂移：`40dfc99` 删除 `app/tools/agent.py`（`agent_dispatch` / `agent_list` / `code_agent` /
`agent_session_create` / `agent_session_list`）后，两张表仍然把它们当"委派类"写了很久。

这类错误不会报错 —— 它只会让读代码的人以为"这个工具存在"。所以这里用一条机械断言把它钉住：
**表里的每个名字，要么在当前注册表里，要么在下面的 `LEGACY_TOOL_NAMES` 里被逐个点名说明。**
新增一个说不清来历的名字 → 这条用例失败（见 vendor/规划.md §1.3「机械化的断言要精确到它想拦的东西」）。

跨语言的一致性（Go ↔ Python 两张表同构）由 `scripts/check_tool_policy_parity.py` 负责。
"""
from __future__ import annotations

import app.tools  # noqa: F401 — 工具注册的唯一入口（见 app/tools/__init__.py）
from app.agent import tool_policy
from app.tools.registry import registry

#: 保留在分级表里、但**当前注册表中不存在**的历史名。它们不是漏删的死代码，而是**保守兜底**：
#: 旧客户端 / 兼容层按这些名字调用时，仍按原级别处置，而不是 fail-closed 到 write。
#: 每个名字都必须写清"为什么还留着"；一旦该工具真的被实现，请把它从这里删掉。
LEGACY_TOOL_NAMES: dict[str, str] = {
    "knowledge": "旧版知识库工具名，已被 kb_list/kb_search/rag_query 取代；read 兜底",
    "stderr_drain": "旧版后台任务日志工具名；read 兜底（勿降级为 write）",
    "tool": "早期通用工具代理名（对应已删除的 app/tools/agent.py 的 AgentInfo）；read 兜底",
    "delete_file": "旧版删除工具名；**delete 兜底**（真出现时绝不能降级成 write）",
    "kb_delete": "旧版知识库删除工具名；**delete 兜底**",
    "execute_command": "旧版命令执行工具名（现为 shell_exec/persistent_shell）；命令类兜底",
}


def _policy_names() -> set[str]:
    return set(tool_policy._TOOL_LEVELS) | set(tool_policy.COMMAND_TOOLS)


def test_policy_table_only_references_known_tools():
    registered = set(registry.list_names())
    explainable = registered | set(LEGACY_TOOL_NAMES)
    unknown = sorted(_policy_names() - explainable)
    assert not unknown, (
        "分级表引用了既不注册、也未在 LEGACY_TOOL_NAMES 点名的名字："
        f"{unknown}。要么删掉，要么在 LEGACY_TOOL_NAMES 里写清为什么保留。"
    )


def test_legacy_names_are_not_actually_registered():
    """兜底名一旦被实现，就必须从 LEGACY_TOOL_NAMES 移出 —— 否则名单会悄悄变成谎言。"""
    registered = set(registry.list_names())
    resurrected = sorted(set(LEGACY_TOOL_NAMES) & registered)
    assert not resurrected, (
        f"{resurrected} 已经注册了，请从 LEGACY_TOOL_NAMES 移除并让分级表按真实工具维护。"
    )


def test_policy_table_is_not_empty_and_levels_are_valid():
    assert tool_policy._TOOL_LEVELS, "分级表为空 —— 解析或维护出了问题"
    assert set(tool_policy._TOOL_LEVELS.values()) <= tool_policy.VALID_LEVELS
    names = sorted(_policy_names())
    assert "read_file" in names and "write_file" in names and "shell_exec" in names
