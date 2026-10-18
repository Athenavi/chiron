"""无人值守回合（自动续跑）的交互闸门回归 —— 不许白等，也不许静默。

续跑出来的 runtime 既不在 `_ACTIVE_RUNTIMES`（审批 / 回答端点靠它定位），事件也没有
订阅者。于是审批与 `ask_user` 一触发就是**每条 300s 的空等**，而 reconciler 是
`await runner(task)` 串行跑续跑的：一次等待把该实例的整轮巡检钉住，用户回来还只看到
一条没有任何上下文的中断。`AgentRuntime(unattended=True)` 把这两条通道改成立刻
fail-closed —— 与"用户点了拒绝"同一条收口，只是理由不同，而且理由必须可见。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agent import runtime as runtime_module
from app.agent.runtime import DECISION_REJECT, AgentRuntime
from app.agent.runtime_types import AgentTask
from app.agent.subagent_runner import SubAgentRunner


def _runtime(*, unattended: bool) -> AgentRuntime:
    return AgentRuntime(gateway=None, unattended=unattended)


def _task() -> AgentTask:
    return AgentTask(
        id="turn-1", tenant_id="t1", user_id="u1", session_id="s1", content="hi", max_turns=2
    )


def _call(cid: str = "tc-1") -> dict[str, Any]:
    return {"id": cid, "name": "shell_exec", "arguments": '{"command": "ls"}'}


async def _never(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("无人值守回合不该进入等待有人应答的实现")


@pytest.fixture
def audit_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """捕获审批审计调用（不碰磁盘），用来核对"拒绝 + 理由"确实登记了。"""
    records: list[dict[str, Any]] = []

    def _spy(tool_call_id: str, tool_name: str, decision: str, **kwargs: Any) -> None:
        records.append({
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "decision": decision,
            **kwargs,
        })

    monkeypatch.setattr(runtime_module, "_record_approval_audit", _spy)
    return records


class TestUnattendedApproval:
    async def test_denies_without_waiting_and_audits(
        self, monkeypatch: pytest.MonkeyPatch, audit_spy: list[dict[str, Any]]
    ):
        rt = _runtime(unattended=True)
        monkeypatch.setattr(rt, "_store_approval_ticket", AsyncMock())
        monkeypatch.setattr(rt, "_wait_approval_decision", _never)
        call = _call()

        await rt._begin_approval(call, _task(), level="write", targs={"command": "ls"})
        result = await rt._await_approval(call, _task())

        assert "unattended" in result["error"]
        # 留下的必须是干净的拒绝：pending future 不能悬着，否则下一轮还会看见它
        assert rt._pending_approvals == {}  # noqa: SLF001
        assert len(audit_spy) == 1
        assert audit_spy[0]["decision"] == DECISION_REJECT
        assert "no one to approve" in audit_spy[0]["reason"]

    async def test_tool_is_not_executed(self, monkeypatch: pytest.MonkeyPatch):
        """拒绝就是拒绝：无人值守不能退化成"没人应答就放行"。

        工具真正的执行点在 `_await_approval` 的**批准分支**里，所以只要它没有走进
        等待/批准，就没有任何副作用发生。
        """
        rt = _runtime(unattended=True)
        monkeypatch.setattr(rt, "_store_approval_ticket", AsyncMock())
        monkeypatch.setattr(rt, "_execute_tool", _never)
        monkeypatch.setattr(rt, "_wait_approval_decision", _never)
        call = _call()

        await rt._begin_approval(call, _task(), level="write", targs={"command": "ls"})
        result = await rt._await_approval(call, _task())

        assert "unattended" in result["error"]

    async def test_attended_turn_still_waits(self, monkeypatch: pytest.MonkeyPatch):
        """有订阅者的正常回合不受影响：仍然等两条通道，超时才拒。"""
        rt = _runtime(unattended=False)
        monkeypatch.setattr(rt, "_store_approval_ticket", AsyncMock())
        wait = AsyncMock(return_value=None)
        monkeypatch.setattr(rt, "_wait_approval_decision", wait)
        call = _call()

        await rt._begin_approval(call, _task(), level="write", targs={"command": "ls"})
        result = await rt._await_approval(call, _task())

        wait.assert_awaited_once()
        assert "timed out" in result["error"]


class TestUnattendedAskUser:
    async def test_returns_a_declined_answer_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """`declined` 是**答复**，`error` 是故障：模型据此知道该自行决定而不是重试。"""
        rt = _runtime(unattended=True)
        monkeypatch.setattr(rt, "_wait_answer_decision", _never)
        call = {"id": "tc-ask", "name": "ask_user", "arguments": '{"question": "选哪个？"}'}

        rt._ask_event(call)  # noqa: SLF001 — 登记待回答 future
        answer = await rt._await_answer(call, _task())

        assert answer["declined"] is True
        assert "unattended" in answer["reason"]
        assert rt._pending_answers == {}  # noqa: SLF001


class TestFlagPlumbing:
    def test_runtime_stores_the_flag(self):
        assert _runtime(unattended=True)._unattended is True  # noqa: SLF001
        assert _runtime(unattended=False)._unattended is False  # noqa: SLF001

    def test_subagent_runner_accepts_the_flag(self):
        runner = SubAgentRunner(gateway=None, unattended=True)
        assert runner._unattended is True  # noqa: SLF001

    async def test_resume_runner_builds_an_unattended_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """续跑执行体必须真的把这一轮标成无人值守（含工具上下文，供子 Agent 继承）。"""
        import app.agent.event_sink as event_sink
        import app.agent.runtime as agent_runtime
        import app.main as main_module
        import app.memory.service as memory_service
        import app.tools.context as tool_context

        sentinel_gateway = object()

        async def _gateway() -> Any:
            return sentinel_gateway

        captured: dict[str, Any] = {}

        class _RecordingRuntime:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

            async def run(self, task: Any):
                if False:  # pragma: no cover — 只需要它是个 async generator
                    yield None

        ctx_calls: list[dict[str, Any]] = []

        monkeypatch.setattr(main_module, "get_gateway", _gateway)
        monkeypatch.setattr(agent_runtime, "AgentRuntime", _RecordingRuntime)
        monkeypatch.setattr(memory_service, "get_service", lambda: object())
        monkeypatch.setattr(event_sink, "EventSink", lambda **kw: object())
        monkeypatch.setattr(
            tool_context, "set_tool_context", lambda **kw: ctx_calls.append(kw)
        )

        await main_module._auto_resume_runner(_task())

        assert captured["gateway"] is sentinel_gateway
        assert captured["unattended"] is True
        assert ctx_calls and ctx_calls[0]["unattended"] is True
