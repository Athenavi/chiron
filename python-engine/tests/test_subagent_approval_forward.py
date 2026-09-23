"""P3-后续：子 Agent 的 approval 必须**送到前端且能回传**。

背景（docs/subagent-interaction-redesign.md §八「尚未完成」）：

子 Agent 默认 `tools_mode=auto`，而 `shell_exec` 这类工具在 auto 下就需要确认 ——
所以"子 Agent 请求批准"几乎每次调用命令类工具都会发生。修复前它只被转成一行
`subagent.notice`：

* 用户**看得见却批不了**（事件里没有 `tool_call_id`，前端无从回传）；
* 子 Agent 于是空转到 runtime 的 approval 超时（默认 300s）后以
  `approval timed out` 被拒 —— 一次**必然发生**的空转。

这里钉住三件事：
1. approval 走**结构化**事件（带 `tool_call_id` / `tool_name` / `tool_arguments`）；
2. 它属于 `IMPORTANT_EVENTS`：不受每秒预算约束、队列满时也不被牺牲
   （丢了就等于又回到"空转到超时"）；
3. 拿不到 `tool_call_id` 时**可见地降级**为 notice（而不是发一张点了没反应的卡片）。
"""
from __future__ import annotations

import asyncio

import pytest

from app.agent.event_sink import EV_APPROVAL, EV_NOTICE, EV_STATUS, IMPORTANT_EVENTS, ST_TOOL, EventSink
from app.agent.subagent_runner import SubAgentRunner, _step_kind

# ── 事件面（前端契约）──


def test_approval_payload_carries_reply_credentials():
    sink = EventSink()
    sink.emit_approval(
        run_id="rs_1",
        tool_call_id="tc_1",
        tool_name="shell_exec",
        tool_arguments='{"command": "ls"}',
        content="请求执行 shell_exec（级别 high）",
        parent_run_id="rs_0",
        depth=2,
        profile="reviewer",
    )
    payload = sink.drain()[0].to_payload()

    assert payload["type"] == EV_APPROVAL
    # 回传凭据：前端就是拿它调 POST /v1/agent/approval
    assert payload["tool_call_id"] == "tc_1"
    assert payload["tool_name"] == "shell_exec"
    assert payload["tool_arguments"] == '{"command": "ls"}'
    assert payload["content"].startswith("请求执行")
    assert payload["parent_run_id"] == "rs_0" and payload["depth"] == 2


def test_approval_payload_has_no_id_alias():
    """payload 里不出现 `id`：Go 的回放路径会把它覆盖成 Stream 消息 id，
    前端若按 `id` 回传就会拿一个 stream id 当 tool_call_id（必然失败）。"""
    sink = EventSink()
    sink.emit_approval(run_id="rs_1", tool_call_id="tc_1")
    payload = sink.drain()[0].to_payload()
    assert "id" not in payload


def test_approval_is_an_important_event():
    assert EV_APPROVAL in IMPORTANT_EVENTS


def test_approval_bypasses_per_run_budget():
    """预算用尽时普通预览可丢，审批**不可丢** —— 用户在等它。"""
    sink = EventSink(merge_window=0.0, per_run_budget=1)
    for _ in range(5):
        sink.emit_progress(run_id="rs_1", channel=EV_STATUS, status=ST_TOOL)  # 耗尽预算
    sink.emit_approval(run_id="rs_1", tool_call_id="tc_1", tool_name="shell_exec")

    types = [e.type for e in sink.drain()]
    assert types.count(EV_STATUS) == 1   # 预算内只留一条预览
    assert EV_APPROVAL in types
    assert sink.dropped >= 4


def test_approval_survives_a_full_queue():
    """队列满时牺牲的是最旧的预览，绝不是审批事件。"""
    sink = EventSink(maxsize=1, merge_window=0.0, per_run_budget=1000)
    sink.emit_progress(run_id="rs_1", channel=EV_STATUS, status=ST_TOOL)
    sink.emit_approval(run_id="rs_1", tool_call_id="tc_1", tool_name="shell_exec")

    types = [e.type for e in sink.drain()]
    assert types == [EV_APPROVAL]


def test_approval_without_credentials_is_not_emitted(caplog):
    """没有 tool_call_id 的审批事件是**无用**的（前端没法回传）：宁可记告警也不发。"""
    sink = EventSink()
    with caplog.at_level("WARNING"):
        sink.emit_approval(run_id="rs_1", tool_call_id="", tool_name="shell_exec")
    assert sink.drain() == []


def test_approval_is_not_merged_by_merge_window():
    """审批不参与内容合并：两次不同的审批不能被揉成一条。"""
    sink = EventSink(merge_window=60.0)
    sink.emit_approval(run_id="rs_1", tool_call_id="tc_1")
    sink.emit_approval(run_id="rs_1", tool_call_id="tc_2")
    payloads = [e.to_payload() for e in sink.drain()]
    assert [p["tool_call_id"] for p in payloads] == ["tc_1", "tc_2"]


