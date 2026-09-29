"""S6a 回归：`write_todos` 计划状态。

三件事：

1. **校验**（空列表 / 超上限 / 非法状态 / 缺 content）—— 计划是模型写的，坏输入必须挡住，
   否则前端会拿到一份渲染不出来的计划；
2. **状态可读回**，且**不堆积进消息历史正文**（那是 token 浪费）；
3. **同一批内多次调用只执行一次** —— 并发写计划只会互相覆盖（对位 deepagents
   的 `TodoListMiddleware` 约束）。
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.agent.runtime import AgentRuntime, AgentTask
from app.gateway.provider import ChatResponse, ToolCall
from app.tools.context import get_tool_context, set_tool_context
from app.tools.todo import MAX_TODOS, WRITE_TODOS_TOOL, current_todos, write_todos


@pytest.fixture(autouse=True)
def _clean_tool_context():
    """清空工具上下文 —— plan 存在 contextvars 里，会跨用例残留。"""
    yield
    from app.tools import context as tool_context

    tool_context._current_context.set(None)


# ── 工具本身 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_todos_normalizes_and_stores():
    set_tool_context(session_id="s-todo")
    out = await write_todos(
        [
            {"content": "读代码", "status": "completed"},
            {"content": "改一处"},
        ]
    )
    assert out["count"] == 2
    assert out["summary"] == {"completed": 1, "in_progress": 0, "pending": 1}
    # 缺省 status 归为 pending；id 自动补
    assert out["todos"][1]["status"] == "pending"
    assert out["todos"][1]["id"] == "t2"
    # 可读回（runtime 与前端都靠它）
    assert [t["content"] for t in current_todos()] == ["读代码", "改一处"]


@pytest.mark.asyncio
async def test_write_todos_rejects_bad_input():
    set_tool_context(session_id="s-todo-bad")

    assert "error" in await write_todos([])
    assert "error" in await write_todos([{"content": "   "}])
    assert "error" in await write_todos([{"content": "x", "status": "done"}])
    assert "error" in await write_todos("not-a-list")  # type: ignore[arg-type]
    assert "error" in await write_todos(
        [{"content": f"t{i}"} for i in range(MAX_TODOS + 1)]
    )


@pytest.mark.asyncio
async def test_write_todos_renders_compact_text():
    """tool_result 里要有可读文本 —— 模型靠它看到自己的计划（而不是每轮重喂一份）。"""
    set_tool_context(session_id="s-todo-render")
    out = await write_todos(
        [{"content": "a", "status": "completed"}, {"content": "b", "status": "in_progress"}]
    )
    assert "[x] a" in out["output"]
    assert "[~] b" in out["output"]


# ── runtime 接线 ──────────────────────────────────────────────────────────


def _runtime_with_calls(calls: list[ToolCall]) -> AgentRuntime:
    gateway = MagicMock()
    first = True

    async def fake_stream(**kwargs: Any):
        nonlocal first
        if first:
            first = False
            yield ChatResponse(content="", finish_reason="tool_calls", tool_calls=list(calls))
        else:
            yield ChatResponse(content="done", finish_reason="stop")

    gateway.chat_stream = fake_stream
    return AgentRuntime(gateway=gateway)


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
async def test_runtime_emits_todo_updated_event():
    set_tool_context(session_id="s-todo-event")
    runtime = _runtime_with_calls(
        [
            ToolCall(
                id="c1",
                name=WRITE_TODOS_TOOL,
                arguments='{"todos": [{"content": "step one", "status": "pending"}]}',
            )
        ]
    )
    events = [event async for event in runtime.run(_task("s-todo-event"))]

    todo_events = [event for event in events if event.type == "todo_updated"]
    assert todo_events, "计划更新必须产出 todo_updated 事件（前端据此渲染进度）"
    assert "step one" in (todo_events[0].content or "")


@pytest.mark.asyncio
async def test_duplicate_plan_writes_in_one_batch_execute_once():
    """同批内多次 `write_todos`：只执行第一次，其余明确报错（不静默覆盖）。"""
    set_tool_context(session_id="s-todo-dup")
    runtime = _runtime_with_calls(
        [
            ToolCall(
                id="c1",
                name=WRITE_TODOS_TOOL,
                arguments='{"todos": [{"content": "first plan"}]}',
            ),
            ToolCall(
                id="c2",
                name=WRITE_TODOS_TOOL,
                arguments='{"todos": [{"content": "second plan"}]}',
            ),
        ]
    )
    events = [event async for event in runtime.run(_task("s-todo-dup"))]

    results = {
        event.tool_call_id: (event.content or "")
        for event in events
        if event.type == "tool_result"
    }
    assert "first plan" in results.get("c1", ""), "第一次应正常写入"
    assert "只允许调用一次" in results.get("c2", ""), "重复的写入应被明确拒绝"

    # 只有第一次的计划生效（第二次没有覆盖它）
    assert [t["content"] for t in current_todos()] == ["first plan"]


@pytest.mark.asyncio
async def test_plan_state_is_not_in_message_history():
    """计划**不堆积进消息历史**：模型通过 tool_result 回显看到它，而不是每轮重喂一份。"""
    set_tool_context(session_id="s-todo-history")
    runtime = _runtime_with_calls(
        [
            ToolCall(
                id="c1",
                name=WRITE_TODOS_TOOL,
                arguments='{"todos": [{"content": "unique-marker-xyz"}]}',
            )
        ]
    )
    captured: list[dict] = []
    original = runtime._gateway.chat_stream

    async def capturing_stream(**kwargs: Any):
        captured.append(kwargs)
        async for chunk in original(**kwargs):
            yield chunk

    runtime._gateway.chat_stream = capturing_stream
    _ = [event async for event in runtime.run(_task("s-todo-history"))]

    assert captured, "应有第二次模型调用"
    system_texts = [
        m.content for m in captured[0].get("messages", []) if getattr(m, "role", "") == "system"
    ]
    assert not any("unique-marker-xyz" in (t or "") for t in system_texts), (
        "计划不该被塞进 system prompt 正文"
    )
    assert get_tool_context("todos", []), "但状态本身必须留在上下文里（供前端与协同使用）"
