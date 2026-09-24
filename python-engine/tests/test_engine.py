"""Tests for the enhanced Agent Engine.

Covers（保留部分）：
  * TestDangerousTools — 危险工具名单口径
  * TestAgentTask     — AgentTask 参数解析
  * TestConvertTools  — 工具定义转换（含 parameters JSON 兜底）

历史说明：本文件曾在重构后引用 `AgentSession`、`ToolApprovalRequest`、
`ToolApprovalResponse` —— 这三个符号**已随旧版引擎一并移除**（全仓无定义），
导致整份文件在收集阶段 ImportError，长期被 conftest 的 collect_ignore 隔离。
其中：

* `TestSessionSaveLoad`（会话持久化）与 `TestToolApprovalRequest`（审批数据结构）
  测的是**已被删除的 API**，无法修复，故整体移除；
* `TestToolApprovalFlow` 同样依赖 `ToolApprovalRequest/Response`，一并移除 ——
  审批流本身**仍有覆盖**：见 tests/test_tool_policy.py 与
  tests/test_subagent_approval_forward.py（均已按当前 tool_policy 分级实现编写）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.engine import (
    AgentEngine,
    AgentTask,
)

# DANGEROUS_TOOLS 已从 engine.py 迁到 guards.py（engine 只是向后兼容的 re-export 层，
# 且不再转出它）；审批数据结构 ToolApprovalRequest/Response 与 AgentSession 已随
# 旧版引擎重构移除，相应测试类见下方说明。
from app.agent.guards import DANGEROUS_TOOLS
from app.gateway.provider import ChatResponse

# ═══════════════════════════════════════════════════════════════════════════
# Helpers / Fixtures
# ═══════════════════════════════════════════════════════════════════════════

def _make_gateway(chat_stream_chunks: list[list[ChatResponse]] | None = None):
    """Create a mock gateway that yields predefined responses per turn.

    Parameters
    ----------
    chat_stream_chunks:
        A list of lists.  Each inner list is a sequence of ChatResponse
        objects yielded for one call to ``chat_stream``.  After all chunks
        are exhausted the gateway yields a single stop response.
    """
    gw = MagicMock()
    if chat_stream_chunks is None:
        chat_stream_chunks = [[
            ChatResponse(content="Hello!", finish_reason="stop"),
        ]]

    call_index = {"i": 0}

    async def _chat_stream(*_args, **_kwargs):
        idx = call_index["i"]
        call_index["i"] += 1
        chunks = chat_stream_chunks[idx] if idx < len(chat_stream_chunks) else [
            ChatResponse(content="", finish_reason="stop"),
        ]
        for chunk in chunks:
            yield chunk

    gw.chat_stream = _chat_stream
    return gw


def _make_tool_registry(tools: dict | None = None):
    """Create a mock tool registry.

    Parameters
    ----------
    tools:
        ``{name: handler}`` where *handler* is an async callable.
    """
    registry = MagicMock()
    tools = tools or {}
    registry.get.side_effect = lambda name: (MagicMock() if name in tools else None)

    async def _execute(name, params):
        handler = tools.get(name)
        if handler:
            return await handler(**params)
        return {"error": f"Tool '{name}' not found"}

    registry.execute = AsyncMock(side_effect=_execute)
    return registry


# ═══════════════════════════════════════════════════════════════════════════
# 1. Basic run
# ═══════════════════════════════════════════════════════════════════════════

class TestDangerousTools:
    """Verify the set of dangerous tools matches expectations."""

    def test_write_file_not_in_dangerous_tools(self):
        """write_file 属于 WRITE_TOOLS（ask 模式下才需确认），不在 DANGEROUS_TOOLS。

        当前判定已统一到 app/agent/tool_policy.py 的动作分级；
        guards.DANGEROUS_TOOLS 仅为兼容既有引用而保留（见其注释）。
        """
        assert "write_file" not in DANGEROUS_TOOLS
        from app.agent.guards import WRITE_TOOLS
        assert "write_file" in WRITE_TOOLS

    def test_shell_exec_is_dangerous(self):
        assert "shell_exec" in DANGEROUS_TOOLS

    def test_read_file_is_safe(self):
        assert "read_file" not in DANGEROUS_TOOLS

    def test_grep_files_is_safe(self):
        assert "grep_files" not in DANGEROUS_TOOLS


class TestAgentTask:
    """Verify AgentTask parsing."""

    def test_parse_basic(self):
        data = {
            "task_id": "t1",
            "tenant_id": "ten",
            "user_id": "u1",
            "content": "Hello",
        }
        task = AgentTask.parse(data)
        assert task.id == "t1"
        assert task.tenant_id == "ten"
        assert task.user_id == "u1"
        assert task.content == "Hello"

    def test_parse_defaults(self):
        task = AgentTask.parse({})
        assert task.id == ""
        assert task.max_turns == 10


class TestConvertTools:
    """Verify tool definition conversion."""

    def test_basic_conversion(self):
        engine = AgentEngine(gateway=MagicMock())
        tools = [
            {"name": "read_file", "description": "Read", "parameters": {"type": "object"}},
        ]
        result = engine._convert_tools(tools)
        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "read_file"

    def test_parameters_json_fallback(self):
        engine = AgentEngine(gateway=MagicMock())
        tools = [
            {"name": "tool1", "description": "T", "parameters_json": '{"type": "object"}'},
        ]
        result = engine._convert_tools(tools)
        assert result[0]["function"]["parameters"] == {"type": "object"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
