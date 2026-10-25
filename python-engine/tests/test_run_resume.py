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
    usage: dict[str, Any] | None = None,
    turn_ids: list[str] | None = None,
    turn_ids_truncated: bool = False,
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
    if usage is not None:
        # M15e：run 级预算跨续跑的账就记在这一格（旧快照可能整格没有）
        snapshot["usage"] = usage
    if turn_ids is not None:
        snapshot["turn_ids"] = turn_ids
    if turn_ids_truncated:
        snapshot["turn_ids_truncated"] = True
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

    async def _load(*, tenant_id: int, session_id: str):  # noqa: ANN202
        return snapshot

    async def _pending(*, session_id: str, limit: int = 50):  # noqa: ANN202
        return list(pending or [])

    async def _mark(  # noqa: ANN202 — 与 checkpoint.mark 同步接受 run_token（A4 防踩踏）
        *, tenant_id: int, session_id: str, status: str, run_token: str = ""
    ) -> bool:
        recorded.append((session_id, status))
        return True

    monkeypatch.setattr("app.agent.checkpoint.load", _load)
    monkeypatch.setattr("app.agent.checkpoint.pending_tool_calls", _pending)
    monkeypatch.setattr("app.agent.checkpoint.mark", _mark)
    return recorded


# ── 入口恢复 ────────────────────────────────────────────────────────────


async def test_uses_snapshot_as_history(monkeypatch):
    _patch_checkpoint(monkeypatch, snapshot=_snapshot())

    state = await load_resume_state(tenant_id=1, session_id="s1")

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

    state = await load_resume_state(tenant_id=1, session_id="s1")

    assert state is not None
    assert state.replay_pending == ("call_b",), "已完成的调用不该进待重放"


async def test_expired_window_marks_abandoned_and_falls_back(monkeypatch):
    marks = _patch_checkpoint(
        monkeypatch, snapshot=_snapshot(seconds_ago=COLD_RESUME_WINDOW_SECONDS + 60)
    )

    state = await load_resume_state(tenant_id=1, session_id="s1")

    assert state is None, "超窗 ⇒ 回落现状语义（用户可整轮重试）"
    assert marks and marks[-1][1] == "abandoned", "超窗必须**可见**，不能静默"


async def test_empty_snapshot_falls_back(monkeypatch):
    _patch_checkpoint(monkeypatch, snapshot={"messages": [], "saved_at": _saved_at()})
    assert await load_resume_state(tenant_id=1, session_id="s1") is None


async def test_missing_snapshot_returns_none(monkeypatch):
    _patch_checkpoint(monkeypatch, snapshot=None)
    assert await load_resume_state(tenant_id=1, session_id="s1") is None


# ── M15e：token / wall 两轴的续跑起点 ───────────────────────────────────


async def test_resume_state_inherits_the_token_axes(monkeypatch):
    """接管那一段**不能**从 0 起算预算：快照里那份累计就是它的起点。

    `turn_index` 与 `tool_steps` 早就这么办了（A4），tokens 一直躺在同一个 `usage` 格子里
    没人读 —— 于是每次崩溃接管都白送一整份 per-run 额度（论文 §5.2）。
    """
    _patch_checkpoint(
        monkeypatch,
        snapshot=_snapshot(
            usage={
                "input_tokens": 12_000,
                "output_tokens": 3_400,
                "cached_tokens": 500,
                "tool_steps": 7,
                "wall_seconds": 41,
            }
        ),
    )

    state = await load_resume_state(tenant_id=1, session_id="s1")

    assert state is not None
    assert (state.input_tokens, state.output_tokens) == (12_000, 3_400)
    assert state.cached_tokens == 500
    assert (state.tool_steps, state.wall_seconds) == (7, 41)


