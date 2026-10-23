"""C1 批 2 回归：run 现场 checkpoint 的**形状**与**落盘语义**。

批 2 只写不读，因此这里验证的是：
1. 快照形状符合三条约定（尾部窗口 + 下限、done_tools、TTL 两档判定）；
2. 落盘**失败不改变对话行为**（只降级恢复粒度）；
3. 状态迁移的活跃集合与唯一索引谓词一致。

真实 SQL 由 `integration` 用例覆盖（需要 PG）；这里用替身池验证**调用契约**。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from app.agent import checkpoint as cp


class _FakePool:
    """替身池：记录调用并模拟 asyncpg 的 `UPDATE n` 状态串。"""

    def __init__(self, *, update_rowcount: int = 1, raise_on: str = "") -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.update_rowcount = update_rowcount
        self.raise_on = raise_on

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError("db down")
        if sql.strip().startswith("UPDATE"):
            return f"UPDATE {self.update_rowcount}"
        return "INSERT 0 1"

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((sql, args))
        return {
            "checkpoint": json.dumps({"turn_index": 2, "done_tools": ["c1"]}),
            "checkpoint_at": None,
            "status": cp.STATUS_CHECKPOINTED,
        }


@pytest.fixture
def fake_pool(monkeypatch: pytest.MonkeyPatch) -> _FakePool:
    pool = _FakePool()
    monkeypatch.setattr(cp, "_pool", lambda: pool)
    return pool


# ── 快照形状 ──────────────────────────────────────────────────────────────


def test_snapshot_keeps_tail_window():
    messages = [{"role": "user", "content": f"m{i}"} for i in range(200)]
    snapshot = cp.build_snapshot(messages=messages, turn_index=3, done_tools=["c1"])

    assert snapshot["turn_index"] == 3
    assert snapshot["done_tools"] == ["c1"]
    # 尾部窗口：最后一条必须在，且不是整段
    assert snapshot["messages"][-1]["content"] == "m199"
    assert len(snapshot["messages"]) < len(messages)


def test_merge_turn_ids_dedupes_and_keeps_order():
    """M19：一条 run 链的网关回合集合 —— 去重、保序、空值忽略。"""
    merged, truncated = cp.merge_turn_ids(["t1", "t2", "t1", ""], "t3")

    assert merged == ["t1", "t2", "t3"]
    assert truncated is False


def test_merge_turn_ids_truncation_is_sticky():
    """超上限只保留最近 N 条；截断标记一旦置上就不会被后续合并洗掉。

    对账依赖这份集合是**完整的**：少一条历史 turn 就会把已计费的回合再补一遍。
    """
    existing = [f"t{i}" for i in range(cp.MAX_TRACKED_TURN_IDS + 5)]

    merged, truncated = cp.merge_turn_ids(existing, "t-new", already_truncated=True)
    again, still_truncated = cp.merge_turn_ids(merged, "t-next", already_truncated=truncated)

    assert len(merged) == cp.MAX_TRACKED_TURN_IDS
    assert merged[-1] == "t-new", "保最近的回合（更早的可能已被 turns 保留期清掉）"
    assert truncated is True
    assert still_truncated is True


def test_merge_turn_ids_keeps_truncation_flag_without_new_overflow():
    """已截断的集合再合并**不会**变回完整（哪怕这次没有新溢出）。

    这条独立于上一条：上一条靠"新合并又超上限"顺带把标记置上，掩盖了
    `already_truncated` 是否真的被保留。
    """
    merged, truncated = cp.merge_turn_ids(["t1"], "t2", already_truncated=True)

    assert merged == ["t1", "t2"]
    assert truncated is True


def test_snapshot_carries_turn_ids():
    snapshot = cp.build_snapshot(
        messages=[{"role": "user", "content": "hi"}],
        turn_index=1,
        turn_id="turn-1",
        turn_ids=["turn-0", "turn-1"],
    )

    assert snapshot["turn_id"] == "turn-1"
    assert snapshot["turn_ids"] == ["turn-0", "turn-1"]
    assert snapshot["turn_ids_truncated"] is False


def test_snapshot_respects_min_tail_messages():
    """下限保证最近若干条完整保留 —— 否则 `assistant(tool_calls)`/`tool` 配对待断在快照边上。"""
    messages = [
        {"role": "assistant", "content": "x" * 200_000, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "content": "r", "tool_call_id": "c1"},
        {"role": "assistant", "content": "done"},
    ]
    snapshot = cp.build_snapshot(messages=messages, turn_index=1, min_tail_messages=2)

    contents = [m.get("content") for m in snapshot["messages"]]
    assert "done" in contents, "最近一条必须保留"
    assert any(m.get("tool_call_id") == "c1" for m in snapshot["messages"]), (
        "配对中的 tool 结果不能被切掉"
    )


def test_resume_window_three_tiers():
    import time

    def at(seconds_ago: float) -> dict[str, Any]:
        stamp = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago)
        )
        return {"saved_at": stamp}

    assert cp.resume_window(at(60)) == "hot"
    assert cp.resume_window(at(cp.HOT_RESUME_WINDOW_SECONDS + 60)) == "cold"
    assert cp.resume_window(at(cp.COLD_RESUME_WINDOW_SECONDS + 60)) == "abandoned"
    # 缺失/非法时间戳 → 保守按 cold（不承诺事件补齐）
    assert cp.resume_window({}) == "cold"
    assert cp.resume_window({"saved_at": "not-a-timestamp"}) == "cold"


# ── 落盘语义 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_updates_active_row(fake_pool: _FakePool):
    ok = await cp.save(
        tenant_id=1,
        session_id="s1",
        checkpoint=cp.build_snapshot(messages=[{"role": "user", "content": "hi"}], turn_index=1),
    )
    assert ok is True
    assert len(fake_pool.calls) == 1
    sql, args = fake_pool.calls[0]
    assert sql.strip().startswith("UPDATE")
    assert args[0] == "s1" and args[1] == 1


@pytest.mark.asyncio
async def test_save_inserts_when_no_active_row(monkeypatch: pytest.MonkeyPatch):
    pool = _FakePool(update_rowcount=0)
    monkeypatch.setattr(cp, "_pool", lambda: pool)

    ok = await cp.save(
        tenant_id=1,
        session_id="s1",
        checkpoint=cp.build_snapshot(messages=[{"role": "user", "content": "hi"}], turn_index=1),
    )
    assert ok is True
    assert len(pool.calls) == 2, "UPDATE 未命中后应回落到 INSERT"
    assert pool.calls[1][0].strip().startswith("INSERT")


@pytest.mark.asyncio
async def test_save_failure_does_not_raise(fake_pool: _FakePool, monkeypatch: pytest.MonkeyPatch):
    """落盘失败**只降级恢复粒度**，绝不影响对话本身（与 workflow 的同一语义）。"""
    monkeypatch.setattr(cp, "_pool", lambda: _FakePool(raise_on="UPDATE"))

    ok = await cp.save(
        tenant_id=1,
        session_id="s1",
        checkpoint=cp.build_snapshot(messages=[{"role": "user", "content": "hi"}], turn_index=1),
    )
    assert ok is False


@pytest.mark.asyncio
async def test_save_skips_without_session_id(fake_pool: _FakePool):
    assert await cp.save(tenant_id=1, session_id="", checkpoint={}) is False
    assert fake_pool.calls == []


@pytest.mark.asyncio
async def test_load_parses_checkpoint(fake_pool: _FakePool):
    loaded = await cp.load(tenant_id=1, session_id="s1")
    assert loaded is not None
    assert loaded["turn_index"] == 2
    assert loaded["done_tools"] == ["c1"]
    sql, _ = fake_pool.calls[0]
    assert "status IN ('running', 'checkpointed', 'resuming')" in sql, (
        "A4：恢复路径只认活跃行 —— 终态（completed/failed/cancelled/abandoned）的"
        "陈旧快照不得被下一次提交捡走续跑"
    )


@pytest.mark.asyncio
async def test_mark_with_run_token_adds_ownership_guard(fake_pool: _FakePool):
    """A4 防踩踏：带 run_token 的终态写入必须校验归属，避免旧 run 标掉新 run 的行。"""
    ok = await cp.mark(
        tenant_id=1, session_id="s1", status=cp.STATUS_COMPLETED, run_token="rt-1"
    )
    assert ok is True
    sql, args = fake_pool.calls[0]
    assert "AND ($4 = '' OR run_token = $4)" in sql
    assert args[3] == "rt-1"


@pytest.mark.asyncio
async def test_mark_without_run_token_keeps_legacy_semantics(fake_pool: _FakePool):
    """reconciler/abandoned 等旁观者路径不带 token：条件应短路放行（$4=''）。"""
    ok = await cp.mark(tenant_id=1, session_id="s1", status=cp.STATUS_ABANDONED)
    assert ok is True
    _, args = fake_pool.calls[0]
    assert args[3] == ""


@pytest.mark.asyncio
async def test_mark_moves_row_out_of_active_set(fake_pool: _FakePool):
    ok = await cp.mark(tenant_id=1, session_id="s1", status=cp.STATUS_COMPLETED)
    assert ok is True
    sql, args = fake_pool.calls[0]
    assert "status IN ('running', 'checkpointed', 'resuming')" in sql, (
        "状态迁移必须只作用于活跃行，否则会改到历史行"
    )
    assert args[2] == cp.STATUS_COMPLETED


def test_active_statuses_match_index_predicate():
    """活跃集合必须与唯一索引 `ux_agent_runs_session_active` 的谓词一致。"""
    assert set(cp.ACTIVE_STATUSES) == {"running", "checkpointed", "resuming"}


# ── runtime 接线 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_runtime_writes_checkpoint_at_turn_end(monkeypatch: pytest.MonkeyPatch):
    """回合末（有工具调用的分支）必须落一次 checkpoint。"""
    from unittest.mock import MagicMock

    from app.agent.runtime import AgentRuntime, AgentTask
    from app.gateway.provider import ChatResponse, ToolCall
    from app.tools.context import set_tool_context

    saved: list[dict[str, Any]] = []

    async def fake_save(**kwargs: Any) -> bool:
        saved.append(kwargs)
        return True

    monkeypatch.setattr(cp, "save", fake_save)
    set_tool_context(session_id="s-cp")

    gateway = MagicMock()
    first = True

    async def fake_stream(**kwargs: Any):
        nonlocal first
        if first:
            first = False
            # 一轮里发一个工具调用，逼出"有工具调用的分支"。
            # 用真实 `ToolCall`（而不是 MagicMock）：runtime 会把工具名与参数序列化进
            # side-effect 账本与 trace，MagicMock 在那一步就会炸。
            yield ChatResponse(
                content="",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(id="c1", name="read_file", arguments='{"path": "nope.txt"}')
                ],
            )
        else:
            yield ChatResponse(content="done", finish_reason="stop")

    gateway.chat_stream = fake_stream

    runtime = AgentRuntime(gateway=gateway)
    task = AgentTask(
        id="t",
        tenant_id=1,
        user_id="u1",
        session_id="s-cp",
        content="hi",
        turn_id="turn-1",
        system_prompt="sp",
        llm_config={"mode": "normal", "tools_mode": "yolo"},
        max_turns=3,
    )
    _ = [event async for event in runtime.run(task)]

    assert saved, "回合末未落 checkpoint"
    assert saved[0]["session_id"] == "s-cp"
    assert saved[0]["checkpoint"]["turn_index"] >= 1
    # M19：run 与网关回合的对应关系必须随快照落盘，否则对账无从知道哪些回合已计费
    assert saved[0]["checkpoint"]["turn_id"] == "turn-1"
    assert saved[0]["checkpoint"]["turn_ids"] == ["turn-1"]
    assert saved[0]["checkpoint"]["turn_ids_truncated"] is False


@pytest.mark.asyncio
async def test_runtime_merges_inherited_turn_ids(monkeypatch: pytest.MonkeyPatch):
    """续跑段必须把上一段的网关回合带下来。

    只写当前 turn = 丢掉历史集合 ⇒ 网关对账把已计费的回合再补一遍（多收）。
    """
    from unittest.mock import MagicMock

    from app.agent.resume import ResumeState
    from app.agent.runtime import AgentRuntime, AgentTask
    from app.tools.context import set_tool_context

    captured: dict[str, Any] = {}

    def fake_build(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {}

    async def fake_save(**kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(cp, "build_snapshot", fake_build)
    monkeypatch.setattr(cp, "save", fake_save)
    set_tool_context(turn_id="turn-1")

    runtime = AgentRuntime(gateway=MagicMock())
    runtime._resumed_from_checkpoint = ResumeState(turn_ids=("turn-0",))
    task = AgentTask(id="t", tenant_id=1, user_id="u1", session_id="s1", content="hi")
    await runtime._save_checkpoint(
        task, [{"role": "user", "content": "hi"}], turn_index=1, done_tools=[], usage={}
    )

    assert captured["turn_ids"] == ["turn-0", "turn-1"]
    assert captured["turn_ids_truncated"] is False
