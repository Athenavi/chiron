"""SubAgent 工具真实执行测试：工具调用走注册表沙箱、结果回传 LLM、上下文隔离。"""
from __future__ import annotations

import json

import pytest

from app.agent.multi_agent import SubAgent
from app.gateway.provider import ChatResponse, ToolCall
from app.tools.context import get_tenant_id, get_user_id
from app.tools.registry import registry


async def _echo_tool(text: str = "") -> dict:
    """测试工具：返回真实结果 + 当前沙箱上下文。"""
    return {
        "stdout": f"echo:{text}",
        "sandbox_user": get_user_id(),
        "sandbox_tenant": get_tenant_id(),
    }


# 注册测试工具（进程级唯一）
registry.register(
    name="subagent_echo",
    description="test echo tool",
    parameters={"type": "object", "properties": {}},
    handler=_echo_tool,
)


class _ToolGateway:
    """第一轮返回工具调用，第二轮返回最终文本；记录收到的 tool 消息。"""

    def __init__(self, tool_name: str = "subagent_echo", args: str = '{"text": "hello-tool"}') -> None:
        self.tool_name = tool_name
        self.args = args
        self.calls = 0
        self.tool_msgs: list = []

    async def chat_stream(self, messages=None, **_kw):
        self.calls += 1
        self.tool_msgs = [m for m in (messages or []) if getattr(m, "role", "") == "tool"]
        if self.calls == 1:
            yield ChatResponse(
                content="",
                finish_reason="tool_calls",
                tool_calls=[ToolCall(id="call_1", name=self.tool_name, arguments=self.args)],
            )
        else:
            yield ChatResponse(content="final answer", finish_reason="stop")


@pytest.mark.asyncio
async def test_subagent_executes_tool_and_returns_result():
    gw = _ToolGateway()
    agent = SubAgent(name="t", description="", system_prompt="sys", gateway=gw, max_turns=5)
    result = await agent.run(task="do it", tenant_id="tenant-1")

    assert result.success
    assert result.output == "final answer"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "subagent_echo"

    # 工具真实执行，结果（含输出）作为 tool 消息回传 LLM
    assert gw.tool_msgs, "tool result should be fed back to the LLM"
    content = json.loads(gw.tool_msgs[-1].content)
    assert content.get("stdout") == "echo:hello-tool"
    assert content.get("tool") == "subagent_echo"


@pytest.mark.asyncio
async def test_subagent_tool_sandbox_context_isolated():
    gw = _ToolGateway()
    agent = SubAgent(name="t", description="", system_prompt="sys", gateway=gw, max_turns=5)
    result = await agent.run(task="do it", context={"user_id": "user-42"}, tenant_id="42")

    assert result.success
    content = json.loads(gw.tool_msgs[-1].content)
    # 沙箱上下文按租户/用户隔离（而非全部落到 default/anonymous）
    assert content.get("sandbox_user") == "user-42"
    assert content.get("sandbox_tenant") == "42"


@pytest.mark.asyncio
async def test_subagent_missing_tool_returns_error():
    gw = _ToolGateway(tool_name="no_such_tool_registered", args="{}")
    agent = SubAgent(name="t", description="", system_prompt="sys", gateway=gw, max_turns=5)
    result = await agent.run(task="do it", tenant_id="t1")

    assert result.success  # 工具错误不中断 agent 循环
    content = json.loads(gw.tool_msgs[-1].content)
    assert "not found" in content.get("error", "")


@pytest.mark.asyncio
async def test_subagent_no_tool_call_completes():
    class _PlainGateway:
        async def chat_stream(self, messages=None, **_kw):
            yield ChatResponse(content="plain reply", finish_reason="stop")

    agent = SubAgent(name="t", description="", system_prompt="sys", gateway=_PlainGateway(), max_turns=5)
    result = await agent.run(task="hi")
    assert result.success
    assert result.output == "plain reply"
    assert result.tool_calls == []