async def test_legacy_snapshot_without_usage_starts_at_zero(monkeypatch):
    """旧快照没有这些键 ⇒ 起点 0（**不能**因为读不到就抛，也不能猜一个数）。"""
    _patch_checkpoint(monkeypatch, snapshot=_snapshot())

    state = await load_resume_state(tenant_id=1, session_id="s1")

    assert state is not None
    assert (state.input_tokens, state.output_tokens, state.cached_tokens) == (0, 0, 0)
    assert (state.tool_steps, state.wall_seconds) == (0, 0)


# ── M19：run 链的网关 turn 集合（网关按-run 对账的依据）──────────────────


async def test_resume_state_inherits_turn_ids(monkeypatch):
    """续跑段没有自己的网关 turn，但**必须**继承历史集合。

    否则网关对账看到的"已计费回合"少一个，会把那条已计过费的回合再补一遍（多收）。
    """
    _patch_checkpoint(
        monkeypatch,
        snapshot=_snapshot(turn_ids=["turn-a", "turn-b"], turn_ids_truncated=False),
    )

    state = await load_resume_state(tenant_id=1, session_id="s1")

    assert state is not None
    assert state.turn_ids == ("turn-a", "turn-b")
    assert state.turn_ids_truncated is False


async def test_legacy_snapshot_has_no_turn_ids(monkeypatch):
    """本字段引入前的快照：集合为空、不算截断（旧 run 不参与对账，也不报错）。"""
    _patch_checkpoint(monkeypatch, snapshot=_snapshot())

    state = await load_resume_state(tenant_id=1, session_id="s1")

    assert state is not None
    assert state.turn_ids == ()
    assert state.turn_ids_truncated is False


async def test_truncated_turn_ids_stay_marked(monkeypatch):
    """截断标记要原样带出：对账据此**跳过**该 run，而不是拿不完整的集合去补差。"""
    _patch_checkpoint(
        monkeypatch,
        snapshot=_snapshot(turn_ids=["turn-b"], turn_ids_truncated=True),
    )

    state = await load_resume_state(tenant_id=1, session_id="s1")

    assert state is not None
    assert state.turn_ids_truncated is True


async def test_dirty_usage_reads_as_zero_not_as_a_negative_allowance(monkeypatch):
    """`usage` 是 JSONB，人工修数据写得歪。负的起点等于**白送**额度，所以只接受非负；
    非数值同理按"这一轴没记"处理，而不是把整份现场判成不可用（那会连带丢掉可续跑的历史）。
    """
    _patch_checkpoint(
        monkeypatch,
        snapshot=_snapshot(
            usage={"input_tokens": -999_999, "output_tokens": "n/a", "wall_seconds": None}
        ),
    )

    state = await load_resume_state(tenant_id=1, session_id="s1")

    assert state is not None, "脏计数不该废掉整份现场"
    assert (state.input_tokens, state.output_tokens, state.wall_seconds) == (0, 0, 0)


# ── M7：接手"等人"的挂起现场 ────────────────────────────────────────────


def _patch_awaiting(
    monkeypatch: Any, *, found: dict[str, Any] | None, claim_ok: bool = True
) -> dict[str, list[str]]:
    """挂起现场的读 / 接手 / 作废三条通道各记一份调用（真实实现是 CAS UPDATE）。"""
    calls: dict[str, list[str]] = {"read": [], "claim": [], "release": []}

    async def _load_awaiting(*, tenant_id: int, session_id: str):  # noqa: ANN202
        calls["read"].append(session_id)
        return found

    async def _claim(*, tenant_id: int, session_id: str, new_run_token: str) -> bool:  # noqa: ANN202
        calls["claim"].append(new_run_token)
        return claim_ok

    async def _release(*, tenant_id: int, session_id: str, run_token: str) -> bool:  # noqa: ANN202
        calls["release"].append(run_token)
        return True

    monkeypatch.setattr("app.agent.checkpoint.load_awaiting", _load_awaiting)
    monkeypatch.setattr("app.agent.checkpoint.claim_awaiting", _claim)
    monkeypatch.setattr("app.agent.checkpoint.release_awaiting", _release)
    return calls


