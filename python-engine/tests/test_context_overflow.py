"""C2 段 b / C4 回归：上下文溢出恢复 + 窗口表驱动的 token 计量。

三件事必须成立：

1. **溢出识别要覆盖各家**（OpenAI 的 `context_length_exceeded`、Anthropic 的
   `prompt is too long`、以及挂在 `body`/`code` 属性上的错误体）—— 不归一就只能靠猜；
2. **只重试一次**（溢出恢复要重发整段上下文，成本翻倍；再失败说明压缩没救回来）；
3. **窗口来自表而不是常量**（`llm_models.context_window`），且查不到时安全回落。
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.agent import model_window
from app.gateway.errors import is_context_overflow


class _FakeWindowPool:
    """替身池：只回答窗口查询。"""

    def __init__(self, window: int | None) -> None:
        self.window = window
        self.queries = 0

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.queries += 1
        if self.window is None:
            return None
        return {"context_window": self.window}


@pytest.fixture(autouse=True)
def _clear_window_cache():
    model_window.clear_cache()
    yield
    model_window.clear_cache()


# ── C2 段 b：溢出识别 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "Error code: 400 - {'code': 'context_length_exceeded'}",
        "This model's maximum context length is 8192 tokens",
        "prompt is too long: 210000 tokens > 200000 maximum",
        "too many tokens in request",
    ],
)
def test_overflow_markers_are_recognized(message: str):
    assert is_context_overflow(RuntimeError(message)) is True


def test_overflow_reads_nested_error_body():
    """SDK 把真正的错误码挂在属性上，`str(exc)` 往往只有笼统提示。"""

    class _SdkError(Exception):
        def __init__(self) -> None:
            super().__init__("Bad request")
            self.body = {"error": {"code": "context_length_exceeded"}}

    assert is_context_overflow(_SdkError()) is True


def test_non_overflow_errors_are_not_misread():
    """宁可少报也不误报：误报会把限流/鉴权失败当成溢出，白重发一次昂贵的请求。"""
    assert is_context_overflow(RuntimeError("429 rate limit exceeded")) is False
    assert is_context_overflow(RuntimeError("invalid api key")) is False


# ── C4：窗口表 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_context_window_reads_table_and_caches(monkeypatch: pytest.MonkeyPatch):
    pool = _FakeWindowPool(200_000)
    monkeypatch.setattr(model_window, "_query_window", _query_with(pool))

    assert await model_window.context_window("gpt-x") == 200_000
    assert await model_window.context_window("gpt-x") == 200_000
    assert pool.queries == 1, "第二次应命中进程内缓存"


@pytest.mark.asyncio
async def test_context_window_falls_back_when_absent(monkeypatch: pytest.MonkeyPatch):
    pool = _FakeWindowPool(None)
    monkeypatch.setattr(model_window, "_query_window", _query_with(pool))

    assert await model_window.context_window("unknown") == model_window.DEFAULT_CONTEXT_WINDOW
    assert await model_window.context_window("") == model_window.DEFAULT_CONTEXT_WINDOW


def _query_with(pool: _FakeWindowPool):
    async def _query(model: str) -> int:
        row = await pool.fetchrow("SELECT context_window", model)
        if row and row.get("context_window"):
            return int(row["context_window"])
        return model_window.DEFAULT_CONTEXT_WINDOW

    return _query


def test_estimator_calibrates_towards_observed_usage():
    est = model_window.TokenEstimator()
    messages = [{"role": "user", "content": "x" * 400}]  # 100 chars/token 的基线估算

    before = est.estimate(messages, model="m1")
    assert before > 0
    assert est.scale("m1") == 1.0, "未校准前系数为 1"

    # provider 说实际用了 2 倍 token → 系数应向上移动（平滑，不会一步到位）
    est.calibrate(messages, model="m1", actual_input_tokens=before * 2)
    assert 1.0 < est.scale("m1") < 2.0
    assert est.estimate(messages, model="m1") > before


def test_estimator_ignores_absurd_observations():
    """一次坏数据不该把系数带偏 —— 那会让后续所有阈值判断跟着错。"""
    est = model_window.TokenEstimator()
    messages = [{"role": "user", "content": "x" * 400}]

    est.calibrate(messages, model="m1", actual_input_tokens=10**9)  # 离谱的大值
    assert est.scale("m1") == 1.0

    est.calibrate(messages, model="m1", actual_input_tokens=0)
    assert est.scale("m1") == 1.0


# ── C2 段 b：runtime 的溢出重试 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_runtime_retries_once_on_context_overflow(monkeypatch: pytest.MonkeyPatch):
    from app.agent.runtime import AgentRuntime, AgentTask
    from app.gateway.provider import ChatResponse
    from app.tools.context import set_tool_context

    set_tool_context(session_id="s-overflow")
    calls = {"n": 0}

    async def fake_stream(**kwargs: Any):
        calls["n"] += 1
        if calls["n"] == 1:
            # 第一次就报溢出（形态与 OpenAI 一致）
            raise RuntimeError("Error code: 400 - context_length_exceeded")
        yield ChatResponse(content="ok", finish_reason="stop")

    gateway = MagicMock()
    gateway.chat_stream = fake_stream

    runtime = AgentRuntime(gateway=gateway)
    task = AgentTask(
        id="t",
        tenant_id="t1",
        user_id="u1",
        session_id="s-overflow",
        content="hi",
        system_prompt="sp",
        llm_config={"mode": "normal"},
        max_turns=2,
    )
    events = [event async for event in runtime.run(task)]

    assert calls["n"] == 2, "溢出后应重试一次"
    assert events[-1].type == "done", f"重试后应正常收尾：{[e.type for e in events]}"
    forced = [
        e for e in events if e.type == "compaction" and "context_overflow" in (e.content or "")
    ]
    assert forced, "溢出恢复必须让前端看得见（compaction 事件带 reason）"


@pytest.mark.asyncio
async def test_runtime_does_not_retry_twice(monkeypatch: pytest.MonkeyPatch):
    """压缩没救回来时**不再重试** —— 继续重试只是烧钱。"""
    from app.agent.runtime import AgentRuntime, AgentTask
    from app.gateway.provider import ChatResponse
    from app.tools.context import set_tool_context

    set_tool_context(session_id="s-overflow-2")
    calls = {"n": 0}

    async def always_overflow(**kwargs: Any):
        calls["n"] += 1
        raise RuntimeError("context_length_exceeded")
        yield ChatResponse(content="unreachable", finish_reason="stop")  # pragma: no cover

    gateway = MagicMock()
    gateway.chat_stream = always_overflow

    runtime = AgentRuntime(gateway=gateway)
    task = AgentTask(
        id="t",
        tenant_id="t1",
        user_id="u1",
        session_id="s-overflow-2",
        content="hi",
        system_prompt="sp",
        llm_config={"mode": "normal"},
        max_turns=2,
    )
    events = [event async for event in runtime.run(task)]

    assert calls["n"] == 2, f"应只重试一次（实际 {calls['n']} 次）"
    assert events[-1].type == "error", "最终应以 error 收尾（不静默）"
