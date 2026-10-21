"""M24：cost 轴的**端到端**接线 —— 单价由网关下发，越界时以 `budget_exceeded:cost` 收尾。

为什么单独一条：`tests/test_cost_axis.py` 只钉纯函数（`exceeded` / 快照），而"runtime 真的
把 `_cost_spent()` 传给了 `exceeded()`"这件事**只有跑一轮才看得见** —— 漏传时纯函数全绿，
线上表现却是"cost 轴永远不拦"（正是 M24 要修的那个缺口）。

两轮是必须的：`exceeded()` 在**轮首**判定，所以第一次调用产生的用量要在**下一轮开头**才被看见
（只跑一轮 stop 的会话走不到那个检查点）。
"""
from __future__ import annotations

from typing import Any

import pytest

from app.gateway.provider import ChatResponse, ToolCall


class _TwoTurnGateway:
    """第一次回一个（不存在的）工具调用逼出第二轮，第二次收尾 —— 让轮首的预算判定跑到。"""

    async def chat_stream(self, **_kwargs: Any) -> Any:
        yield ChatResponse(
            content="",
            finish_reason="tool_calls",
            tool_calls=[ToolCall(id="c1", name="__absent_tool__", arguments="{}")],
            input_tokens=2000,
            output_tokens=0,
        )
        yield ChatResponse(content="收尾", finish_reason="stop", input_tokens=2000, output_tokens=0)


async def _run(monkeypatch: pytest.MonkeyPatch, *, price: float, cap: int) -> list[Any]:
    from app.agent.runtime import AgentRuntime, AgentTask

    monkeypatch.setenv("AGENT_RUN_MAX_TOKENS", "0")
    monkeypatch.setenv("AGENT_MAX_STEPS", "0")
    monkeypatch.setenv("AGENT_RUN_MAX_TURNS", "0")
    monkeypatch.setenv("AGENT_MAX_SECONDS", "0")
    monkeypatch.setenv("AGENT_MAX_COST", str(cap))

    runtime = AgentRuntime(gateway=_TwoTurnGateway())

    async def _load(_task: Any) -> None:
        return None

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(runtime, "_load_resume_state", _load)
    monkeypatch.setattr(runtime, "_save_checkpoint", _noop)
    monkeypatch.setattr(runtime, "_mark_run_status", _noop)

    task = AgentTask(
        id="run_m24_cost",
        tenant_id="t7",
        user_id="u",
        session_id="s1",
        content="hi",
        max_turns=4,
        # 网关下发的单价（credits / 1000 tokens）——引擎不持有自己的价表。
        llm_config={"cost_per_1k_input": price, "cost_per_1k_output": 0.0},
    )
    return [event async for event in runtime.run(task)]


def _errors(events: list[Any]) -> list[str]:
    return [str(e.error or "") for e in events if e.type == "error"]


async def test_cost_axis_trips_end_to_end_on_the_downstream_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 2000 input tokens × 10 credits/1k = 20 credits > 上限 1 ⇒ 必须在下一轮轮首被拦。
    events = await _run(monkeypatch, price=10.0, cap=1)

    assert "budget_exceeded:cost" in _errors(events), (
        "cost 轴必须真的拦 —— 漏把 _cost_spent() 传给 exceeded() 时这条会红"
    )


async def test_cost_axis_stays_silent_without_a_price(monkeypatch: pytest.MonkeyPatch) -> None:
    """网关没下发单价（旧网关 / 直连引擎）⇒ 不编造数字，行为与 M24 之前一致。"""
    events = await _run(monkeypatch, price=0.0, cap=1)

    assert "budget_exceeded:cost" not in _errors(events)
