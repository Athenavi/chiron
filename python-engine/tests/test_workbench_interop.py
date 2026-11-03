"""工作台互通：本会话新增逻辑的单元测试。

覆盖其中**不依赖外部服务**的部分 —— F3.2 的工具集筛选与专家人格。
这些此前只有语法检查，是这批改动里最大的空洞。
"""

from __future__ import annotations

import pytest

from app.agent.runtime import _plugin_names_of, _restrict_tools_to_plugins, _tool_name_of
from app.tools.context import set_tool_context
from app.tools.registry import registry
from app.tools.subagent import _expert_system_prompt


def _noop(**kwargs):
    return {}


def _tool(name: str) -> dict:
    """OpenAI 形态的工具项（runtime 里拿到的就是这种）"""
    return {"type": "function", "function": {"name": name}}


@pytest.fixture
def cleanup_registry():
    """用例里注册的测试工具在结束后清理，避免污染全局 registry"""
    created: list[str] = []
    yield created
    for name in created:
        registry.unregister(name)


class TestPluginToolRestriction:
    """F4：按"带进对话的插件"限定 MCP 工具集（内置工具不受影响）"""

    def test_no_selection_keeps_everything(self):
        tools = [_tool("read_file"), _tool("fs_read")]
        assert _restrict_tools_to_plugins(tools, {}) == tools
        assert _restrict_tools_to_plugins(tools, None) == tools

    def test_builtin_survives_and_unselected_mcp_dropped(self, cleanup_registry):
        registry.register("read_file", "", {}, _noop)  # 默认 source=builtin
        registry.register("fs_read", "", {}, _noop, source="mcp")
        registry.register("gh_list", "", {}, _noop, source="mcp")
        cleanup_registry += ["read_file", "fs_read", "gh_list"]

        tools = [_tool("read_file"), _tool("fs_read"), _tool("gh_list")]
        kept = [
            _tool_name_of(t)
            for t in _restrict_tools_to_plugins(tools, {"plugin_names": ["fs"]})
        ]
        # 内置保留；只放选中插件（fs）的工具；gh_list 被筛掉
        assert kept == ["read_file", "fs_read"]

    def test_unregistered_tool_is_not_filtered(self):
        # registry 里查不到 → 不当成 MCP，不该因为"来源未知"被误删
        tools = [_tool("never_registered")]
        assert _restrict_tools_to_plugins(tools, {"plugin_names": ["fs"]}) == tools

    def test_judged_by_registry_source_not_name_prefix(self, cleanup_registry):
        # 名字以 fs_ 开头、但注册为 builtin → 不该被当成该插件的工具
        registry.register("fs_but_builtin", "", {}, _noop)
        registry.register("real_mcp", "", {}, _noop, source="mcp")
        cleanup_registry += ["fs_but_builtin", "real_mcp"]

        tools = [_tool("fs_but_builtin"), _tool("real_mcp")]
        kept = [
            _tool_name_of(t)
            for t in _restrict_tools_to_plugins(tools, {"plugin_names": ["fs"]})
        ]
        assert kept == ["fs_but_builtin"]

    def test_similar_plugin_names_do_not_cross_match(self, cleanup_registry):
        # 归属判定是 name.startswith(f"{plugin}_")，靠那个下划线排他：
        # "fsx_alpha" 不该被 "fs" 选中，"fs_read" 也不该被 "fsx" 选中。
        registry.register("fs_read", "", {}, _noop, source="mcp")
        registry.register("fsx_alpha", "", {}, _noop, source="mcp")
        cleanup_registry += ["fs_read", "fsx_alpha"]

        tools = [_tool("fs_read"), _tool("fsx_alpha")]
        assert [
            _tool_name_of(t)
            for t in _restrict_tools_to_plugins(tools, {"plugin_names": ["fs"]})
        ] == ["fs_read"]
        assert [
            _tool_name_of(t)
            for t in _restrict_tools_to_plugins(tools, {"plugin_names": ["fsx"]})
        ] == ["fsx_alpha"]
        # 两个都选 → 两个都留
        assert [
            _tool_name_of(t)
            for t in _restrict_tools_to_plugins(tools, {"plugin_names": ["fs", "fsx"]})
        ] == ["fs_read", "fsx_alpha"]

    def test_builtin_tools_survive_every_plugin_selection(self, cleanup_registry):
        # 插件是"额外能力"，不该把内置工具一起砍掉 —— 选中一个不存在的插件时，
        # MCP 工具全去，内置工具必须原样留下。
        registry.register("fs_read", "", {}, _noop, source="mcp")
        cleanup_registry += ["fs_read"]
        registry.register("read_file", "", {}, _noop)  # 内置

        tools = [_tool("read_file"), _tool("fs_read")]
        assert _restrict_tools_to_plugins(tools, {"plugin_names": ["nope"]}) == [_tool("read_file")]

    def test_plugin_names_parsing(self):
        assert _plugin_names_of({"plugin_names": ["a", " a ", "", 42]}) == ["a"]
        assert _plugin_names_of({"plugin_names": "nope"}) == []
        assert _plugin_names_of({"plugin_names": ["a", "a"]}) == ["a"]
        assert _plugin_names_of(None) == []

    def test_tool_name_both_shapes(self):
        assert _tool_name_of({"function": {"name": "x"}}) == "x"
        assert _tool_name_of({"name": "y"}) == "y"
        assert _tool_name_of({}) == ""


class TestExpertPersona:
    """F3.2：按名字取专家人格 —— 只认清单里的名字，拒绝自由文本"""

    def test_blank_name(self):
        assert _expert_system_prompt("") == ""
        assert _expert_system_prompt("   ") == ""

    def test_name_outside_roster_is_rejected(self):
        set_tool_context(experts=[{"name": "评审", "system_prompt": "你是评审"}])
        # 不在清单里 → 退回通用 child，而不是让模型用任意字符串塑造子代理人格
        assert _expert_system_prompt("攻击者") == ""

    def test_known_name_returns_persona(self):
        set_tool_context(experts=[{"name": "评审", "system_prompt": "你是评审"}])
        assert _expert_system_prompt("评审") == "你是评审"

    def test_no_experts_in_context(self):
        set_tool_context(experts=[])
        assert _expert_system_prompt("评审") == ""
