"""C3 回归：工具批的分组规则与并发执行。

三条必须成立：

1. **只有"读级 + 无交互语义 + 无同路径冲突"的调用才并发** —— 其余一律串行，
   宁可少并发也不让顺序变得不可预测；
2. **事件保序**：`tool_result` 按 `tool_calls` 的原始顺序产出（前端依赖顺序做增量渲染）；
3. **单条失败不拖垮整批**（异常转结构化错误）。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.agent.runtime import AgentRuntime, AgentTask, _split_tool_batch
from app.gateway.provider import ChatResponse, ToolCall
from app.tools.context import set_tool_context


def _call(cid: str, name: str, arguments: str = "{}") -> dict[str, Any]:
    return {"id": cid, "name": name, "arguments": arguments}


# ── 分组规则 ──────────────────────────────────────────────────────────────


def test_readonly_calls_are_parallelizable():
    parallel, serial = _split_tool_batch(
        [
            _call("c1", "read_file", '{"path": "a.txt"}'),
            _call("c2", "read_file", '{"path": "b.txt"}'),
            _call("c3", "glob_files", '{"pattern": "*.py"}'),
        ]
    )
    assert [c["id"] for c in parallel] == ["c1", "c2", "c3"]
    assert serial == []


def test_write_and_delete_calls_stay_serial():
    """写/删有副作用，顺序敏感 —— 必须串行。"""
    parallel, serial = _split_tool_batch(
        [
            _call("c1", "read_file", '{"path": "a.txt"}'),
            _call("c2", "write_file", '{"path": "b.txt", "content": "x"}'),
            _call("c3", "shell_exec", '{"command": "ls"}'),
        ]
    )
    assert [c["id"] for c in parallel] == ["c1"]
    assert [c["id"] for c in serial] == ["c2", "c3"]


def test_ask_user_is_serial():
    """`ask_user` 有交互语义（要逐个等答案），不能并发。"""
    parallel, serial = _split_tool_batch(
        [
            _call("c1", "read_file", '{"path": "a.txt"}'),
            _call("c2", "ask_user", '{"question": "?"}'),
        ]
    )
    assert [c["id"] for c in parallel] == ["c1"]
    assert [c["id"] for c in serial] == ["c2"]


def test_same_path_calls_are_downgraded_to_serial():
    """同路径并发读写在语义上无法保证顺序 → 相关调用全部降级串行。

    对位 deepagents 的 `_parallel_file_mutation_error`：它为同一问题在文件工具里加了
    "同路径并发写"拒绝。
    """
    parallel, serial = _split_tool_batch(
        [
            _call("c1", "read_file", '{"path": "same.txt"}'),
            _call("c2", "read_file", '{"path": "./same.txt"}'),  # 归一化后同路径
            _call("c3", "read_file", '{"path": "other.txt"}'),
        ]
    )
    assert [c["id"] for c in parallel] == ["c3"]
    assert sorted(c["id"] for c in serial) == ["c1", "c2"]


# ── 并发执行 ──────────────────────────────────────────────────────────────


def _runtime_with_calls(calls: list[ToolCall], *, model: str = "test-model") -> tuple[AgentRuntime, list[dict]]:
    """构造 runtime + fake gateway：第一轮返回这批工具调用，第二轮收尾。"""
    captured: list[dict] = []
    gateway = MagicMock()
    first = True

    async def fake_stream(**kwargs: Any):
        nonlocal first
        captured.append(kwargs)
        if first:
            first = False
            yield ChatResponse(content="", finish_reason="tool_calls", tool_calls=list(calls))
        else:
            yield ChatResponse(content="done", finish_reason="stop")

    gateway.chat_stream = fake_stream
    runtime = AgentRuntime(gateway=gateway)
    return runtime, captured


def _task(session: str) -> AgentTask:
    return AgentTask(
        id="t",
        tenant_id="t1",
        user_id="u1",
        session_id=session,
        content="hi",
        system_prompt="sp",
        llm_config={"mode": "normal", "tools_mode": "yolo"},
        max_turns=3,
    )


@pytest.mark.asyncio
async def test_readonly_calls_run_concurrently(monkeypatch: pytest.MonkeyPatch):
    """3 个独立读必须**真的并发** —— 否则 C3 等于没做。"""
    set_tool_context(session_id="s-c3-concurrent")
    started: list[float] = []

    # 注意 `self`：monkeypatch 设的是**类属性**，通过实例访问会绑定 self —— 少写这个参数
    # 会抛 TypeError，而它在 `gather(return_exceptions=True)` 里被吞成"工具执行失败"，
    # 表现为"看起来执行了但结果全是错"（loop guard 会报"最近 3 次返回相同结果"）。
    async def slow_readonly(
        self: AgentRuntime, call: dict[str, Any], task: AgentTask
    ) -> dict[str, Any]:
        started.append(time.monotonic())
        await asyncio.sleep(0.05)
        return {"path": call["arguments"]}

    monkeypatch.setattr(AgentRuntime, "_execute_readonly_call", slow_readonly)

    runtime, _ = _runtime_with_calls(
        [
            ToolCall(id="c1", name="read_file", arguments='{"path": "a.txt"}'),
            ToolCall(id="c2", name="read_file", arguments='{"path": "b.txt"}'),
            ToolCall(id="c3", name="read_file", arguments='{"path": "c.txt"}'),
        ]
    )
    began = time.monotonic()
    _ = [event async for event in runtime.run(_task("s-c3-concurrent"))]
    elapsed = time.monotonic() - began

    assert len(started) == 3, "三个读都应执行"
    # 用**开始时间的跨度**判断并发，而不是总耗时：总耗时含 runtime 的其它开销
    # （压缩、trace、checkpoint 尝试…），阈值会很脆；而"三个调用是否几乎同时开始"
    # 才是并发与否的直接证据（串行时会被各自的 sleep 拉开 ≈0.1s）。
    spread = max(started) - min(started)
    assert spread < 0.03, f"三个读未并发（开始时间跨度 {spread:.3f}s；总耗时 {elapsed:.3f}s）"


@pytest.mark.asyncio
async def test_tool_result_events_keep_original_order(monkeypatch: pytest.MonkeyPatch):
    """事件保序：并发执行完仍按 `tool_calls` 原始顺序产出 `tool_result`。"""
    set_tool_context(session_id="s-c3-order")

    async def uneven_readonly(
        self: AgentRuntime, call: dict[str, Any], task: AgentTask
    ) -> dict[str, Any]:
        # 故意让第一个最慢：若实现是"谁先完成谁先报"，顺序就会乱
        delays = {"c1": 0.06, "c2": 0.02, "c3": 0.0}
        await asyncio.sleep(delays.get(call["id"], 0.0))
        return {"id": call["id"]}

    monkeypatch.setattr(AgentRuntime, "_execute_readonly_call", uneven_readonly)

    runtime, _ = _runtime_with_calls(
        [
            ToolCall(id="c1", name="read_file", arguments='{"path": "a.txt"}'),
            ToolCall(id="c2", name="read_file", arguments='{"path": "b.txt"}'),
            ToolCall(id="c3", name="read_file", arguments='{"path": "c.txt"}'),
        ]
    )
    events = [event async for event in runtime.run(_task("s-c3-order"))]

    result_ids = [
        event.tool_call_id for event in events if event.type == "tool_result"
    ]
    assert result_ids == ["c1", "c2", "c3"], f"事件顺序被打乱：{result_ids}"


@pytest.mark.asyncio
async def test_single_tool_failure_does_not_sink_the_batch(monkeypatch: pytest.MonkeyPatch):
    """单条失败转结构化错误 —— 同批其它调用已完成的结果不能跟着丢。"""
    set_tool_context(session_id="s-c3-failure")

    async def flaky_readonly(
        self: AgentRuntime, call: dict[str, Any], task: AgentTask
    ) -> dict[str, Any]:
        if call["id"] == "c2":
            raise RuntimeError("boom")
        return {"id": call["id"]}

    monkeypatch.setattr(AgentRuntime, "_execute_readonly_call", flaky_readonly)

    runtime, _ = _runtime_with_calls(
        [
            ToolCall(id="c1", name="read_file", arguments='{"path": "a.txt"}'),
            ToolCall(id="c2", name="read_file", arguments='{"path": "b.txt"}'),
            ToolCall(id="c3", name="read_file", arguments='{"path": "c.txt"}'),
        ]
    )
    events = [event async for event in runtime.run(_task("s-c3-failure"))]

    assert events[-1].type == "done", "单条失败不该中断整轮"
    results = {event.tool_call_id: event.content for event in events if event.type == "tool_result"}
    assert "boom" in (results.get("c2") or ""), "失败应转成结构化错误回灌"
    assert results.get("c1") and results.get("c3"), "同批其它结果不能丢"


@pytest.mark.asyncio
async def test_concurrency_limit_one_is_fully_serial(monkeypatch: pytest.MonkeyPatch):
    """`TOOL_CONCURRENCY_LIMIT=1` 等价于完全串行 —— 这是回滚开关。"""
    set_tool_context(session_id="s-c3-limit")
    from app.config import settings

    monkeypatch.setattr(settings, "tool_concurrency_limit", 1)

    started: list[float] = []

    async def counted_readonly(
        self: AgentRuntime, call: dict[str, Any], task: AgentTask
    ) -> dict[str, Any]:
        started.append(time.monotonic())
        await asyncio.sleep(0.03)
        return {"id": call["id"]}

    monkeypatch.setattr(AgentRuntime, "_execute_readonly_call", counted_readonly)

    runtime, _ = _runtime_with_calls(
        [
            ToolCall(id="c1", name="read_file", arguments='{"path": "a.txt"}'),
            ToolCall(id="c2", name="read_file", arguments='{"path": "b.txt"}'),
        ]
    )
    _ = [event async for event in runtime.run(_task("s-c3-limit"))]

    # limit=1 时两次调用必须被搜拉开（串行），而不是几乎同时开始
    assert len(started) == 2
    spread = max(started) - min(started)
    assert spread >= 0.025, f"limit=1 时应完全串行（开始时间跨度 {spread:.3f}s）"
