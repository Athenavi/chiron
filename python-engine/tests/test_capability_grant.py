"""A4（方案 04 批次 1）：工具面的「天花板 ∩ 收窄」。

两条授权来源，方向不同、都不能放宽对方：

* **天花板** `ProfileSpec.allowed_tools` —— "这个 worker **最多**能给哪些"；
* **收窄** `SubAgentRunner(call_tools=…)` —— "**这一次**只要哪些"。

有效工具面 = **交集**。硬约束是：**调用侧只能收窄，永不放宽**。本文件把这条钉住，
外加"既有只读默认不被削弱"的回归（改授权模型最危险的方向是放宽）。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.agent.profile import ProfileSpec
from app.agent.subagent_runner import SubAgentRunner


def _names(spec: ProfileSpec | None, *, call_tools: list[str] | None = None, allow_write: bool = False):
    """跑一次 `_resolve_tools`，返回最终工具名集合。"""
    runner = SubAgentRunner(MagicMock(), call_tools=call_tools, allow_write=allow_write)
    tools = runner._resolve_tools(spec, "normal", child_depth=1, max_depth=3)  # noqa: SLF001
    return {t["name"] for t in (tools or [])} if tools is not None else None


def test_no_profile_no_narrowing_keeps_read_only_default():
    """回归：不传 profile、不传收窄时，子 Agent 的默认工具面仍是**只读**。"""
    names = _names(None)

    assert names is not None
    assert "write_file" not in names
    assert "bash" not in names


def test_call_tools_narrows_within_profile_ceiling():
    spec = ProfileSpec(name="p", allowed_tools=["read_file", "list_files", "search"])
    runner = SubAgentRunner(MagicMock(), call_tools=["read_file"])
    tools = runner._resolve_tools(spec, "normal", child_depth=1, max_depth=3)  # noqa: SLF001

    assert {t["name"] for t in tools} == {"read_file"}


def test_call_tools_cannot_widen_beyond_profile_ceiling():
    """**核心不变量**：调用侧列了 Profile 之外的名字，不会因此生效。"""
    spec = ProfileSpec(name="p", allowed_tools=["read_file"])
    runner = SubAgentRunner(MagicMock(), call_tools=["read_file", "write_file", "bash"])
    tools = runner._resolve_tools(spec, "normal", child_depth=1, max_depth=3)  # noqa: SLF001

    names = {t["name"] for t in tools}
    assert names == {"read_file"}
    assert "write_file" not in names and "bash" not in names


def test_empty_call_tools_means_no_tools():
    """空列表（而非 `None`）是**强收窄**：这次不给任何工具，子 Agent 只能凭已有上下文作答。"""
    spec = ProfileSpec(name="p", allowed_tools=["read_file"])
    runner = SubAgentRunner(MagicMock(), call_tools=[])

    assert runner._resolve_tools(spec, "normal", child_depth=1, max_depth=3) == []  # noqa: SLF001


def test_call_tools_does_not_override_read_only():
    """收窄不能把只读默认解掉 —— 两个约束是叠加的，不是二选一。"""
    spec = ProfileSpec(name="p", allowed_tools=["read_file", "write_file"], read_only=True)
    runner = SubAgentRunner(MagicMock(), call_tools=["read_file", "write_file"])
    names = {t["name"] for t in runner._resolve_tools(spec, "normal", 1, 3)}  # noqa: SLF001

    assert "write_file" not in names
    assert "read_file" in names


@pytest.mark.parametrize("value", [None, []])
def test_narrowing_semantics_are_distinct(value: list[str] | None):
    """`None`（不收窄）与 `[]`（不收窄到零）必须可区分 —— 否则"不给工具"无法表达。"""
    runner = SubAgentRunner(MagicMock(), call_tools=value)

    if value is None:
        assert runner._resolve_tools(None, "normal", 1, 3) is not None  # noqa: SLF001
    else:
        assert runner._resolve_tools(None, "normal", 1, 3) == []  # noqa: SLF001
