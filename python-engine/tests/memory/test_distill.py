"""C6：回合级 L2 自动提炼（默认关 · 低置信入库 · 用量计入预算）。

三组断言：

* **解析**：模型输出必须容错解析（围栏、前后解释文字），但**校验不放宽**（未知 slot、空 key/value
  一律丢弃）—— 宁可少记，不可乱记；
* **入库**：候选一律低置信 + `derived`，且**已存在同名 key 就跳过**（不登记冲突）；
* **预算**：开启时，提炼用掉的 token 要并进 run 的累计值 —— 否则 `task_budget` 的 tokens 轴
  统计失真（与 S6b 的 grader 同一条规矩）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.gateway.provider import ChatResponse
from app.memory.distill import (
    MAX_CANDIDATE_CONFIDENCE,
    parse_candidates,
    render_transcript,
)
from app.memory.service import MemoryService
from tests.fakes import InMemoryProfileStore

VALID = '[{"slot":"preference","key":"lang","value":"中文","confidence":80}]'


# ── 解析 ────────────────────────────────────────────────────────────────


def test_parses_plain_json_array():
    [item] = parse_candidates(VALID)

    assert (item.slot, item.key, item.value) == ("preference", "lang", "中文")


def test_parses_fenced_output_with_commentary():
    raw = f"Sure! Here you go:\n```json\n{VALID}\n```\nHope it helps."

    [item] = parse_candidates(raw)

    assert item.key == "lang"


def test_confidence_is_capped_at_candidate_ceiling():
    """自动提炼的条目**永远**低于用户显式确认 —— 上限就是这条规矩的实现。"""
    [item] = parse_candidates(VALID)

    assert item.confidence == MAX_CANDIDATE_CONFIDENCE


def test_unknown_slot_or_empty_fields_are_dropped():
    raw = (
        '[{"slot":"emotion","key":"k","value":"v","confidence":50},'
        '{"slot":"fact","key":"","value":"v"},'
        '{"slot":"fact","key":"k","value":"   "},'
        '{"slot":"fact","key":"ok","value":"v"}]'
    )

    remaining = parse_candidates(raw)

    assert [c.key for c in remaining] == ["ok"], "校验不放宽：宁少不乱"


def test_non_json_output_returns_empty():
    assert parse_candidates("no json at all") == []
    assert parse_candidates("{}") == []
    assert parse_candidates("[not json") == []


def test_max_items_is_respected():
    raw = "[" + ",".join(
        f'{{"slot":"fact","key":"k{i}","value":"v{i}"}}' for i in range(10)
    ) + "]"

    assert len(parse_candidates(raw, max_items=2)) == 2


def test_render_transcript_keeps_only_user_and_assistant():
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "我喜欢中文"},
        {"role": "tool", "content": "tool output"},
        {"role": "assistant", "content": "好的"},
    ]

    text = render_transcript(messages)

    assert "我喜欢中文" in text and "好的" in text
    assert "SYS" not in text and "tool output" not in text


# ── 入库 ────────────────────────────────────────────────────────────────


async def test_ingest_stores_low_confidence_derived_entries():
    from app.memory.distill import Candidate

    svc = MemoryService(store=InMemoryProfileStore())

    stats = await svc.ingest_candidates(
        "t", "u", [Candidate(slot="preference", key="lang", value="中文", confidence=90)]
    )

    assert stats["added"] == 1
    [entry] = (await svc.list_entries("t", "u"))["entries"]
    assert entry["confidence"] == MAX_CANDIDATE_CONFIDENCE, "候选置信度必须封顶"
    assert entry["source"] == "derived", "自动条目要能被整理优先淘汰"


async def test_ingest_skips_existing_key_without_conflict():
    from app.memory.distill import Candidate

    svc = MemoryService(store=InMemoryProfileStore())
    await svc.upsert("t", "u", slot="preference", key="lang", value="中文")

    stats = await svc.ingest_candidates(
        "t", "u", [Candidate(slot="preference", key="lang", value="English", confidence=10)]
    )

    assert stats == {"added": 0, "skipped_existing": 1, "skipped_invalid": 0}
    [entry] = (await svc.list_entries("t", "u"))["entries"]
    assert entry["value"] == "中文", "候选不该覆盖已有值"


async def test_ingest_is_project_scoped():
    from app.memory.distill import Candidate

    svc = MemoryService(store=InMemoryProfileStore())

    await svc.ingest_candidates(
        "t", "u", [Candidate(slot="fact", key="k", value="A", confidence=10)], project="proj-a"
    )
    await svc.ingest_candidates(
        "t", "u", [Candidate(slot="fact", key="k", value="B", confidence=10)], project="proj-b"
    )

    assert (await svc.list_entries("t", "u", project="proj-a"))["total"] == 1
    assert (await svc.list_entries("t", "u", project="proj-b"))["total"] == 1


# ── 端到端：用量计入预算 ────────────────────────────────────────────────


def _fake_memory(seen: dict[str, Any]) -> MagicMock:
    memory = MagicMock()
    session_ctx = MagicMock()
    session_ctx.profile_cached = False
    session_ctx.summaries = 0
    memory.on_session_start = AsyncMock(return_value=session_ctx)
    recalled = MagicMock()
    recalled.has_content = False
    recalled.profile_block = ""
    recalled.summary_items = []
    memory.recall = AsyncMock(return_value=recalled)

    async def _turn_complete(**kwargs: Any) -> None:
        seen["tokens_in"] = kwargs.get("tokens_in")
        seen["tokens_out"] = kwargs.get("tokens_out")
        seen["project"] = kwargs.get("project")

    memory.on_turn_complete = _turn_complete
    memory.ingest_candidates = AsyncMock(return_value={"added": 1})
    memory.on_session_end = AsyncMock()
    return memory


async def _run_turn(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> dict[str, Any]:
    from app.agent.runtime import AgentRuntime, AgentTask

    monkeypatch.setattr(settings, "memory_distill_enabled", enabled)
    seen: dict[str, Any] = {}
    gateway = MagicMock()

    async def fake_stream(**kwargs: Any):
        yield ChatResponse(content="好的", finish_reason="stop")

    async def fake_chat(**kwargs: Any) -> ChatResponse:
        return ChatResponse(content=VALID, input_tokens=100, output_tokens=50)

    gateway.chat_stream = fake_stream
    gateway.chat = fake_chat

    runtime = AgentRuntime(gateway=gateway, memory=_fake_memory(seen))
    task = AgentTask(
        id="t1",
        tenant_id="t",
        user_id="u",
        session_id="s1",
        content="我喜欢中文",
        system_prompt="base",
        llm_config={"mode": "normal"},
        max_turns=1,
    )
    _ = [event async for event in runtime.run(task)]
    return seen


async def test_distill_usage_counts_toward_the_run(monkeypatch):
    """提炼消耗的 token 必须并进 run 的累计值（否则 task_budget 的 tokens 轴统计失真）。"""
    seen = await _run_turn(monkeypatch, enabled=True)

    assert seen["tokens_in"] == 100, "假模型只产出这一次 usage，它必须出现在累计值里"
    assert seen["tokens_out"] == 50


async def test_disabled_by_default_does_not_call_llm(monkeypatch):
    seen = await _run_turn(monkeypatch, enabled=False)

    assert seen["tokens_in"] == 0, "默认关时不该发生这次调用"
