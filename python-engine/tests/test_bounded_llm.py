"""差距A：bounded_llm（有界独立 LLM 调用组件）单测。

钉住组件的契约：**失败不抛异常**（ok=False）、超时受控、JSON 解析 fail-closed。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.agent.bounded_llm import (
    DEFAULT_TIMEOUT_SECONDS,
    BoundedResult,
    call_bounded,
    clip,
    parse_json_object,
)


class _FakeGateway:
    def __init__(self, *, content: str = "ok", in_tok: int = 3, out_tok: int = 7, delay: float = 0.0, boom: bool = False) -> None:
        self._content = content
        self._in = in_tok
        self._out = out_tok
        self._delay = delay
        self._boom = boom
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: Any, model: str, max_tokens: int, temperature: float = 0.0) -> Any:  # noqa: ANN401
        self.calls.append({"max_tokens": max_tokens, "temperature": temperature})
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._boom:
            raise RuntimeError("provider exploded")

        class _Resp:
            content = self._content
            input_tokens = self._in
            output_tokens = self._out

        return _Resp()


async def test_success_returns_text_and_usage() -> None:
    gw = _FakeGateway(content="hello", in_tok=5, out_tok=9)
    r = await call_bounded(gw, messages=[{"role": "user", "content": "x"}], model="m")
    assert r.ok is True
    assert r.text == "hello"
    assert r.input_tokens == 5 and r.output_tokens == 9
    assert r.total_tokens == 14


async def test_no_gateway_is_fail_soft() -> None:
    r = await call_bounded(None, messages=[], model="m", purpose="guardian")
    assert r.ok is False
    assert "no gateway" in r.error


async def test_provider_error_is_swallowed() -> None:
    gw = _FakeGateway(boom=True)
    r = await call_bounded(gw, messages=[], model="m", purpose="rubric")
    assert r.ok is False
    assert "rubric" in r.error


async def test_timeout_is_bounded() -> None:
    gw = _FakeGateway(delay=0.5)
    r = await call_bounded(gw, messages=[], model="m", timeout=0.05, purpose="plan-review")
    assert r.ok is False
    assert "timed out" in r.error


async def test_max_tokens_and_temperature_forwarded() -> None:
    gw = _FakeGateway()
    await call_bounded(gw, messages=[], model="m", max_tokens=123, temperature=0.0)
    assert gw.calls[0]["max_tokens"] == 123
    assert gw.calls[0]["temperature"] == 0.0


def test_parse_direct_json() -> None:
    assert parse_json_object('{"a": 1}') == {"a": 1}


def test_parse_json_wrapped_in_prose() -> None:
    assert parse_json_object('Sure:\n{"a": 1}\ndone') == {"a": 1}


def test_parse_json_rejects_non_object() -> None:
    assert parse_json_object("[1,2]") is None
    assert parse_json_object("no json here") is None
    assert parse_json_object("") is None
    assert parse_json_object('{"broken": ') is None


def test_clip_truncates() -> None:
    assert clip("abc", 10) == "abc"
    assert clip("a" * 20, 5).startswith("aaaaa")
    assert "truncated" in clip("a" * 20, 5)


def test_clip_serializes_non_strings() -> None:
    assert clip({"k": 1}, 100) == '{"k": 1}'


def test_default_timeout_is_bounded() -> None:
    assert 1.0 <= DEFAULT_TIMEOUT_SECONDS <= 120.0


def test_result_defaults_are_failure() -> None:
    r = BoundedResult()
    assert r.ok is False
    assert r.total_tokens == 0


# ── 旁路调用的 OpenCode session 兜底 + provider error 识别 ──────────────────
# 回归背景（真实故障）：`/v1/agent/refine` 这类**主对话 runtime 之外**的旁路调用
# 从未设置 `x-opencode-session` 的 contextvar ⇒ OpenCode 系上游 400 MissingSessionID
# ⇒ `GatewayRouter.chat` 吞错返回空 content ⇒ 前端只看到 "refine produced empty output"。


class _SessionCapturingGateway:
    """记录 `chat` 被调用时可见的会话标识，并返回可控的 `ChatResponse` 形态对象。"""

    def __init__(self, *, finish_reason: str = "stop", message: str = "") -> None:
        self._finish_reason = finish_reason
        self._message = message
        self.seen_session_id: str | None = None

    async def chat(self, messages: Any, model: str, max_tokens: int, temperature: float = 0.0) -> Any:  # noqa: ANN401
        from app.providers.session_context import get_llm_session_id

        self.seen_session_id = get_llm_session_id()
        fr, msg = self._finish_reason, self._message

        class _Resp:
            content = "refined text"
            input_tokens = 1
            output_tokens = 2
            finish_reason = fr
            message = msg

        return _Resp()


async def test_bypass_sets_stable_session_id_when_missing() -> None:
    from app.providers.session_context import set_llm_session_id

    set_llm_session_id("")
    gw = _SessionCapturingGateway()
    r = await call_bounded(gw, messages=[], model="m", purpose="prompt-refine")
    assert r.ok is True and r.text == "refined text"
    assert gw.seen_session_id == "chiron-bypass-prompt-refine"


async def test_bypass_keeps_existing_session_id() -> None:
    from app.providers.session_context import set_llm_session_id

    set_llm_session_id("sess-existing")
    gw = _SessionCapturingGateway()
    await call_bounded(gw, messages=[], model="m", purpose="guardian")
    assert gw.seen_session_id == "sess-existing"


async def test_provider_error_finish_reason_is_reported_not_empty_output() -> None:
    from app.providers.session_context import set_llm_session_id

    set_llm_session_id("s")
    gw = _SessionCapturingGateway(finish_reason="error", message="400 MissingSessionID")
    r = await call_bounded(gw, messages=[], model="m", purpose="prompt-refine")
    assert r.ok is False
    assert "MissingSessionID" in r.error


async def test_bypass_session_would_be_injected_as_opencode_header() -> None:
    """组合验证：旁路调用设好兜底会话后，provider 层确实会注入 `x-opencode-session`。

    这是 `/v1/agent/refine` 走到 OpenCode 系上游时真正需要的头（缺失即 400）。
    """
    from app.providers.named import NamedOpenAIProvider
    from app.providers.session_context import set_llm_session_id

    set_llm_session_id("")
    gw = _SessionCapturingGateway()
    await call_bounded(gw, messages=[], model="m", purpose="prompt-refine")

    kwargs = NamedOpenAIProvider(
        "opencode-go-anthropic", api_key="sk-fake-for-test"
    )._build_kwargs([], "m", 100, 0.0, None)
    assert kwargs.get("extra_headers") == {"x-opencode-session": "chiron-bypass-prompt-refine"}


