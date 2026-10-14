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
