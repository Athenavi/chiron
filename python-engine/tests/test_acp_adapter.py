"""ACP 适配层：映射（纯函数）+ HTTP/SSE 客户端 + 缺依赖时的行为。

**不测 SDK 接线本身**（`agent.py` 里真正调 SDK 的那几行）—— 那需要装官方 SDK 并起一个 ACP 客户端，
属于"待联调"的范畴（见 `acp_adapter/README.md`）。这里测的是两块**能被完全验证**的东西：

* `mapping.py`：事件 → ACP 更新语义（含把内联思考切成 thought/正文两类）；
* `stream.py`：SSE 解析、终态判定、取消请求的响应解析、以及"提交失败也要产出 error 事件"。
"""

from __future__ import annotations

from typing import Any

import pytest

from acp_adapter import agent as acp_agent
from acp_adapter import mapping
from acp_adapter.mapping import (
    Notice,
    PermissionAsk,
    RunFinished,
    TextDelta,
    ThoughtDelta,
    ToolFinished,
    ToolStarted,
)
from acp_adapter.stream import ChironClient, parse_sse_line, stream_run

# ── mapping：思考切分 ───────────────────────────────────────────────────


def test_split_thinking_without_tags():
    assert mapping.split_thinking("你好") == [TextDelta("你好")]


def test_split_thinking_separates_thought_from_answer():
    out = mapping.split_thinking("[thinking]先想[/thinking]答案是 42")

    assert out == [ThoughtDelta("先想"), TextDelta("答案是 42")]


def test_split_thinking_handles_unclosed_tag():
    """未闭合的思考标记不该把内容弄丢（模型可能被截断）。"""
    out = mapping.split_thinking("[thinking]还没想完")

    assert out == [ThoughtDelta("还没想完")]


def test_split_thinking_handles_empty():
    assert mapping.split_thinking("") == []


# ── mapping：事件翻译 ───────────────────────────────────────────────────


def test_translate_text_event():
    assert mapping.translate({"type": "text", "content": "hi"}) == [TextDelta("hi")]


def test_translate_tool_call_and_result():
    started = mapping.translate(
        {"type": "tool_call", "tool_call_id": "c1", "tool_name": "read_file", "tool_arguments": "{}"}
    )
    finished = mapping.translate({"type": "tool_result", "tool_call_id": "c1", "content": "{}"})

    assert started == [ToolStarted(call_id="c1", name="read_file", arguments="{}")]
    assert finished == [ToolFinished(call_id="c1", ok=True, detail="")]


def test_translate_tool_result_error_is_recorded():
    """被拒/出错是护栏证据 —— 必须标成 ok=False 并带上原因。"""
    out = mapping.translate(
        {"type": "tool_result", "tool_call_id": "c2", "content": '{"error":"denied by policy"}'}
    )

    assert out == [ToolFinished(call_id="c2", ok=False, detail="denied by policy")]


def test_translate_approval_becomes_permission_ask():
    out = mapping.translate(
        {"type": "approval", "tool_call_id": "c3", "tool_name": "shell_exec", "tool_arguments": "{}"}
    )

    assert out == [PermissionAsk(call_id="c3", tool="shell_exec", arguments="{}")]


def test_translate_guardrail_and_ask_become_notices():
    """ACP 没有与它们一一对应的通道 —— 降级成提示，而不是硬塞进 permission。"""
    guard = mapping.translate({"type": "guardrail_blocked", "content": "blocked"})
    ask = mapping.translate({"type": "ask", "content": "你喜欢哪种?"})

    assert guard == [Notice(kind="guardrail", detail="blocked")]
    assert ask == [Notice(kind="ask", detail="你喜欢哪种?")]


def test_translate_terminal_events():
    assert mapping.translate({"type": "done"}) == [RunFinished("done")]
    assert mapping.translate({"type": "cancelled"}) == [RunFinished("cancelled")]
    assert mapping.translate({"type": "error", "error": "boom"}) == [
        RunFinished("error", "boom")
    ]


