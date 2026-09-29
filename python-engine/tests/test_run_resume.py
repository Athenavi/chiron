"""C1 批 3+：run 恢复路径与 reconciler（方案 01 §3.1）。

覆盖三件事：
* **入口恢复**：有可用快照 ⇒ 用快照做历史起点（已完成回合/工具不重放）；窗口超限/快照损坏
  ⇒ **回落现状**并把原因写清楚（不静默）；
* **`replay_pending`**：依据 `tool_calls` 两段式写入（`output = ''`）判定"已开始未结束"，
  在历史里只**告知**（占位），**不自动重放**（可能已产生副作用）；
* **reconciler**：接管不变量 = 运行锁**已释放**且快照超龄；锁还在 ⇒ 跳过（不抢活着的现场）；
  超冷窗口 ⇒ `abandoned`（可见）；并记指标。
"""

from __future__ import annotations

import time
from typing import Any

from app.agent.checkpoint import COLD_RESUME_WINDOW_SECONDS, HOT_RESUME_WINDOW_SECONDS
from app.agent.resume import (
    UNKNOWN_RESULT,
    load_resume_state,
    merge_user_message,
    sweep_once,
    with_replay_pending,
)


def _saved_at(seconds_ago: float = 0.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def _snapshot(
    *,
    seconds_ago: float = 0.0,
    turn_index: int = 2,
    done: list[str] | None = None,
    task_meta: bool = True,
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "messages": [
            {"role": "user", "content": "帮我改这个文件"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_a", "name": "x"}]},
            {"role": "tool", "tool_call_id": "call_a", "content": "ok"},
        ],
        "turn_index": turn_index,
        "done_tools": done if done is not None else ["call_a"],
        "saved_at": _saved_at(seconds_ago),
    }
    if task_meta:
        # 自动续跑的唯一来源：这些配置只存在于提交请求里
        snapshot["task_meta"] = {
            "user_id": "u1",
            "system_prompt": "sys",
            "llm_config": {"model": "m"},
            "workbench_context": {},
            "max_turns": 5,
            "subagent_depth": 0,
        }
    return snapshot


def _patch_checkpoint(
    monkeypatch: Any,
    *,
    snapshot: dict[str, Any] | None,
    pending: list[dict[str, Any]] | None = None,
    marks: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    recorded: list[tuple[str, str]] = marks if marks is not None else []

    async def _load(*, tenant_id: str, session_id: str):  # noqa: ANN202
        return snapshot

    async def _pending(*, session_id: str, limit: int = 50):  # noqa: ANN202
        return list(pending or [])

    async def _mark(*, tenant_id: str, session_id: str, status: str) -> bool:  # noqa: ANN202
        recorded.append((session_id, status))
        return True

    monkeypatch.setattr("app.agent.checkpoint.load", _load)
    monkeypatch.setattr("app.agent.checkpoint.pending_tool_calls", _pending)
    monkeypatch.setattr("app.agent.checkpoint.mark", _mark)
    return recorded


# ── 入口恢复 ────────────────────────────────────────────────────────────


async def test_uses_snapshot_as_history(monkeypatch):
    _patch_checkpoint(monkeypatch, snapshot=_snapshot())

    state = await load_resume_state(tenant_id="t1", session_id="s1")

    assert state is not None
    assert state.turn_index == 2
    assert state.window == "hot"
    assert len(state.messages) == 3, "快照消息就是历史起点"


async def test_pending_excludes_already_done_tools(monkeypatch):
    _patch_checkpoint(
        monkeypatch,
        snapshot=_snapshot(done=["call_a"]),
        pending=[
            {"id": "call_a", "tool_name": "x", "turn_id": "t"},
            {"id": "call_b", "tool_name": "y", "turn_id": "t"},
        ],
    )

    state = await load_resume_state(tenant_id="t1", session_id="s1")

    assert state is not None
    assert state.replay_pending == ("call_b",), "已完成的调用不该进待重放"


async def test_expired_window_marks_abandoned_and_falls_back(monkeypatch):
    marks = _patch_checkpoint(
        monkeypatch, snapshot=_snapshot(seconds_ago=COLD_RESUME_WINDOW_SECONDS + 60)
    )

    state = await load_resume_state(tenant_id="t1", session_id="s1")

    assert state is None, "超窗 ⇒ 回落现状语义（用户可整轮重试）"
    assert marks and marks[-1][1] == "abandoned", "超窗必须**可见**，不能静默"


async def test_empty_snapshot_falls_back(monkeypatch):
    _patch_checkpoint(monkeypatch, snapshot={"messages": [], "saved_at": _saved_at()})
    assert await load_resume_state(tenant_id="t1", session_id="s1") is None


async def test_missing_snapshot_returns_none(monkeypatch):
    _patch_checkpoint(monkeypatch, snapshot=None)
    assert await load_resume_state(tenant_id="t1", session_id="s1") is None


# ── 用户消息合并与 replay_pending 注入 ──────────────────────────────────


def test_merge_user_message_does_not_duplicate():
    messages = [{"role": "user", "content": "帮我改这个文件"}]
    assert merge_user_message(messages, "帮我改这个文件") is messages, "同内容不重复追加"
    assert len(merge_user_message(messages, "换个说法")) == 2


def test_with_replay_pending_adds_unknown_placeholder():
    messages = [{"role": "assistant", "content": "", "tool_calls": [{"id": "call_b", "name": "y"}]}]

    out = with_replay_pending(messages, ("call_b",))

    assert out[-1]["role"] == "tool"
    assert out[-1]["tool_call_id"] == "call_b"
    assert UNKNOWN_RESULT in out[-1]["content"]


def test_with_replay_pending_skips_when_result_present():
    messages = [{"role": "tool", "tool_call_id": "call_b", "content": "already there"}]
    assert with_replay_pending(messages, ("call_b",)) is messages


def test_with_replay_pending_noop_for_empty():
    messages: list[dict[str, Any]] = []
    assert with_replay_pending(messages, ()) is messages


# ── reconciler ──────────────────────────────────────────────────────────


class _FakePool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return self._rows


class _FakeRedis:
    def __init__(self, held: bool) -> None:
        self._held = held

    async def get(self, key: str) -> Any:
        return b"run-token" if self._held else None


def _patch_reconciler(
    monkeypatch: Any,
    *,
    rows: list[dict[str, Any]],
    lock_held: bool,
    claim_ok: bool = True,
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    """返回 `(marks, claims)`：状态改动与接管 CAS 各记一份，便于断言"走没走接管"。"""
    pool = _FakePool(rows)
    marks: list[tuple[str, str]] = []
    claims: list[tuple[str, str, str]] = []

    async def _mark(*, tenant_id: str, session_id: str, status: str) -> bool:  # noqa: ANN202
        marks.append((session_id, status))
        return True

    async def _claim(  # noqa: ANN202
        *, tenant_id: str, session_id: str, expected_run_token: str, new_run_token: str
    ) -> bool:
        claims.append((session_id, expected_run_token, new_run_token))
        return claim_ok

    async def _get_redis():  # noqa: ANN202
        return _FakeRedis(lock_held)

    monkeypatch.setattr("app.agent.checkpoint._pool", lambda: pool)
    monkeypatch.setattr("app.agent.checkpoint.mark", _mark)
    monkeypatch.setattr("app.agent.checkpoint.claim", _claim)
    monkeypatch.setattr("app.redis_client.get_redis", _get_redis)
    monkeypatch.setattr("app.agent.resume._metrics", lambda: None)
    return marks, claims


def _row(status: str, *, seconds_ago: float = 0.0, run_token: str = "tok_old") -> dict[str, Any]:
    return {
        "session_id": "s1",
        "tenant_id": "t1",
        "status": status,
        "checkpoint": _snapshot(seconds_ago=seconds_ago),
        "run_token": run_token,
    }


async def test_sweep_skips_when_run_lock_still_held(monkeypatch):
    """接管不变量的关键一半：锁还在 ⇒ 旧主还活着 ⇒ **不接管**。"""
    marks, _ = _patch_reconciler(monkeypatch, rows=[_row("running")], lock_held=True)

    stats = await sweep_once()

    assert stats["skipped_alive"] == 1
    assert marks == [], "不能抢活着的现场"


async def test_sweep_revives_running_when_owner_gone(monkeypatch):
    marks, _ = _patch_reconciler(monkeypatch, rows=[_row("running")], lock_held=False)

    stats = await sweep_once()

    assert stats["revived"] == 1
    assert marks == [("s1", "checkpointed")], "running → checkpointed（可续跑，等用户重试）"


async def test_sweep_abandons_expired_snapshot(monkeypatch):
    marks, _ = _patch_reconciler(
        monkeypatch,
        rows=[_row("checkpointed", seconds_ago=COLD_RESUME_WINDOW_SECONDS + 60)],
        lock_held=False,
    )

    stats = await sweep_once()

    assert stats["abandoned"] == 1
    assert marks == [("s1", "abandoned")]


async def test_sweep_without_pool_is_noop(monkeypatch):
    monkeypatch.setattr("app.agent.checkpoint._pool", lambda: None)
    stats = await sweep_once()
    assert stats["scanned"] == 0


async def test_sweep_failure_does_not_raise(monkeypatch):
    class _BoomPool:
        async def fetch(self, sql: str, *args: Any) -> Any:
            raise RuntimeError("db down")

    monkeypatch.setattr("app.agent.checkpoint._pool", lambda: _BoomPool())
    stats = await sweep_once()
    assert stats["scanned"] == 0  # 巡检失败只记账，不抛


def test_metrics_are_registered():
    """指标必须真的存在（否则接管/续跑在监控上完全不可见）。"""
    from app.observability import metrics

    assert metrics.RUN_RECONCILE_TOTAL is not None
    assert metrics.RUN_RESUMED_TOTAL is not None
    assert metrics.RUN_RESUME_TURNS_SAVED is not None


# ── 自动续跑（热窗口内接管并跑完） ───────────────────────────────────────


async def test_sweep_auto_resumes_within_hot_window(monkeypatch):
    """热窗口 + runner + CAS 成功 ⇒ **真的续跑**（方案 §3.1 的"自动接管"）。"""
    marks, claims = _patch_reconciler(monkeypatch, rows=[_row("running")], lock_held=False)
    ran: list[Any] = []

    async def runner(task: Any) -> None:
        ran.append(task)

    stats = await sweep_once(runner=runner)

    assert stats["resumed"] == 1
    assert len(ran) == 1, "runner 必须被调用（否则就只是标记了一下）"
    task = ran[0]
    assert task.session_id == "s1" and task.tenant_id == "t1"
    assert task.run_token, "续跑必须带新的 run_token（CAS 的结果，供后续归属判定）"
    assert task.llm_config == {"model": "m"}, "重建的 task 要带上原 llm_config"
    assert task.content == "", "用户消息已在快照历史里 ⇒ content 置空（不重复追加）"
    assert claims and claims[0][1] == "tok_old", "接管前必须用旧 token 做 CAS"
    assert marks == [], "接管成功路径不该再改状态（claim 已把它置为 resuming）"


async def test_sweep_does_not_auto_resume_in_cold_window(monkeypatch):
    """冷窗口里用户早走了 —— 不替他烧 token 跑完，只标记可续跑。"""
    marks, claims = _patch_reconciler(
        monkeypatch,
        rows=[_row("running", seconds_ago=HOT_RESUME_WINDOW_SECONDS + 60)],
        lock_held=False,
    )
    ran: list[Any] = []

    async def runner(task: Any) -> None:
        ran.append(task)

    stats = await sweep_once(runner=runner)

    assert stats["resumed"] == 0 and ran == [], "冷窗口不自动续跑"
    assert stats["revived"] == 1
    assert claims == [], "不接管就不该做 CAS"
    assert marks == [("s1", "checkpointed")]


async def test_auto_resume_skipped_when_task_meta_missing(monkeypatch):
    """快照没有 `task_meta` ⇒ 重建不出 task ⇒ **回落**为"标记可续跑"（不卡在 resuming）。"""
    rows = [_row("running")]
    rows[0]["checkpoint"] = _snapshot(task_meta=False)
    marks, _ = _patch_reconciler(monkeypatch, rows=rows, lock_held=False)
    ran: list[Any] = []

    async def runner(task: Any) -> None:
        ran.append(task)

    stats = await sweep_once(runner=runner)

    assert ran == [], "半个 task 比不跑更危险 ⇒ 宁可回落"
    assert stats["revived"] == 1
    assert marks[-1] == ("s1", "checkpointed"), "接管后必须回落状态，否则 run 永远停在 resuming"


async def test_auto_resume_requires_runner(monkeypatch):
    """没注入 runner（例如未接线的部署）⇒ 退化为「只修正状态」，行为与上一轮一致。"""
    _, claims = _patch_reconciler(monkeypatch, rows=[_row("running")], lock_held=False)

    stats = await sweep_once()

    assert stats["resumed"] == 0 and stats["revived"] == 1
    assert claims == []


async def test_auto_resume_lost_cas_does_not_run(monkeypatch):
    """CAS 抢输（别的实例先接管）⇒ 本实例**什么都不做**（状态已被对方改成 resuming）。"""
    marks, claims = _patch_reconciler(
        monkeypatch, rows=[_row("running")], lock_held=False, claim_ok=False
    )
    ran: list[Any] = []

    async def runner(task: Any) -> None:
        ran.append(task)

    stats = await sweep_once(runner=runner)

    assert stats["resumed"] == 0 and ran == []
    assert stats["contended"] == 1, "CAS 没抢到要单独记账"
    assert len(claims) == 1, "仍应尝试过 CAS"
    assert marks == [], "没抢到就不能再动状态（否则会覆盖对方刚抢到的 resuming）"


def test_rebuild_task_requires_task_meta():
    from app.agent.resume import rebuild_task

    assert rebuild_task(tenant_id="t", session_id="s", snapshot={}, run_token="x") is None
    assert (
        rebuild_task(
            tenant_id="t",
            session_id="s",
            snapshot={"task_meta": {"llm_config": "not-a-dict"}},
            run_token="x",
        )
        is None
    )