def test_step_kind_keeps_approval_distinct():
    """落库的 kind 必须是 approval：历史回放（DB steps）据此还原可点击的卡片。"""
    assert _step_kind("approval") == "approval"
    assert _step_kind("ask") == "notice"   # ask 的可交互回传是另一条通道，尚未接入


# ── runner 转发（端到端一半：引擎侧）──


class _NullStore:
    async def start_run(self, *args, **kwargs):
        return None

    async def add_step(self, *args, **kwargs):
        return None

    async def flush_steps(self, *args, **kwargs):
        return None

    async def finish_run(self, *args, **kwargs):
        return None


def _runner_with_events(monkeypatch, events):
    from app.agent import runtime as runtime_mod

    async def _run(self, task):  # noqa: ANN001
        for evt in events:
            yield evt

    monkeypatch.setattr(runtime_mod.AgentRuntime, "run", _run)
    sink = EventSink()
    runner = SubAgentRunner(
        store=_NullStore(),
        sink=sink,
        gateway=object(),
        parent_session_id="s1",
        parent_run_id="rs_parent",
        tenant_id="t1",
        user_id="u1",
    )
    return runner, sink


@pytest.mark.asyncio
async def test_runner_forwards_approval_with_credentials(monkeypatch):
    from app.agent import runtime as runtime_mod

    approval = runtime_mod.AgentEvent(
        type="approval",
        tool_call_id="tc_1",
        tool_name="shell_exec",
        tool_arguments='{"command": "ls"}',
        content="请求执行 shell_exec（级别 high）",
    )
    runner, sink = _runner_with_events(monkeypatch, [approval])

    await runner.run("跑个命令", profile_ref="", mode="normal", max_turns=2)

    payloads = [e.to_payload() for e in sink.drain()]
    approvals = [p for p in payloads if p["type"] == EV_APPROVAL]
    assert approvals, "子 Agent 请求审批必须送到前端（否则只能空转到超时）"
    assert approvals[0]["tool_call_id"] == "tc_1"
    assert approvals[0]["tool_name"] == "shell_exec"
    assert approvals[0]["tool_arguments"] == '{"command": "ls"}'


@pytest.mark.asyncio
async def test_runner_degrades_to_notice_without_tool_call_id(monkeypatch):
    """引擎没给 id 时仍要让用户看见（可见地降级），而不是静默不发。"""
    from app.agent import runtime as runtime_mod

    approval = runtime_mod.AgentEvent(
        type="approval", tool_call_id="", tool_name="shell_exec", content="请求执行 shell_exec"
    )
    runner, sink = _runner_with_events(monkeypatch, [approval])

    await runner.run("跑个命令", profile_ref="", mode="normal", max_turns=2)

    payloads = [e.to_payload() for e in sink.drain()]
    assert not [p for p in payloads if p["type"] == EV_APPROVAL]
    notices = [p for p in payloads if p["type"] == EV_NOTICE]
    assert notices and "需要确认" in notices[0]["content"]


@pytest.mark.asyncio
async def test_runner_forwards_ask_as_notice(monkeypatch):
    """ask 暂时只保证"看得见"（其回传通道 /v1/agent/answer 尚未接入前端控件）。"""
    from app.agent import runtime as runtime_mod

    ask = runtime_mod.AgentEvent(type="ask", tool_call_id="tc_2", content="选哪个环境？")
    runner, sink = _runner_with_events(monkeypatch, [ask])

    await runner.run("问一句", profile_ref="", mode="normal", max_turns=2)

    notices = [e.to_payload() for e in sink.drain() if e.type == EV_NOTICE]
    assert notices and "需要补充信息" in notices[0]["content"]


@pytest.mark.asyncio
async def test_approval_reaches_persistent_delivery(monkeypatch):
    """常驻投递器（后台 run 的通道）也必须收到审批事件。

    后台 run 的父 turn 早已结束 —— 走不到 SSE 生成器，审批只能靠常驻投递器
    （Redis Stream + pub/sub → 网关 → 浏览器）。
    """
    from app.agent import runtime as runtime_mod

    delivered: list[dict] = []

    async def _deliver(payload):
        delivered.append(payload)

    runner, sink = _runner_with_events(
        monkeypatch,
        [runtime_mod.AgentEvent(type="approval", tool_call_id="tc_9", tool_name="shell_exec")],
    )
    sink.attach_persistent(_deliver)

    await runner.run("跑个命令", profile_ref="", mode="normal", max_turns=2)
    await asyncio.sleep(0)  # 让投递器的后台任务跑一轮
    await asyncio.sleep(0)

    assert any(p.get("tool_call_id") == "tc_9" for p in delivered)