async def test_awaiting_snapshot_is_claimed_with_the_new_run_token(monkeypatch):
    """读挂起现场**必须**与接手配对：以本次 run 的 token 把 `awaiting_input → resuming`。"""
    _patch_checkpoint(monkeypatch, snapshot=None)
    calls = _patch_awaiting(
        monkeypatch, found={"checkpoint": _snapshot(), "run_token": "rt-old"}
    )

    state = await load_resume_state(tenant_id=1, session_id="s1", run_token="rt-new")

    assert state is not None and state.window == "hot"
    assert calls["claim"] == ["rt-new"], "接手要以**新** token 为归属凭据"


async def test_awaiting_snapshot_also_carries_the_budget_seed(monkeypatch):
    """人工介入的续跑走的是另一条读路径（`load_awaiting`），但**同一份账**：也继承起点。

    它同时解释了"为什么事件的扁平 token 必须留在本段口径" —— 挂起那一段已经用它自己的
    `done` 报过一次账，续跑再报全程累计就是把同一段扣两遍（断言见
    `tests/test_run_budget_resume.py`）。
    """
    _patch_checkpoint(monkeypatch, snapshot=None)
    _patch_awaiting(
        monkeypatch,
        found={
            "checkpoint": _snapshot(usage={"input_tokens": 800, "output_tokens": 120}),
            "run_token": "rt-old",
        },
    )

    state = await load_resume_state(tenant_id=1, session_id="s1", run_token="rt-new")

    assert state is not None
    assert (state.input_tokens, state.output_tokens) == (800, 120)


async def test_awaiting_not_claimed_falls_back_to_a_plain_turn(monkeypatch):
    """抢不到（别的回合已接手 / 会话上已有活跃 run 撞唯一索引）⇒ 不拿这份现场续跑。"""
    _patch_checkpoint(monkeypatch, snapshot=None)
    _patch_awaiting(
        monkeypatch, found={"checkpoint": _snapshot(), "run_token": "rt-old"}, claim_ok=False
    )

    assert await load_resume_state(tenant_id=1, session_id="s1", run_token="rt-new") is None


async def test_over_window_awaiting_is_released_not_left_hanging(monkeypatch):
    """超窗的挂起现场要**作废**：`mark()` 只认活跃状态 ⇒ 不单独收尾就永远停在
    `awaiting_input`，之后每一次 run 都白读一遍这份陈旧现场。"""
    marks = _patch_checkpoint(monkeypatch, snapshot=None)
    calls = _patch_awaiting(
        monkeypatch,
        found={"checkpoint": _snapshot(seconds_ago=COLD_RESUME_WINDOW_SECONDS + 60), "run_token": "rt-old"},
    )

    assert await load_resume_state(tenant_id=1, session_id="s1", run_token="rt-new") is None
    assert calls["release"] == ["rt-old"]
    assert marks == [], "挂起行不能被 `mark()` 迁（那是活跃集合的通道）"


async def test_broken_awaiting_snapshot_is_released(monkeypatch):
    _patch_checkpoint(monkeypatch, snapshot=None)
    calls = _patch_awaiting(
        monkeypatch, found={"checkpoint": {"messages": [], "saved_at": _saved_at()}, "run_token": "rt-old"}
    )

    assert await load_resume_state(tenant_id=1, session_id="s1", run_token="rt-new") is None
    assert calls["release"] == ["rt-old"]
    assert calls["claim"] == [], "没有可用消息 ⇒ 不该把它标成 resuming（那会占住会话）"


