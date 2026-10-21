"""M23：换 key 重试的两半 —— 错误分类与编排（提交点之前、预算默认 1）。

为什么要单独钉这两件事：
1. **分类错了会静默出两种事故**：把 400（请求本身错）当可重试，就白花一次调用并掩盖真错误；
   把 429（该 key 限流）当不可重试，用户就会看到一次本可避免的失败或模型降级。
2. **编排错了会重复计费**：只在"还没向调用方产出任何东西"之前换 key —— 这里用
   `resolve`/`call` 两个可注入的协程把"提交点"变成可断言的事实。
"""
from __future__ import annotations

from typing import Any

import pytest

from app.config import settings
from app.gateway import key_retry as kr


class _StatusError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"http {status}")
        self.status_code = status


class _TimeoutError(Exception):
    """类名含 `timeout` ⇒ 传输类（不 import 任何 SDK，见 `_TRANSPORT_HINTS`）。"""


@pytest.fixture(autouse=True)
def _default_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "key_retry_max", 1)


# ── 错误分类 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [401, 403, 408, 409, 429, 500, 502, 503, 504])
def test_retryable_statuses(status: int) -> None:
    assert kr.is_retryable_key_error(_StatusError(status)) is True


@pytest.mark.parametrize("status", [400, 404, 422, 451])
def test_non_retryable_statuses(status: int) -> None:
    """请求本身错 / 内容策略：换 key 结果一样，重试只是白花一次调用。"""
    assert kr.is_retryable_key_error(_StatusError(status)) is False


def test_transport_errors_are_retryable() -> None:
    assert kr.is_retryable_key_error(_TimeoutError("connect timed out")) is True


def test_unknown_error_is_not_retryable() -> None:
    """判不出来就不重试：宁可让调用方按既有语义降级，也不要盲目放大上游压力。"""
    assert kr.is_retryable_key_error(ValueError("boom")) is False


def test_status_is_read_from_response_object() -> None:
    class _WrappedError(Exception):
        def __init__(self) -> None:
            super().__init__("wrapped")
            self.response = type("R", (), {"status_code": 429})()

    assert kr.is_retryable_key_error(_WrappedError()) is True


# ── 预算开关 ──────────────────────────────────────────────────────────────


def test_budget_defaults_and_zero_means_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "key_retry_max", 1)
    assert kr.key_retry_max() == 1
    monkeypatch.setattr(settings, "key_retry_max", 0)
    assert kr.key_retry_max() == 0
    monkeypatch.setattr(settings, "key_retry_max", -3)
    assert kr.key_retry_max() == 0, "负值等价于关"


def test_budget_falls_back_when_config_is_dirty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "key_retry_max", "n/a")
    assert kr.key_retry_max() == kr.DEFAULT_KEY_RETRY_MAX


# ── 编排 ──────────────────────────────────────────────────────────────────


class _Harness:
    """记录每次取到的 key 与调用结果，供断言"换没换 key、报没报失败"。"""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.resolved = 0
        self.calls: list[str] = []
        self.reported: list[str] = []

    async def resolve(self) -> tuple[Any, Any]:
        self.resolved += 1
        key = f"k{self.resolved}"
        return (f"client-{key}", {"key": key})

    async def call(self, client: Any) -> str:
        self.calls.append(str(client))
        outcome = self._outcomes.pop(0) if self._outcomes else "ok"
        if isinstance(outcome, BaseException):
            raise outcome
        return str(outcome)

    async def report_failure(self, item: Any) -> None:
        self.reported.append(str((item or {}).get("key", "")))


async def test_retries_with_a_different_key_on_retryable_error() -> None:
    h = _Harness([_StatusError(429), "second-try-ok"])

    got = await kr.call_with_key_retry(
        provider="openai", resolve=h.resolve, call=h.call, report_failure=h.report_failure
    )

    assert got == "second-try-ok"
    assert h.calls == ["client-k1", "client-k2"], "必须换一把 key（重新 resolve）"
    assert h.reported == ["k1"], "旧 key 无论是否重试都要上报冷却"


async def test_does_not_retry_on_non_retryable_error() -> None:
    h = _Harness([_StatusError(400)])

    with pytest.raises(_StatusError):
        await kr.call_with_key_retry(
            provider="openai", resolve=h.resolve, call=h.call, report_failure=h.report_failure
        )

    assert h.resolved == 1, "不可重试的错误不该再取一把 key"
    assert h.reported == ["k1"]


async def test_budget_zero_disables_the_mechanism(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "key_retry_max", 0)
    h = _Harness([_StatusError(429)])

    with pytest.raises(_StatusError):
        await kr.call_with_key_retry(
            provider="openai", resolve=h.resolve, call=h.call, report_failure=h.report_failure
        )

    assert h.resolved == 1


async def test_no_key_available_is_not_retried() -> None:
    """取不到 key（KeyRing 空）时重试没有意义 —— 再取一次还是空的。"""

    class _NoKey(_Harness):
        async def resolve(self) -> tuple[Any, Any]:
            self.resolved += 1
            return (None, None)

    h = _NoKey([_StatusError(429)])

    with pytest.raises(_StatusError):
        await kr.call_with_key_retry(
            provider="openai", resolve=h.resolve, call=h.call, report_failure=h.report_failure
        )

    assert h.resolved == 1


async def test_budget_exhausted_raises_the_second_error() -> None:
    h = _Harness([_StatusError(503), _StatusError(503)])

    with pytest.raises(_StatusError):
        await kr.call_with_key_retry(
            provider="openai", resolve=h.resolve, call=h.call, report_failure=h.report_failure
        )

    assert h.resolved == 2, "默认预算 1 ⇒ 最多两把 key"
    assert h.reported == ["k1", "k2"]


async def test_first_try_success_does_not_report_or_retry() -> None:
    h = _Harness(["ok"])

    got = await kr.call_with_key_retry(
        provider="openai", resolve=h.resolve, call=h.call, report_failure=h.report_failure
    )

    assert got == "ok"
    assert h.resolved == 1 and h.reported == []