def test_translate_ignores_operational_events():
    """trace_span / compaction / rubric 面向运维与评测，不面向编辑器用户。"""
    for etype in ("trace_span", "compaction", "rubric", "unknown_future_type"):
        assert mapping.translate({"type": etype}) == []


# ── stream：SSE 解析与终态 ──────────────────────────────────────────────


def _mock_transport(events: list[str], *, status: int = 200) -> Any:
    """构造一个返回给定 SSE 行的 httpx transport。"""
    import httpx

    body = "".join(f"data: {line}\n\n" for line in events)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/events":
            return httpx.Response(
                200, text=body, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(status, json={"ok": True})

    return httpx.MockTransport(handler)


def test_parse_sse_line():
    assert parse_sse_line('data: {"type":"text"}') == {"type": "text"}
    assert parse_sse_line("event: ping") is None
    assert parse_sse_line("data:") is None
    assert parse_sse_line("data: not json") is None
    assert parse_sse_line('data: [1,2]') is None  # 非对象


async def test_events_stops_at_terminal_event():
    client = ChironClient(
        "http://t", "k", transport=_mock_transport(['{"type":"text"}', '{"type":"done"}', '{"type":"text"}'])
    )

    seen = [event async for event in client.events("s1")]

    assert [e["type"] for e in seen] == ["text", "done"], "终态之后的残留不该继续读"


async def test_cancelled_is_also_terminal():
    """`cancelled` 是终态 —— 否则用户取消后编辑器会一直等。"""
    client = ChironClient("http://t", "k", transport=_mock_transport(['{"type":"cancelled"}']))

    seen = [event async for event in client.events("s1")]

    assert [e["type"] for e in seen] == ["cancelled"]


async def test_stream_run_emits_error_instead_of_raising():
    """提交失败要变成一条 `error` 事件 —— 抛出去只会变成 SDK dispatcher 里的无上下文崩溃。"""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = ChironClient("http://t", "k", transport=httpx.MockTransport(handler))

    events = [event async for event in stream_run(client, "s1", "hi")]

    assert events and events[0]["type"] == "error"
    assert "submit_failed" in events[0]["error"]


async def test_interrupt_parses_gateway_response():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/agent/interrupt"
        return httpx.Response(200, json={"success": True, "data": {"ok": True, "redis": True}})

    client = ChironClient("http://t", "k", transport=httpx.MockTransport(handler))

    assert await client.interrupt("s1") is True


async def test_interrupt_reports_rejection():
    import httpx

    client = ChironClient(
        "http://t",
        "k",
        transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"ok": False})),
    )

    assert await client.interrupt("s1") is False


# ── 缺依赖 / 缺身份 ─────────────────────────────────────────────────────


def test_require_sdk_gives_actionable_hint():
    """没装官方 SDK 时要给出**可执行**的提示（本机确实没装，所以这条能真跑）。"""
    try:
        import acp  # noqa: F401

        pytest.skip("本机已安装 agent-client-protocol")
    except ImportError:
        pass

    with pytest.raises(SystemExit) as excinfo:
        acp_agent.require_sdk()

    assert "pip install agent-client-protocol" in str(excinfo.value)


def test_agent_requires_api_key(monkeypatch):
    """一个连接 = 一个身份：没有 API Key 就不该连上再报错。"""
    monkeypatch.delenv(acp_agent.API_KEY_ENV, raising=False)

    with pytest.raises(SystemExit) as excinfo:
        acp_agent.ChironACPAgent()

    assert acp_agent.API_KEY_ENV in str(excinfo.value)


def test_prompt_text_collects_text_blocks_and_warns_on_others(caplog: Any):
    """非文本块要留痕：'贴了图但没反应'必须能从日志里看出来。"""
    class _Text:
        type = "text"
        text = "你好"

    class _Image:
        type = "image"

    with caplog.at_level("WARNING"):
        out = acp_agent.prompt_text([_Text(), _Image()])

    assert out == "你好"
    assert any("非文本块" in rec.message for rec in caplog.records)