async def test_caller_without_run_token_never_touches_a_suspension(monkeypatch):
    """没有归属凭据的调用方**完全不碰**挂起现场 —— 否则陈旧挂起会被无关消息复活。"""
    _patch_checkpoint(monkeypatch, snapshot=None)
    calls = _patch_awaiting(monkeypatch, found={"checkpoint": _snapshot(), "run_token": "rt-old"})

    assert await load_resume_state(tenant_id=1, session_id="s1") is None
    assert calls["read"] == []


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
    """返回 `(marks, claims)`：状态改动与接管 CAS 各记一份，便于断言"走没走接管"。

    fake 同时按**真实 SQL 的前提条件**判定命中与否（活跃状态集合 + `run_token` 归属）。
    这套恢复状态机的不变量全在条件里 —— fake 若无条件返回 True，"续跑没落地要退回
    checkpointed"与"终态不能被旁观者覆盖"两类测试就只是在验证"调用发生过"。
    """
    pool = _FakePool(rows)
    marks: list[tuple[str, str]] = []
    claims: list[tuple[str, str, str]] = []
    active = {"running", "checkpointed", "resuming"}
    state: dict[str, tuple[str, str]] = {
        str(r["session_id"]): (str(r["status"]), str(r.get("run_token") or "")) for r in rows
    }

    async def _mark(*, tenant_id: int, session_id: str, status: str, run_token: str = "") -> bool:  # noqa: ANN202
        marks.append((session_id, status))
        cur_status, cur_token = state.get(session_id, ("", ""))
        hit = cur_status in active and (not run_token or cur_token == run_token)
        if hit:
            state[session_id] = (status, cur_token)
        return hit

    async def _claim(  # noqa: ANN202
        *, tenant_id: int, session_id: str, expected_run_token: str, new_run_token: str
    ) -> bool:
        claims.append((session_id, expected_run_token, new_run_token))
        if not claim_ok:
            return False
        cur_status, cur_token = state.get(session_id, ("", ""))
        hit = cur_status in active and (
            not expected_run_token or cur_token == expected_run_token
        )
        if hit:
            state[session_id] = ("resuming", new_run_token)
        return hit

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
        "tenant_id": 1,
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
        from app.agent import checkpoint as ckpt

        ran.append(task)
        # 模拟 runtime 收尾写终态（M1）：这就是"续跑落地"的样子
        await ckpt.mark(
            tenant_id=task.tenant_id,
            session_id=task.session_id,
            status=ckpt.STATUS_COMPLETED,
            run_token=task.run_token,
        )

    stats = await sweep_once(runner=runner)

    assert stats["resumed"] == 1
    assert stats["released"] == 0, "runtime 已写终态 ⇒ 现场退回的条件更新必须 0 行命中"
    assert len(ran) == 1, "runner 必须被调用（否则就只是标记了一下）"
    task = ran[0]
    assert task.session_id == "s1" and task.tenant_id == 1
    assert task.run_token, "续跑必须带新的 run_token（CAS 的结果，供后续归属判定）"
    assert task.llm_config == {"model": "m"}, "重建的 task 要带上原 llm_config"
    assert task.content == "", "用户消息已在快照历史里 ⇒ content 置空（不重复追加）"
    assert claims and claims[0][1] == "tok_old", "接管前必须用旧 token 做 CAS"
    assert marks[0] == ("s1", "completed"), "runner 收尾写终态"
    assert marks[1] == ("s1", "checkpointed"), "sweep 也会试一次退回（真实 SQL 里 0 行命中）"
    assert stats["released"] == 0, "终态已落 ⇒ 退回不得生效"


async def test_sweep_returns_the_scene_when_runner_raises(monkeypatch):
    """runner 抛错 ⇒ `resuming` 必须退回 `checkpointed`。

    不退，这一行就永远落在扫描条件（`status IN ('running','checkpointed')`）之外：
    巡检看不见、不再重试，前端也只是长时间无任何信号。
    """
    marks, _ = _patch_reconciler(monkeypatch, rows=[_row("running")], lock_held=False)

    async def runner(task: Any) -> None:
        raise RuntimeError("gateway exploded")

    stats = await sweep_once(runner=runner)

    assert stats["resumed"] == 0 and stats["failed"] == 1
    assert stats["released"] == 1
    assert marks == [("s1", "checkpointed")]


