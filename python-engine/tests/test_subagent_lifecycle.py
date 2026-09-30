"""A5（方案 04 §3）：子 agent 生命周期遥测。

本文件钉住四条，其中第一条是重点：

1. **content-free 是类型级事实**：`SubagentLifecycle` 的字段名里不许出现 content / prompt /
   text / path 之类 —— 排除"以后有人顺手加个 `error_text`"的可能。这是 A5 的核心取舍
   （对齐 Reasonix 的 `SubagentLifecycleInfo`）：只有不含内容，遥测才能被转发进诊断链路。
2. **opt-in**：没注册 sink 时零开销、不抛异常；sink 自己抛异常也不影响委派。
3. **7 个阶段**齐全，且**有产出的失败**记为 `partial` 而不是 `failed` —— 这两者的处置不同
   （前者该保留产物、不该盲目重试）。
4. **落库是单独写**（`store.LIFECYCLE_SQL`），未迁移的库只丢这几列。
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from app.agent import runtime as runtime_mod
from app.agent.subagent_runner import SubAgentRunner
from app.subagent import store as store_mod
from app.subagent.lifecycle import (
    ALLOWED_FIELD_NAMES,
    FORBIDDEN_FIELD_HINTS,
    PHASE_COMPLETED,
    PHASE_CREATED,
    PHASE_PARTIAL,
    PHASE_RESUME,
    PHASES,
    SubagentLifecycle,
    emit_lifecycle,
    register_lifecycle_sink,
    reset_lifecycle_sink,
)

# ── 1. content-free：类型级断言 ──────────────────────────────────────────


def test_lifecycle_dataclass_has_no_content_bearing_fields():
    """字段名一律不许暗示内容。这是"A5 不含内容"的**机械化**保证（而不是注释承诺）。"""
    offenders = []
    for f in dataclasses.fields(SubagentLifecycle):
        if f.name in ALLOWED_FIELD_NAMES:
            continue
        lowered = f.name.lower()
        for hint in FORBIDDEN_FIELD_HINTS:
            if hint in lowered:
                offenders.append(f"{f.name} (命中 {hint})")

    assert not offenders, f"生命周期遥测不得承载内容字段：{offenders}"


def test_allow_list_stays_deliberate():
    """豁免名单必须**确实**是"命中禁用词但非内容"的那类 —— 否则它就成了绕过检查的后门。"""
    for name in ALLOWED_FIELD_NAMES:
        assert any(hint in name.lower() for hint in FORBIDDEN_FIELD_HINTS), (
            f"{name} 没必要豁免（它本来就不命中禁用词）"
        )
    # 豁免的字段必须真在 dataclass 上（防止名单里留有早已删除的名字）
    field_names = {f.name for f in dataclasses.fields(SubagentLifecycle)}
    assert ALLOWED_FIELD_NAMES <= field_names


def test_documented_phases_are_content_free_too():
    """阶段名本身也不该描述内容。"""
    for phase in PHASES:
        assert "text" not in phase and "prompt" not in phase


def test_payload_expands_extra_without_addressing_content():
    info = SubagentLifecycle(
        phase=PHASE_COMPLETED,
        run_id="r1",
        extra={"steps": 3, "cache_hit": True},
    )

    payload = info.to_payload()

    assert payload["steps"] == 3 and payload["cache_hit"] is True
    assert "extra" not in payload  # 展开后不再留嵌套容器
    assert payload["run_id"] == "r1"


# ── 2. opt-in 与"旁路失败不影响主流程" ───────────────────────────────────


def test_emit_without_sink_is_noop():
    reset_lifecycle_sink()

    # 没注册 sink 时不该有成本、更不该抛
    emit_lifecycle(SubagentLifecycle(phase=PHASE_CREATED, run_id="r1"))


def test_sink_exception_is_swallowed():
    class _Boom:
        def emit(self, info: SubagentLifecycle) -> None:
            raise RuntimeError("diagnostics backend down")

    register_lifecycle_sink(_Boom())
    try:
        # 关键：诊断后端挂了不能把子 Agent 带崩
        emit_lifecycle(SubagentLifecycle(phase=PHASE_CREATED, run_id="r1"))
    finally:
        reset_lifecycle_sink()


def test_registered_sink_receives_events():
    seen: list[SubagentLifecycle] = []

    class _Sink:
        def emit(self, info: SubagentLifecycle) -> None:
            seen.append(info)

    register_lifecycle_sink(_Sink())
    try:
        emit_lifecycle(SubagentLifecycle(phase=PHASE_CREATED, run_id="r1", depth=2))
    finally:
        reset_lifecycle_sink()

    assert [i.phase for i in seen] == [PHASE_CREATED]
    assert seen[0].depth == 2


def test_seven_phases_match_reference():
    assert len(PHASES) == 7
    assert set(PHASES) == {
        "child_created",
        "child_running",
        "child_completed",
        "child_partial",
        "child_failed",
        "child_cancelled",
        "child_resume",
    }


# ── 3. runner 级：真的会发（含 partial 语义）────────────────────────────


class _NullStore:
    async def start_run(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def add_step(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def flush_steps(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def finish_run(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def mark_lifecycle(self, *args: Any, **kwargs: Any) -> None:
        return None


def _runner(monkeypatch: pytest.MonkeyPatch, events: list[Any], **kwargs: Any) -> SubAgentRunner:
    async def _run(self, task):  # noqa: ANN001
        for evt in events:
            yield evt

    monkeypatch.setattr(runtime_mod.AgentRuntime, "run", _run)
    return SubAgentRunner(store=_NullStore(), gateway=object(), **kwargs)


def _capture():
    seen: list[SubagentLifecycle] = []

    class _Sink:
        def emit(self, info: SubagentLifecycle) -> None:
            seen.append(info)

    register_lifecycle_sink(_Sink())
    return seen


@pytest.mark.asyncio
async def test_runner_emits_created_and_completed(monkeypatch):
    seen = _capture()
    try:
        runner = _runner(monkeypatch, [runtime_mod.AgentEvent(type="text", content="做完了")])
        await runner.run("干活", mode="normal", max_turns=1)
    finally:
        reset_lifecycle_sink()

    phases = [i.phase for i in seen]
    assert phases[0] == PHASE_CREATED
    assert PHASE_COMPLETED in phases


@pytest.mark.asyncio
async def test_runner_marks_partial_when_failed_with_output(monkeypatch):
    """有产出的失败 = partial。`retryable` 为假：有产物时盲目重试只是白烧 token。"""
    seen = _capture()
    try:
        runner = _runner(
            monkeypatch,
            [
                runtime_mod.AgentEvent(type="text", content="我已经改了一半"),
                runtime_mod.AgentEvent(type="error", error="budget_exceeded"),
            ],
        )
        result = await runner.run("干活", mode="normal", max_turns=1)
    finally:
        reset_lifecycle_sink()

    assert result.status == "partial"
    terminal = [i for i in seen if i.phase == PHASE_PARTIAL]
    assert terminal, "应有 child_partial 阶段"
    assert terminal[-1].retryable is False


@pytest.mark.asyncio
async def test_unknown_failure_is_not_retryable(monkeypatch):
    """A5 曾断言"没跑出任何东西的失败**值得**重试"；R3 把它修正为**反过来的保守默认**。

    为什么改：`error="boom"` 这类**未知**错误的处置无法判断 —— 它可能是"工具参数非法"（重试一万次
    也一样），也可能是"上游偶发"（值得重来）。而重试要花 token 与钱、还可能重复副作用（写文件、
    发请求），所以拿不准必须落在"不重试"这一侧。**可**重试的是能识别出来的那几类
    （`timeout` / `context_overflow` / `provider_retry`，见 tests/test_subagent_outcome.py）。
    """
    seen = _capture()
    try:
        runner = _runner(monkeypatch, [runtime_mod.AgentEvent(type="error", error="boom")])
        result = await runner.run("干活", mode="normal", max_turns=1)
    finally:
        reset_lifecycle_sink()

    assert result.status == "failed"
    assert [i for i in seen if i.phase == "child_failed"][-1].retryable is False


@pytest.mark.asyncio
async def test_rerun_is_reported_as_resume_not_created(monkeypatch):
    """`rerun_of` 非空 ⇒ 这是恢复/重跑，诊断上必须与"新派发"分开。"""
    seen = _capture()
    try:
        runner = _runner(monkeypatch, [runtime_mod.AgentEvent(type="text", content="继续")])
        await runner.run("接着干", mode="normal", max_turns=1, rerun_of="rs_old")
    finally:
        reset_lifecycle_sink()

    phases = [i.phase for i in seen]
    assert phases[0] == PHASE_RESUME
    assert PHASE_CREATED not in phases


# ── 4. 落库形态 ─────────────────────────────────────────────────────────


def test_lifecycle_sql_updates_separately_and_is_content_free():
    sql = store_mod.LIFECYCLE_SQL

    for column in ("retryable", "output_bytes", "validator_mode", "validator_outcome",
                   "validator_attempt"):
        assert column in sql, f"{column} 应可独立更新"
    assert "COALESCE" in sql, "传 None 应保持原值（调用方可以只更新一部分）"
    # 单独写：不并入 RUN_FINISH_SQL —— 未迁移的库只丢遥测，不影响收尾
    assert "LIFECYCLE_SQL" not in store_mod.RUN_FINISH_SQL
    assert "retryable" not in store_mod.RUN_FINISH_SQL


def test_partial_is_a_terminal_status():
    """`partial` 是终态 —— 否则僵尸收口器会把它当"还在跑"。"""
    assert "partial" in store_mod._TERMINAL_STATUSES  # noqa: SLF001
