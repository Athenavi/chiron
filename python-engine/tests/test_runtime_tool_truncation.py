"""产品链路（`/v1/agent/submit` → `AgentRuntime`）的**截断 tool_call 安全网**。

背景：`finish_reason=length` 表示输出被 `max_tokens` 截断，此时 tool_call 的 `arguments`
是**半截 JSON**，解析出来是残缺参数 —— 拿它去执行可能写坏文件（`runtime.py` 的守卫注释即此意）。

为什么补这条：该性质**此前只有遗留循环 `loop.py` 的用例覆盖**（`tests/test_agent.py`），
而**产品链路零覆盖**。两条路径各有一份实现，删掉遗留那份之前，先把安全网钉在**在用的那条**上。
"""

from unittest.mock import MagicMock

import pytest

from app.agent.runtime import run_agent

TRUNCATED_ARGUMENTS = '{"path": "a.txt", "content": "hel'  # 半截 JSON（模拟 max_tokens 截断）
COMPLETE_ARGUMENTS = '{"path": "a.txt", "content": "hello"}'


def _chunk(*, finish_reason: str, arguments: str, content: str = "") -> MagicMock:
    return MagicMock(
        content=content,
        input_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        finish_reason=finish_reason,
        tool_calls=[
            MagicMock(id="call_1", name="write_file", arguments=arguments)
        ],
    )


async def _collect(gateway: MagicMock, max_turns: int = 1) -> list[dict]:
    events: list[dict] = []
    async for event in run_agent(
        gateway=gateway,
        system_prompt="test",
        history=[],
        content="write a file",
        tools=[{"name": "write_file", "parameters": {"type": "object"}}],
        llm_config={"model": "test"},
        max_turns=max_turns,
    ):
        events.append(event)
    return events


@pytest.mark.asyncio
async def test_truncated_tool_call_is_not_dispatched_on_product_path():
    gateway = MagicMock()

    async def truncated_stream(*_a, **_kw):
        yield _chunk(finish_reason="length", arguments=TRUNCATED_ARGUMENTS)

    gateway.chat_stream = truncated_stream
    events = await _collect(gateway, max_turns=1)

    types = [e["type"] for e in events]
    assert "tool_call" not in types, f"被 max_tokens 截断的 tool_call 不得下发执行，事件流={types}"

    results = [e for e in events if e["type"] == "tool_result"]
    assert results, f"必须回灌 tool_result 说明截断（否则模型无从纠正），事件流={types}"
    assert "truncat" in str(results[0].get("content", "")).lower(), results[0]


@pytest.mark.asyncio
async def test_complete_tool_call_is_dispatched_on_product_path():
    """正对照：未被截断时正常下发（防止"把守护写成一律不下发"这种过度修正）。"""
    gateway = MagicMock()
    calls = {"n": 0}

    async def two_round_stream(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            yield _chunk(finish_reason="tool_calls", arguments=COMPLETE_ARGUMENTS)
            return
        yield MagicMock(
            content="done",
            input_tokens=0,
            output_tokens=0,
            cached_tokens=0,
            finish_reason="stop",
            tool_calls=None,
        )

    gateway.chat_stream = two_round_stream
    events = await _collect(gateway, max_turns=3)

    types = [e["type"] for e in events]
    assert "tool_call" in types, f"完整 tool_call 应正常下发，事件流={types}"
