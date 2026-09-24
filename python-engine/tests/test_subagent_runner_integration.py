"""P1 集成测试：SubAgentRunner 的完整事件链路（不依赖真实 LLM）。

用 monkeypatch 替换 ``AgentRuntime.run``，断言三件事：
1. 子 Agent 的进度事件确实携带层级字段（run_id/parent_run_id/depth/profile）进入旁路；
2. ``[thinking]`` 包装的思考分流到 reasoning 频道，**不进入 L2 输出**；
3. 收尾唯一终态（subagent.done）带 usage，且发生在预览之后。
"""
from __future__ import annotations

import pytest

from app.agent.event_sink import EV_DONE, EV_REASONING, EV_STARTED, EV_STATUS, EV_TEXT, EventSink
from app.agent.subagent_runner import SubAgentRunner


def _fake_run_factory(events):
    async def _fake_run(self, task):  # noqa: ANN001 - 模拟 AgentRuntime.run 的签名
        for event in events:
            yield event

    return _fake_run


@pytest.mark.asyncio
async def test_runner_emits_hierarchy_events_and_separates_reasoning(monkeypatch):
    from app.agent import runtime as runtime_mod

    events = [
        runtime_mod.AgentEvent(type="text", content="[thinking]先读文件[/thinking]"),
        runtime_mod.AgentEvent(type="text", content="结论：改动没问题"),
        runtime_mod.AgentEvent(type="tool_call", tool_name="read_file", tool_call_id="c1"),
        runtime_mod.AgentEvent(type="text", content="", input_tokens=30, output_tokens=12),
    ]
    monkeypatch.setattr(runtime_mod.AgentRuntime, "run", _fake_run_factory(events))

    sink = EventSink(merge_window=0.0)
    runner = SubAgentRunner(
        gateway=object(),          # 摘要会失败并回落提取式（本测试不关心摘要质量）
        sink=sink,
        parent_session_id="s1",
        parent_run_id="rs_parent",
        tenant_id="t1",
        user_id="u1",
    )
    result = await runner.run("看看这个改动", profile_ref="", mode="normal", max_turns=3)

    # 1) L2 输出：只含正文 + 不可信包装；思考被剥离
    assert "结论：改动没问题" in result.output
    assert "先读文件" not in result.output
    assert result.output.startswith("<subagent-result")
    assert result.status == "completed"

    # 2) 旁路事件：层级字段齐全 + 顺序正确
    drained = sink.drain()
    types = [e.type for e in drained]
    assert types[0] == EV_STARTED
    assert types[-1] == EV_DONE
    assert EV_REASONING in types and EV_TEXT in types and EV_STATUS in types
    for event in drained:
        assert event.run_id == result.run_id
        assert event.depth == 1
        assert event.parent_run_id == "rs_parent"

    reasoning = next(e for e in drained if e.type == EV_REASONING)
    assert reasoning.content == "先读文件"

    text = next(e for e in drained if e.type == EV_TEXT)
    assert text.content == "结论：改动没问题"

    done = drained[-1]
    assert done.usage.get("output_tokens") == 12


@pytest.mark.asyncio
async def test_runner_depth_guard_blocks_further_delegation(monkeypatch):
    """max_depth=1 且当前深度已 1 → 直接失败（不再起子 Agent）。"""
    runner = SubAgentRunner(gateway=object(), depth=1, parent_session_id="s1")
    result = await runner.run("再委派一层")
    assert result.status == "failed"
    assert "depth" in result.error


@pytest.mark.asyncio
async def test_runner_reports_failure_without_output(monkeypatch):
    from app.agent import runtime as runtime_mod

    events = [runtime_mod.AgentEvent(type="error", error="provider 500")]
    monkeypatch.setattr(runtime_mod.AgentRuntime, "run", _fake_run_factory(events))

    sink = EventSink(merge_window=0.0)
    runner = SubAgentRunner(gateway=object(), sink=sink, parent_session_id="s1")
    result = await runner.run("会失败的任务")

    assert result.status == "failed"
    assert "provider 500" in result.error
    # 失败也要有唯一终态，且 notice 告知原因
    types = [e.type for e in sink.drain()]
    assert types[-1] == EV_DONE
    assert "subagent.notice" in types
