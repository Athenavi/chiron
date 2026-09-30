"""C2：记忆注入形态 —— 独立 block + 前缀缓存断点（方案 02 §4）。

五条约定：

* 记忆**不**拼进 `system_prompt`（那是逐字稳定的提示词前缀，前缀缓存靠它命中）；
* 记忆是一条**独立的 system 消息**，位置在稳定前缀之后（与 deepagents 的
  `append_to_system_message` 对位）；
* 缓存断点标在**稳定前缀的末尾**（不是记忆块上）—— 这样"记忆变化"不击穿前缀缓存；
* system 段的定形只发生在 `_apply_system_prefix` **一处**，因此对 session cache 分支同样生效；
* session cache **不含**记忆：那是每轮按 query 重新召回的临时上下文，存下来会冒充历史。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.runtime import (
    _MEMORY_TRUST_HEADER,
    AgentRuntime,
    AgentTask,
    _apply_system_prefix,
)
from app.gateway.provider import ChatMessage, ChatResponse
from app.gateway.router import normalize_messages

SYSTEM = "SYS PROMPT 逐字稳定"


def _task(*, system: str = SYSTEM, memory: str = "", history: list[Any] | None = None) -> AgentTask:
    return AgentTask(
        id="t1",
        tenant_id="t",
        user_id="u",
        session_id="s1",
        content="hi",
        system_prompt=system,
        memory_context=memory,
        history=history or [],
        max_turns=2,
    )


def _shape(task: AgentTask) -> list[dict[str, Any]]:
    """把消息列表按 C2 的规则定形（含一条已有 user 消息）。"""
    return _apply_system_prefix([{"role": "user", "content": "hi"}], task)


# ── 组装形态 ────────────────────────────────────────────────────────────


def test_memory_is_a_separate_system_message():
    msgs = _shape(_task(memory="记忆正文 MEM"))

    assert [m["role"] for m in msgs] == ["system", "system", "user"]
    assert msgs[0]["content"] == SYSTEM, "记忆**不许**混进稳定前缀"
    assert "MEM" in msgs[1]["content"], "记忆应自成一条消息"


def test_prefix_is_identical_when_memory_changes():
    """C2 的验收口径：记忆变化**不**击穿 system 前缀 —— 两轮请求的第一条消息逐字相同。"""
    first = _shape(_task(memory="记忆 A"))
    second = _shape(_task(memory="截然不同的记忆 B"))

    assert first[0] == second[0], "前缀一变，整段提示词缓存就白攒了"


def test_breakpoint_marker_sits_on_the_prefix():
    msgs = _shape(_task(memory="MEM"))

    assert msgs[0].get("cache_breakpoint") is True, "断点应在**稳定前缀**的末尾"
    assert "cache_breakpoint" not in msgs[1], "记忆块每轮都可能变，不该是断点"


def test_no_memory_keeps_previous_shape():
    msgs = _shape(_task(memory=""))

    assert [m["role"] for m in msgs] == ["system", "user"], "没记忆时不应多出一条消息"


def test_stale_system_messages_are_replaced():
    """从 session cache 取回的消息里可能带着**旧** system 段 —— 必须被替换，否则会重复。"""
    stale = [
        {"role": "system", "content": "旧前缀"},
        {"role": "user", "content": "历史"},
    ]

    msgs = _apply_system_prefix(stale, _task(memory="MEM"))

    systems = [m["content"] for m in msgs if m["role"] == "system"]
    assert systems == [SYSTEM, "MEM"], "旧 system 应被摘掉，只留定形后的前缀与记忆"
    assert [m["role"] for m in msgs if m["role"] != "system"] == ["user"]


def test_history_snapshot_excludes_memory():
    msgs = AgentRuntime(gateway=MagicMock())._build_history_msgs(_task(memory="记忆正文 MEM"))

    assert all("MEM" not in str(m.get("content", "")) for m in msgs)


# ── provider 边界 ───────────────────────────────────────────────────────


def test_breakpoint_reaches_the_provider_message():
    [normalized] = normalize_messages(
        [{"role": "system", "content": "P", "cache_breakpoint": True}]
    )

    assert normalized.cache_breakpoint is True, "标了要能传到 provider 边界，否则等于没标"


def test_breakpoint_is_not_sent_on_the_wire():
    """标记是**引擎内部**元信息 —— 发给不认识它的 API 会直接 400。"""
    payload = ChatMessage(role="system", content="P", cache_breakpoint=True).to_dict()

    assert "cache_breakpoint" not in payload


# ── 端到端 ──────────────────────────────────────────────────────────────


def _fake_memory(profile: str = "用户偏好中文回答") -> MagicMock:
    memory = MagicMock()
    session_ctx = MagicMock()
    session_ctx.profile_cached = False
    session_ctx.summaries = 0
    memory.on_session_start = AsyncMock(return_value=session_ctx)
    recalled = MagicMock()
    recalled.has_content = True
    recalled.profile_block = profile
    recalled.summary_items = []
    memory.recall = AsyncMock(return_value=recalled)
    memory.on_turn_complete = AsyncMock()
    memory.on_session_end = AsyncMock()
    return memory


async def _run_and_capture(**runtime_kwargs: Any) -> tuple[list[dict[str, Any]], AgentTask]:
    calls: list[dict[str, Any]] = []
    gateway = MagicMock()

    async def fake_stream(**kwargs: Any):
        calls.append(kwargs)
        yield ChatResponse(content="ok", finish_reason="stop")

    gateway.chat_stream = fake_stream

    runtime = AgentRuntime(gateway=gateway, **runtime_kwargs)
    task = _task()
    _ = [event async for event in runtime.run(task)]
    return calls, task


@pytest.mark.asyncio
async def test_recall_lands_in_memory_context_not_system_prompt():
    """端到端（无 session cache）：召回结果进 `memory_context`，**不**污染 system 前缀。"""
    calls, task = await _run_and_capture(memory=_fake_memory())

    assert task.memory_context, "召回结果应写进 memory_context"
    assert _MEMORY_TRUST_HEADER in task.memory_context, "A2 的信任声明不能丢"
    system_msgs = [m.content for m in calls[0]["messages"] if m.role == "system"]
    # 前缀还会带上技能目录（D2），故用 startswith 定位"前缀"、用信任声明定位"记忆"
    assert system_msgs[0].startswith(SYSTEM), "system 前缀必须逐字未被污染"
    assert any(_MEMORY_TRUST_HEADER in t for t in system_msgs[1:]), (
        "记忆应作为独立 system 消息出现"
    )


@pytest.mark.asyncio
async def test_memory_survives_the_session_cache_path():
    """**回归**：走 session cache 的分支（生产路径）曾把记忆整段丢掉。

    那条分支的消息来自 cache，不经过 `_build_messages` —— 记忆若只在 `_build_messages` 里插，
    带 `session_store` 的真实运行就永远看不到记忆。
    """
    from app.session_store import SessionStore

    calls, _ = await _run_and_capture(
        memory=_fake_memory(), session_store=SessionStore(max_sessions=5)
    )

    system_msgs = [m.content for m in calls[0]["messages"] if m.role == "system"]
    assert len(system_msgs) == 2, "session cache 路径也必须带上记忆"
    assert _MEMORY_TRUST_HEADER in system_msgs[1]
