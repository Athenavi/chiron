"""C3：按次用量事件（`usage`）的契约 —— 它是**本次调用**的增量，`done` 仍是**整轮累计**。

为什么这条需要单独钉住：网关侧的累计器直接喂计费（`DeductTokens`）。若引擎把增量写成累计，
或消费方把两条通道混着加，表现就是**用户被多扣费**、缓存命中率被算错（Go 侧同口径的守卫见
`internal/api/usage_accounting_test.go`）。
"""

from unittest.mock import MagicMock

import pytest

from app.agent.runtime import run_agent


@pytest.mark.asyncio
async def test_per_call_usage_is_delta_while_done_stays_cumulative():
    mock_gateway = MagicMock()
    calls = {"n": 0}

    async def two_call_stream(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # 第 1 次调用：要求写文件（触发下一轮 ⇒ 运行时会有第二次 LLM 调用）
            yield MagicMock(
                content="",
                input_tokens=100,
                output_tokens=20,
                cached_tokens=7,
                finish_reason="tool_calls",
                tool_calls=[
                    MagicMock(
                        id="call_1",
                        name="write_file",
                        arguments='{"path": "a.txt", "content": "x"}',
                    )
                ],
            )
            return
        yield MagicMock(
            content="done",
            input_tokens=30,
            output_tokens=10,
            cached_tokens=3,
            finish_reason="stop",
            tool_calls=None,
        )

    mock_gateway.chat_stream = two_call_stream

    events = []
    async for event in run_agent(
        gateway=mock_gateway,
        system_prompt="test",
        history=[],
        content="write a file",
        tools=[{"name": "write_file", "parameters": {"type": "object"}}],
        llm_config={"model": "test"},
        max_turns=3,
    ):
        events.append(event)

    usage = [e for e in events if e["type"] == "usage"]
    assert len(usage) == 2, f"每次 LLM 调用应有一条 usage 事件，实到 {len(usage)}：{[e['type'] for e in events]}"
    assert (usage[0]["input_tokens"], usage[0]["output_tokens"]) == (100, 20)
    assert (usage[1]["input_tokens"], usage[1]["output_tokens"]) == (30, 10), (
        "usage 必须是**本次调用**的增量；若是累计值，消费方会与 done 重复计费"
    )
    assert (usage[0]["cached_tokens"], usage[1]["cached_tokens"]) == (7, 3)

    done = [e for e in events if e["type"] == "done"][-1]
    assert (done["input_tokens"], done["output_tokens"]) == (130, 30), (
        "done 仍必须是**整轮累计**（两条通道各自成立）"
    )
