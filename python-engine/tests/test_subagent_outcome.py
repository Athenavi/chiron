"""R3（`vendor/规划.md` §3.5）：结构化结局与错误分类。

改动的实质：把"能否重试"从**拼接字符串**里解出来，变成**机器可读**的结论。对齐参照是 Reasonix 的
`subagent_outcome.go` + `subagentErrorDisposition`。

两个重点：

* **取消 / 预算 / 溢出 / provider 重试 / 工具失败必须可区分** —— 它们各自的处置不同（重试？压缩后
  重试？不再重试？）；
* **未知一律不重试**（安全默认）—— 重试要花 token 与钱，还可能重复副作用（写文件、发请求）。
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from app.agent import runtime as runtime_mod
from app.agent.subagent_runner import SubAgentRunner
from app.subagent import store as store_mod
from app.subagent.outcome import (
    CODE_BUDGET_EXCEEDED,
    CODE_CANCELLED,
    CODE_CONTEXT_OVERFLOW,
    CODE_INVALID_INPUT,
    CODE_PROVIDER_RETRY,
    CODE_TIMEOUT,
    CODE_TOOL_FAILURE,
    CODE_UNKNOWN,
    ERROR_CODES,
    RETRYABLE_CODES,
    classify_error,
    format_outcome,
    outcome_of,
)


class _ProvokeRetryLater:
    """只为让 `classify_error` 走"按类名匹配 RetryLaterError"那条支路。

    不直接 import `app.queue.worker`：那是队列模块，为一个分类测试把它拖进来（及其依赖）不值得。
    分类实现本身**优先**用 isinstance（那里才有真 import），类名匹配是它的兜底分支。
    """


_ProvokeRetryLater.__name__ = "RetryLaterError"


# ── 1. 分类：各码可区分 ────────────────────────────────────────────────


def test_cancellation_is_not_a_retryable_failure():
    """取消是"被停掉"，不是"失败" —— 重试等于违逆调用方的意愿。"""
    assert classify_error(asyncio.CancelledError()) == (CODE_CANCELLED, False)


def test_budget_exceeded_is_not_retryable():
    """同参数重试必然再超预算 —— 重试是白烧。"""
    from app.subagent.budget import BudgetExceeded

    assert classify_error(BudgetExceeded("tokens")) == (CODE_BUDGET_EXCEEDED, False)


def test_context_overflow_is_retryable_after_compaction():
    """溢出是**可**重试的，但处置与"稍后原样重试"不同：要先压缩。"""
    exc = RuntimeError("context_length_exceeded: prompt is too long")

    assert classify_error(exc) == (CODE_CONTEXT_OVERFLOW, True)


def test_provider_retry_later_is_retryable():
    assert classify_error(_ProvokeRetryLater()) == (CODE_PROVIDER_RETRY, True)


def test_tool_failure_is_not_retryable():
    """工具报的错（参数非法 / 沙箱拒绝）不会因为重试而变好。"""
    from app.tools.run_code import ToolCallError

    assert classify_error(ToolCallError("read_file", "bad args")) == (CODE_TOOL_FAILURE, False)


def test_timeout_is_retryable():
    assert classify_error(TimeoutError("too slow")) == (CODE_TIMEOUT, True)


def test_invalid_input_is_not_retryable():
    assert classify_error(ValueError("task is required")) == (CODE_INVALID_INPUT, False)


def test_unknown_defaults_to_not_retryable():
    """**安全默认**：拿不准就不重试（见模块文档）。"""
    assert classify_error(RuntimeError("???是什么错")) == (CODE_UNKNOWN, False)
    assert classify_error(None) == (CODE_UNKNOWN, False)


def test_retryable_set_matches_the_codes_we_claim():
    """防止"文档说可重试、集合里没有"这类漂移。"""
    assert RETRYABLE_CODES <= set(ERROR_CODES)
    assert CODE_UNKNOWN not in RETRYABLE_CODES


# ── 2. outcome_of：状态与异常的配合 ───────────────────────────────────


def test_success_and_partial_are_never_marked_retryable():
    for status in ("completed", "partial"):
        outcome = outcome_of(status=status, run_id="r1")
        assert outcome.retryable is False
        assert outcome.failed is False


def test_cancelled_status_maps_to_cancelled_code_without_exception():
    outcome = outcome_of(status="cancelled", run_id="r1")

    assert outcome.error_code == CODE_CANCELLED
    assert outcome.retryable is False


def test_lost_maps_to_timeout_and_is_retryable():
    """孤儿收口（进程没了）等价于超时 —— 值得重来一次。"""
    outcome = outcome_of(status="lost", run_id="r1")

    assert outcome.error_code == CODE_TIMEOUT
    assert outcome.retryable is True


def test_failure_preserves_partial_output():
    """对齐 Reasonix 的 `SubagentRunErrorPreservesPartialAnswer`：失败不等于什么都没产出。"""
    outcome = outcome_of(
        status="failed", run_id="r1", error="boom", partial_output="已经写了一半的结论"
    )

    assert outcome.partial_output == "已经写了一半的结论"
    assert outcome.ref == "r1"


def test_payload_is_machine_readable():
    payload = outcome_of(status="failed", run_id="r9", exc=TimeoutError("x")).to_payload()

    assert payload["error_code"] == CODE_TIMEOUT
    assert payload["retryable"] is True
    assert payload["result_ref"] == "r9"
    assert set(payload) >= {"status", "error_code", "retryable", "partial_output", "result_ref"}


def test_format_outcome_is_one_line():
    text = format_outcome(outcome_of(status="failed", run_id="r1", exc=TimeoutError("慢")))

    assert text.startswith("status=failed code=timeout")
    assert "retryable=yes" in text
    assert "\n" not in text


# ── 3. 同源（防两套判断）──────────────────────────────────────────────


def test_lifecycle_sql_carries_error_code_but_not_in_finish_sql():
    """`error_code` 与 `retryable` 一起走**单独写**（未迁移的库只丢这几列）。"""
    assert "error_code" in store_mod.LIFECYCLE_SQL
    assert "error_code" not in store_mod.RUN_FINISH_SQL


def test_runner_no_longer_makes_its_own_retry_judgement():
    """关键防回归：runner 里不许再有 `retryable=status in (...)` 这种第二套判断。"""
    import app.agent.subagent_runner as runner_mod

    src = pathlib.Path(runner_mod.__file__).read_text(encoding="utf-8")

    assert "retryable=status in" not in src, "重试判定必须来自 outcome_of（同源），不能各判一套"
    assert "retryable=_outcome.retryable" in src


# ── 4. 端到端：runner 把码与可重试落到 store ───────────────────────────


class _RecordingStore:
    def __init__(self) -> None:
        self.lifecycles: list[dict[str, object]] = []

    async def start_run(self, *a: object, **k: object) -> None:
        return None

    async def add_step(self, *a: object, **k: object) -> None:
        return None

    async def flush_steps(self, *a: object, **k: object) -> None:
        return None

    async def finish_run(self, *a: object, **k: object) -> None:
        return None

    async def mark_lifecycle(self, *a: object, **k: object) -> None:
        self.lifecycles.append(dict(k))


@pytest.mark.asyncio
async def test_runner_records_error_code_and_retryable(monkeypatch):
    """预算超限 ⇒ `budget_exceeded` + 不可重试（此前会被粗判成"可重试"）。"""

    async def fake_run(self, task):  # noqa: ANN001
        from app.subagent.budget import BudgetExceeded

        raise BudgetExceeded("tokens")
        yield  # pragma: no cover — 让它成为 async generator

    monkeypatch.setattr(runtime_mod.AgentRuntime, "run", fake_run)
    store = _RecordingStore()
    runner = SubAgentRunner(store=store, gateway=object())

    result = await runner.run("干活", mode="normal", max_turns=1)

    assert result.status == "failed"
    assert store.lifecycles, "应写入生命周期遥测"
    recorded = store.lifecycles[-1]
    assert recorded["error_code"] == CODE_BUDGET_EXCEEDED
    assert recorded["retryable"] is False
