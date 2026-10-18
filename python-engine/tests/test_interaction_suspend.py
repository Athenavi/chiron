"""M7b 回归：一步"等人"如何结束回合，以及答复从哪两条通道回来。

三条不变量（对应 §5.1 人工介入闭环）：

1. **没人答 ⇒ 挂起，不是失败**：落库现场在 ⇒ 以 `awaiting_input` 结束本轮（不假装被拒绝、
   不写拒绝审计、不触发会话级收尾）；落库现场**不在**（PG 不可用）⇒ 退回 M6 的即时拒绝
   （`149f6a7`），因为"没有现场的等待等于永久等待"。
2. **答复只生效一次、且认参数**：票按 (会话, 类型, 工具名, 参数哈希) 取；取不到就重新问；
   载荷读不出 ⇒ fail-closed，不执行。
3. **停在等待上的调用没被执行过**：给它一条 `not_executed` 结果（保持 tool_calls 与 tool
   消息配对），但不进 `done_tools`、不计步数、不记副作用。

`pending_interactions` 的真 SQL 在 `test_pending_interactions_db.py`（integration）；这里用
替身模块验证 runtime 与它的**调用契约**。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent import interactions
from app.agent import runtime as runtime_module
from app.agent.runtime import (
    ASK_DECLINED,
    ASK_USER_TOOL,
    DECISION_APPROVE,
    DECISION_REJECT,
    AgentRuntime,
    ApprovalDecision,
)
from app.agent.runtime_types import AgentTask
from app.agent.tool_policy import args_hash


class _FakeInteractions:
    """`app.agent.interactions` 的替身：可控地"有现场 / 没有现场 / 有票"。"""

    def __init__(self) -> None:
        #: `create` 的返回：非空 = 落库成功；空串 = 没有持久通道（PG 不可用）
        self.create_id = "ix-1"
        self.created: list[dict[str, Any]] = []
        self.consumed: list[dict[str, Any]] = []
        self.settled: list[dict[str, Any]] = []
        #: 可取的授权票：[{kind, tool_name, args_hash, session_id, decision, reason}]
        self.tickets: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> str:
        self.created.append(kwargs)
        return self.create_id

    async def consume_grant(self, **kwargs: Any) -> dict[str, Any] | None:
        self.consumed.append(kwargs)
        for index, ticket in enumerate(self.tickets):
            if (
                ticket["session_id"] == kwargs["session_id"]
                and ticket["kind"] == kwargs["kind"]
                and ticket["tool_name"] == kwargs["tool_name"]
                and ticket["args_hash"] == kwargs["args_hash"]
            ):
                return dict(self.tickets.pop(index))
        return None

    async def settle(self, **kwargs: Any) -> bool:
        self.settled.append(kwargs)
        return True

    def approve_ticket(
        self, *, kind: str, tool_name: str, args: dict[str, Any], decision: Any = None
    ) -> str:
        """按 runtime 将计算的那份参数指纹放一张票进去，返回它的 args_hash。"""
        digest = args_hash(tool_name, args)
        self.tickets.append(
            {
                "kind": kind,
                "tool_name": tool_name,
                "args_hash": digest,
                "session_id": "s1",
                "decision": decision if decision is not None else {"decision": DECISION_APPROVE},
                "reason": "ok by me",
                "id": "ix-ticket",
            }
        )
        return digest


@pytest.fixture
def ix(monkeypatch: pytest.MonkeyPatch) -> _FakeInteractions:
    fake = _FakeInteractions()
    monkeypatch.setattr(interactions, "create", fake.create)
    monkeypatch.setattr(interactions, "consume_grant", fake.consume_grant)
    monkeypatch.setattr(interactions, "settle", fake.settle)
    return fake


def _runtime(*, unattended: bool) -> AgentRuntime:
    return AgentRuntime(gateway=None, unattended=unattended)


def _task() -> AgentTask:
    """带 `run_token` 的任务 = 这一轮有可续跑的 run 现场（挂起的前提，见 `_register_interaction`）。"""
    return AgentTask(
        id="turn-1", tenant_id="t1", user_id="u1", session_id="s1", content="hi", run_token="rt-1"
    )


def _call(cid: str = "tc-1") -> dict[str, Any]:
    return {"id": cid, "name": "run_code", "arguments": '{"code": "print(1)"}'}


def _args(cid_call: dict[str, Any]) -> dict[str, Any]:
    return json.loads(cid_call["arguments"])


async def _never(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("不该走到「有人应答」的等待实现")


@pytest.fixture
def audit_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def _spy(tool_call_id: str, tool_name: str, decision: str, **kwargs: Any) -> None:
        records.append(
            {"tool_call_id": tool_call_id, "tool_name": tool_name, "decision": decision, **kwargs}
        )

    monkeypatch.setattr(runtime_module, "_record_approval_audit", _spy)
    return records


async def _begin(rt: AgentRuntime, call: dict[str, Any], monkeypatch) -> Any:
    """跑完"发起审批"的全部前置（票据写入走替身，Redis 不参与）。"""
    monkeypatch.setattr(rt, "_store_approval_ticket", AsyncMock())
    return await rt._begin_approval(  # noqa: SLF001
        call, _task(), level="dangerous", targs=_args(call)
    )


# ── 1. 没人答 ⇒ 挂起 ────────────────────────────────────────────────────


class TestPark:
    async def test_unattended_with_durable_row_parks(
        self, ix: _FakeInteractions, monkeypatch: pytest.MonkeyPatch, audit_spy: list[dict]
    ):
        rt = _runtime(unattended=True)
        call = _call()
        await _begin(rt, call, monkeypatch)
        monkeypatch.setattr(rt, "_wait_approval_decision", _never)

        result = await rt._await_approval(call, _task())  # noqa: SLF001

        assert result["awaiting_user_input"] is True
        assert result["interaction_id"] == "ix-1"
        parked = rt._awaiting_input  # noqa: SLF001
        assert parked is not None
        assert parked["kind"] == "approval"
        assert parked["tool_call_id"] == "tc-1"
        assert parked["tool_name"] == "run_code"
        assert parked["id"] == "ix-1"
        assert "需要你确认" in str(parked["notice"])
        # 挂起 ≠ 拒绝：**不写**决定审计（决定还没发生），也不留悬着的 future
        assert audit_spy == []
        assert rt._pending_approvals == {}  # noqa: SLF001

    async def test_a_run_without_a_resumable_scene_does_not_park(
        self, ix: _FakeInteractions, monkeypatch: pytest.MonkeyPatch, audit_spy: list[dict]
    ):
        """子 Agent（合成会话 `sub:…`、没有 run 行可迁）保持 M6 的即时拒绝。

        挂起要有下文可接：一张挂在没人续跑的会话上的票，用户答了也不会发生什么 ——
        那比"没人答所以拒了"更不诚实，所以这里连**登记**都不做。
        """
        rt = _runtime(unattended=True)
        call = _call()
        child = AgentTask(
            id="turn-1", tenant_id="t1", user_id="u1", session_id="sub:s1:r1", content="hi"
        )
        monkeypatch.setattr(rt, "_store_approval_ticket", AsyncMock())
        await rt._begin_approval(  # noqa: SLF001
            call, child, level="dangerous", targs=_args(call)
        )
        monkeypatch.setattr(rt, "_wait_approval_decision", _never)

        result = await rt._await_approval(call, child)  # noqa: SLF001

        assert "unattended" in result["error"]
        assert ix.created == [], "不该登记一张取不走的票"
        assert rt._awaiting_input is None  # noqa: SLF001
        assert audit_spy and audit_spy[0]["decision"] == DECISION_REJECT

    async def test_attended_timeout_parks(
        self, ix: _FakeInteractions, monkeypatch: pytest.MonkeyPatch, audit_spy: list[dict]
    ):
        rt = _runtime(unattended=False)
        call = _call()
        await _begin(rt, call, monkeypatch)
        monkeypatch.setattr(rt, "_wait_approval_decision", AsyncMock(return_value=None))

        result = await rt._await_approval(call, _task())  # noqa: SLF001

        assert result["awaiting_user_input"] is True, "没人答 ≠ 答了不"
        assert audit_spy == []
        assert ix.settled == [], "没有答复也就没有要收尾的现场"

    async def test_without_durable_row_falls_back_to_m6_deny(
        self, ix: _FakeInteractions, monkeypatch: pytest.MonkeyPatch, audit_spy: list[dict]
    ):
        """落库现场写不进去（PG 不可用）时**必须**退回即时拒绝：静默挂起比拒绝更糟。"""
        ix.create_id = ""
        rt = _runtime(unattended=True)
        call = _call()
        await _begin(rt, call, monkeypatch)
        monkeypatch.setattr(rt, "_wait_approval_decision", _never)

        result = await rt._await_approval(call, _task())  # noqa: SLF001

        assert "unattended" in result["error"]
        assert rt._awaiting_input is None  # noqa: SLF001
        assert len(audit_spy) == 1 and audit_spy[0]["decision"] == DECISION_REJECT

    async def test_ask_user_parks_without_an_answer(
        self, ix: _FakeInteractions, monkeypatch: pytest.MonkeyPatch
    ):
        rt = _runtime(unattended=True)
        call = {"id": "tc-ask", "name": ASK_USER_TOOL, "arguments": '{"question": "选哪个？"}'}
        monkeypatch.setattr(rt, "_wait_answer_decision", _never)

        await rt._ask_event(call, _task())  # noqa: SLF001
        result = await rt._await_answer(call, _task())  # noqa: SLF001

        assert result["awaiting_user_input"] is True
        assert rt._awaiting_input["kind"] == "question"  # noqa: SLF001
        assert "需要你回答" in str(rt._awaiting_input["notice"])  # noqa: SLF001
        assert rt._pending_answers == {}  # noqa: SLF001

    async def test_ask_without_durable_row_keeps_declined_answer(
        self, ix: _FakeInteractions, monkeypatch: pytest.MonkeyPatch
    ):
        ix.create_id = ""
        rt = _runtime(unattended=True)
        call = {"id": "tc-ask", "name": ASK_USER_TOOL, "arguments": '{"question": "?"}'}
        monkeypatch.setattr(rt, "_wait_answer_decision", _never)

        await rt._ask_event(call, _task())  # noqa: SLF001
        result = await rt._await_answer(call, _task())  # noqa: SLF001

        assert result["declined"] is True
        assert rt._awaiting_input is None  # noqa: SLF001


# ── 2. 现场登记的内容 ───────────────────────────────────────────────────


class TestRegistration:
    async def test_approval_registers_the_call_fingerprint(
        self, ix: _FakeInteractions, monkeypatch: pytest.MonkeyPatch
    ):
        rt = _runtime(unattended=True)
        call = _call()
        await _begin(rt, call, monkeypatch)

        assert len(ix.created) == 1
        created = ix.created[0]
        assert created["kind"] == "approval"
        assert created["tool_name"] == "run_code"
        assert created["tool_call_id"] == "tc-1"
        assert created["session_id"] == "s1"
        assert created["user_id"] == "u1"
        # 认票认的是**参数指纹**：续跑的那一轮是新 run、新 tool_call_id
        assert created["args_hash"] == args_hash("run_code", _args(call))
        assert created["request"]["arguments"] == _args(call)

    async def test_ask_registers_the_question(
        self, ix: _FakeInteractions, monkeypatch: pytest.MonkeyPatch
    ):
        rt = _runtime(unattended=False)
        call = {
            "id": "tc-ask",
            "name": ASK_USER_TOOL,
            "arguments": '{"question": "选哪个？", "options": ["A", "B"]}',
        }

        event = await rt._ask_event(call, _task())  # noqa: SLF001

        assert event.type == "ask" and event.options == ["A", "B"]
        created = ix.created[0]
        assert created["kind"] == "question"
        assert created["tool_name"] == ASK_USER_TOOL
        assert created["args_hash"] == args_hash(ASK_USER_TOOL, {"question": "选哪个？", "options": ["A", "B"]})


# ── 3. 取票：答复只生效一次、且认参数 ───────────────────────────────────


class TestGrant:
    async def test_grant_executes_without_waiting(
        self, ix: _FakeInteractions, monkeypatch: pytest.MonkeyPatch, audit_spy: list[dict]
    ):
        rt = _runtime(unattended=True)
        call = _call()
        ix.approve_ticket(kind="approval", tool_name="run_code", args=_args(call))
        monkeypatch.setattr(rt, "_wait_approval_decision", _never)
        monkeypatch.setattr(rt, "_second_check_approval", AsyncMock(return_value=""))
        execute = AsyncMock(return_value=({"ok": True}, None))
        monkeypatch.setattr(rt, "_execute_tool", execute)

        await _begin(rt, call, monkeypatch)
        result = await rt._await_approval(call, _task())  # noqa: SLF001

        assert result == {"ok": True}
        execute.assert_awaited_once()
        assert audit_spy and audit_spy[0]["decision"] == DECISION_APPROVE
        # 取到票 ⇒ **不再**登记新的等待（否则同一个问题在列表里出现两遍）
        assert ix.created == []

    async def test_grant_is_consumed_once(self, ix: _FakeInteractions, monkeypatch):
        """两次续跑拿不到同一张票：`consume_grant` 是单次消费（替身按 pop 模拟）。"""
        rt = _runtime(unattended=True)
        call = _call()
        ix.approve_ticket(kind="approval", tool_name="run_code", args=_args(call))
        await _begin(rt, call, monkeypatch)
        assert ix.tickets == [], "第一次问的时候就该被取走"
        await _begin(rt, call, monkeypatch)
        assert len(ix.created) == 1, "票没了 ⇒ 第二次要重新问，而不是放行"

    async def test_grant_for_different_arguments_does_not_apply(
        self, ix: _FakeInteractions, monkeypatch: pytest.MonkeyPatch
    ):
        """批准的是一件事，模型发来的是另一件事（参数变了）⇒ 必须重新确认。"""
        rt = _runtime(unattended=True)
        call = _call()
        ix.approve_ticket(kind="approval", tool_name="run_code", args={"code": "rm -rf /"})
        monkeypatch.setattr(rt, "_wait_approval_decision", _never)

        await _begin(rt, call, monkeypatch)
        result = await rt._await_approval(call, _task())  # noqa: SLF001

        assert result["awaiting_user_input"] is True, "没匹配上就挂起重问，绝不执行"
        assert ix.tickets, "哈希不同 ⇒ 票还在原地"

    async def test_reject_grant_is_not_executed(
        self, ix: _FakeInteractions, monkeypatch: pytest.MonkeyPatch, audit_spy: list[dict]
    ):
        rt = _runtime(unattended=True)
        call = _call()
        ix.approve_ticket(
            kind="approval",
            tool_name="run_code",
            args=_args(call),
            decision={"decision": DECISION_REJECT, "reason": "别跑"},
        )
        monkeypatch.setattr(rt, "_execute_tool", _never)

        await _begin(rt, call, monkeypatch)
        result = await rt._await_approval(call, _task())  # noqa: SLF001

        assert "denied by user" in result["error"]
        assert audit_spy[0]["decision"] == DECISION_REJECT
        assert audit_spy[0]["reason"] == "别跑", "跨进程带回来的理由也要进审计"

    async def test_unreadable_grant_fails_closed(
        self, ix: _FakeInteractions, monkeypatch: pytest.MonkeyPatch, audit_spy: list[dict]
    ):
        """票读不出 = 没有任何人给过一次可执行的批准 ⇒ 拒绝执行（而不是猜）。"""
        rt = _runtime(unattended=True)
        call = _call()
        ix.approve_ticket(
            kind="approval", tool_name="run_code", args=_args(call), decision={"nonsense": 1}
        )
        monkeypatch.setattr(rt, "_execute_tool", _never)

        await _begin(rt, call, monkeypatch)
        result = await rt._await_approval(call, _task())  # noqa: SLF001

        assert "could not be read" in result["error"]
        assert audit_spy[0]["decision"] == DECISION_REJECT

    async def test_ask_grant_reuses_the_answer(self, ix: _FakeInteractions, monkeypatch):
        rt = _runtime(unattended=False)
        call = {"id": "tc-ask", "name": ASK_USER_TOOL, "arguments": '{"question": "选哪个？"}'}
        ix.approve_ticket(
            kind="question", tool_name=ASK_USER_TOOL, args={"question": "选哪个？"},
            decision={"answer": "B"},
        )
        monkeypatch.setattr(rt, "_ask_event", _never)

        got = await rt._take_ask_grant(call, _task())  # noqa: SLF001

        assert got == {"answer": "B"}
        assert ix.created == [], "已有答复 ⇒ 不再问一遍"

    async def test_ask_grant_unreadable_falls_back_to_asking(self, ix: _FakeInteractions):
        rt = _runtime(unattended=False)
        call = {"id": "tc-ask", "name": ASK_USER_TOOL, "arguments": '{"question": "?"}'}
        ix.approve_ticket(
            kind="question", tool_name=ASK_USER_TOOL, args={"question": "?"}, decision={"x": 1}
        )

        assert await rt._take_ask_grant(call, _task()) is None  # noqa: SLF001


# ── 4. 实时答复要把落库现场一并关掉 ─────────────────────────────────────


class TestSettle:
    async def test_live_answer_settles_the_row(
        self, ix: _FakeInteractions, monkeypatch: pytest.MonkeyPatch
    ):
        """不关的后果不是"多一行脏数据"而是语义：列表挂着已答完的问题，用户再答一次
        就变成一张将来能被同类调用取走的票。"""
        rt = _runtime(unattended=False)
        call = _call()
        await _begin(rt, call, monkeypatch)
        monkeypatch.setattr(
            rt, "_wait_approval_decision",
            AsyncMock(return_value=ApprovalDecision(decision=DECISION_APPROVE, reason="行")),
        )
        monkeypatch.setattr(rt, "_second_check_approval", AsyncMock(return_value=""))
        monkeypatch.setattr(rt, "_execute_tool", AsyncMock(return_value=({"ok": True}, None)))

        result = await rt._await_approval(call, _task())  # noqa: SLF001

        assert result == {"ok": True}
        assert ix.settled == [
            {
                "interaction_id": "ix-1",
                "decision": {"decision": DECISION_APPROVE},
                "reason": "行",
                "decided_by": "u1",
            }
        ]

    async def test_live_edit_settles_with_the_edited_arguments(
        self, ix: _FakeInteractions, monkeypatch: pytest.MonkeyPatch
    ):
        rt = _runtime(unattended=False)
        call = _call()
        await _begin(rt, call, monkeypatch)
        monkeypatch.setattr(
            rt,
            "_wait_approval_decision",
            AsyncMock(
                return_value=ApprovalDecision(
                    decision="edit", arguments={"code": "print(2)"}, reason="改成这样"
                )
            ),
        )
        monkeypatch.setattr(rt, "_second_check_approval", AsyncMock(return_value=""))
        monkeypatch.setattr(rt, "_execute_tool", AsyncMock(return_value=({"ok": True}, None)))

        await rt._await_approval(call, _task())  # noqa: SLF001

        assert ix.settled[0]["decision"] == {"decision": "edit", "arguments": {"code": "print(2)"}}

    async def test_declined_answer_settles(self, ix: _FakeInteractions, monkeypatch):
        rt = _runtime(unattended=False)
        call = {"id": "tc-ask", "name": ASK_USER_TOOL, "arguments": '{"question": "?"}'}
        await rt._ask_event(call, _task())  # noqa: SLF001
        monkeypatch.setattr(rt, "_wait_answer_decision", AsyncMock(return_value=ASK_DECLINED))

        got = await rt._await_answer(call, _task())  # noqa: SLF001

        assert got["declined"] is True
        assert ix.settled[0]["decision"] == {"declined": True}

    async def test_answer_settles(self, ix: _FakeInteractions, monkeypatch):
        rt = _runtime(unattended=False)
        call = {"id": "tc-ask", "name": ASK_USER_TOOL, "arguments": '{"question": "?"}'}
        await rt._ask_event(call, _task())  # noqa: SLF001
        monkeypatch.setattr(rt, "_wait_answer_decision", AsyncMock(return_value="B"))

        got = await rt._await_answer(call, _task())  # noqa: SLF001

        assert got == {"answer": "B"}
        assert ix.settled[0]["decision"] == {"answer": "B"}

    async def test_settle_is_a_noop_without_a_durable_row(
        self, ix: _FakeInteractions, monkeypatch: pytest.MonkeyPatch
    ):
        ix.create_id = ""
        rt = _runtime(unattended=False)
        call = {"id": "tc-ask", "name": ASK_USER_TOOL, "arguments": '{"question": "?"}'}
        await rt._ask_event(call, _task())  # noqa: SLF001
        monkeypatch.setattr(rt, "_wait_answer_decision", AsyncMock(return_value="B"))

        await rt._await_answer(call, _task())  # noqa: SLF001

        assert ix.settled == []


# ── 5. 挂起的回合：一次 run 的整体出口 ─────────────────────────────────


def _gateway_with_calls(calls: list[dict[str, Any]]) -> MagicMock:
    from app.gateway.provider import ChatResponse, ToolCall

    gateway = MagicMock()

    async def fake_stream(**_kwargs: Any):
        yield ChatResponse(
            content="",
            finish_reason="tool_calls",
            tool_calls=[ToolCall(**c) for c in calls],
        )

    gateway.chat_stream = fake_stream
    return gateway


class _RecordingStore:
    def __init__(self) -> None:
        self.appended: list[list[dict[str, Any]]] = []

    async def get_or_init(self, session_id: str, msgs: list[dict[str, Any]]) -> list[dict]:
        return list(msgs)

    async def append(self, session_id: str, msgs: list[dict[str, Any]]) -> None:
        self.appended.append(list(msgs))


async def test_suspend_marks_awaiting_input_and_defers_batch(
    ix: _FakeInteractions, monkeypatch: pytest.MonkeyPatch
):
    """一步在等人 ⇒ 回合结束时的六件事（run() 级，单元替身）。

    1. 迁到 `awaiting_input` 且**不**写 `completed`；
    2. 快照里 `done_tools` 含停在等待上的那一条（它的结果已经在历史里），**不含**还没跑的；
    3. 还没跑的调用拿到 `not_executed` 结果 —— tool_calls 与 tool 消息必须配对；
    4. 发出一条 `text` 事件解释"这一步在等你"（前端/SSE 无需改动就看得见）；
    5. 会话缓存**保存**（挂起不是会话结束 ⇒ 不该触发 L3 rollup）；
    6. 不跑 rubric 自查。
    """
    from app.agent import checkpoint as cp

    marks: list[str] = []
    saved: list[dict[str, Any]] = []

    async def _mark(**kwargs: Any) -> bool:
        marks.append(str(kwargs.get("status") or ""))
        return True

    async def _save(**kwargs: Any) -> None:
        saved.append(kwargs)

    monkeypatch.setattr(cp, "mark", _mark)
    monkeypatch.setattr(cp, "save", _save)

    from app.tools.context import set_tool_context

    set_tool_context(session_id="s1")

    gateway = _gateway_with_calls(
        [
            # `tools_mode=ask` 下 write 级工具需要确认，且不落 Broker（delete/external 才要）——
            # 这里要的是"确认 → 没人答 → 挂起"这条路径，不是服务端授权那一层
            {"id": "c1", "name": "write_file", "arguments": '{"path": "a.txt", "content": "x"}'},
            {"id": "c2", "name": "shell_exec", "arguments": '{"command": "ls"}'},
        ]
    )
    rt = AgentRuntime(gateway=gateway)
    monkeypatch.setattr(rt, "_store_approval_ticket", AsyncMock())
    monkeypatch.setattr(rt, "_wait_approval_decision", AsyncMock(return_value=None))
    rubric = AsyncMock(return_value=None)
    monkeypatch.setattr(rt, "_run_rubric_turn", rubric)
    store = _RecordingStore()
    rt._session_store = store  # noqa: SLF001

    task = AgentTask(
        id="t",
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        content="hi",
        max_turns=3,
        run_token="rt-1",
        llm_config={"mode": "normal", "tools_mode": "ask"},
    )
    events = [event async for event in rt.run(task)]

    assert marks == ["awaiting_input"], f"挂起必须迁出活跃集合，且不写 completed：{marks}"
    assert saved, "挂起的现场同样要落 checkpoint（否则下一轮无从继续）"
    assert saved[0]["checkpoint"]["done_tools"] == ["c1"], "还没跑的 c2 不算完成"
    assert any(
        event.type == "text" and "需要你确认" in str(event.content) for event in events
    ), "用户要看得见「在等什么」"
    tool_results = {
        event.tool_call_id: event.content
        for event in events
        if event.type == "tool_result"
    }
    assert "没有执行" in str(tool_results.get("c2")), "被推迟的调用要有一条明确的 tool 结果"
    assert store.appended, "缓存保存 = 挂起不当作异常退出（不触发会话级 rollup）"
    rubric.assert_not_awaited()