async def test_sweep_returns_the_scene_when_runner_lands_nothing(monkeypatch):
    """runner **正常返回但什么都没做**（gateway 不可用时 `_auto_resume_runner` 就是这样）也算没落地。

    指标上 `released` 才是"续跑没落地"的权威计数：`resumed` 只在现场没被退回时才加。
    """
    marks, _ = _patch_reconciler(monkeypatch, rows=[_row("running")], lock_held=False)

    async def runner(task: Any) -> None:
        return None

    stats = await sweep_once(runner=runner)

    assert stats["resumed"] == 0 and stats["released"] == 1
    assert marks == [("s1", "checkpointed")]


async def test_sweep_release_is_scoped_to_the_taken_over_run(monkeypatch):
    """现场退回必须带上**接管时的 run_token**：这期间同会话若已开新 run，旁观者不能踩。

    状态前提（活跃集合 + token 归属）本身在 `checkpoint.mark` 的 SQL 里，这里钉住
    "sweep 传了这个归属条件"。
    """
    pool = _FakePool([_row("running")])

    async def _get_redis():  # noqa: ANN202
        return _FakeRedis(False)

    monkeypatch.setattr("app.agent.checkpoint._pool", lambda: pool)
    monkeypatch.setattr("app.redis_client.get_redis", _get_redis)
    monkeypatch.setattr("app.agent.resume._metrics", lambda: None)

    new_tokens: list[str] = []
    mark_calls: list[tuple[str, str, str]] = []

    async def _claim(*, tenant_id: int, session_id: str, expected_run_token: str, new_run_token: str) -> bool:  # noqa: ANN202
        new_tokens.append(new_run_token)
        return True

    async def _mark(*, tenant_id: int, session_id: str, status: str, run_token: str = "") -> bool:  # noqa: ANN202
        mark_calls.append((session_id, status, run_token))
        return True

    async def runner(task: Any) -> None:
        return None  # 落地失败：既不写终态也不抛错

    monkeypatch.setattr("app.agent.checkpoint.claim", _claim)
    monkeypatch.setattr("app.agent.checkpoint.mark", _mark)

    stats = await sweep_once(runner=runner)

    assert stats["released"] == 1 and stats["resumed"] == 0
    assert mark_calls == [("s1", "checkpointed", new_tokens[0])], "退回必须以接管 token 为条件"



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

    assert rebuild_task(tenant_id=1, session_id="s", snapshot={}, run_token="x") is None
    assert (
        rebuild_task(
            tenant_id=1,
            session_id="s",
            snapshot={"task_meta": {"llm_config": "not-a-dict"}},
            run_token="x",
        )
        is None
    )


# ── M7：巡检顺带回收过期的人工介入现场 ──────────────────────────────────


async def test_sweep_expires_stale_interactions(monkeypatch):
    """过期票要**落成状态**：不回收的话 `ux_pending_interactions_open` 一直为它留名额，
    重新询问同一件事时 `create` 会静默失败。"""
    from app.agent import interactions

    seen: list[int] = []

    async def _expire(*, limit: int = 50) -> int:
        seen.append(limit)
        return 3

    monkeypatch.setattr(interactions, "expire_stale", _expire)
    _patch_reconciler(monkeypatch, rows=[], lock_held=False)

    stats = await sweep_once()

    assert seen == [50]
    assert stats["pending_expired"] == 3


async def test_sweep_still_expires_when_the_run_scan_fails(monkeypatch):
    """run 表读不到不该连带把票也留着（过期回收与僵尸扫描是两件事）。

    真实实现里 `expire_stale` 自己 fail-soft（返回 0），所以这里同样不需要它抛出去。
    """
    from app.agent import interactions

    async def _expire(*, limit: int = 50) -> int:
        return 1

    class _BoomPool:
        async def fetch(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("db down")

    monkeypatch.setattr(interactions, "expire_stale", _expire)
    monkeypatch.setattr("app.agent.checkpoint._pool", lambda: _BoomPool())

    stats = await sweep_once()

    assert stats["scanned"] == 0
    assert stats["pending_expired"] == 1
