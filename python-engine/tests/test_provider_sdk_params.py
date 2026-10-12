"""Provider SDK 参数兼容 —— **只发对方认的参数**（`app/providers/sdk_compat.py`）。

回归背景（真实故障）：b9d9566 把 python-engine 的 anthropic SDK 从 0.52.0 升到 1.13.0
后，`Messages.stream()` 不再接受 `temperature`（Anthropic 已废弃采样参数）。Chiron 此前
**无条件透传**，于是拿到的是 Python 层报错而非上游 400：

    provider opencode-go-anthropic stream failed:
    TypeError: AsyncMessages.stream() got an unexpected keyword argument 'temperature'

用户侧表现是"provider 配好了、模型也选对了，一发请求就失败"。

本文件用「签名里没有 temperature 的假 SDK」复现该形态（本地装不到 1.13.0，且真实 SDK
签名随时会再变），并锁住两条不变式：

1. 新 SDK 形态下**不炸**，且参数确实没有被下发（而不是"运气好没触发"）；
2. 旧 SDK 形态下**照发** temperature —— 兼容新 SDK 不能把老行为一起改掉。
"""

from types import SimpleNamespace
from typing import Any

from app.gateway.provider import ChatMessage
from app.providers.anthropic import AnthropicProvider
from app.providers.openai import OpenAIProvider
from app.providers.sdk_compat import drop_unsupported_params

USER = [ChatMessage(role="user", content="hi")]


# ── drop_unsupported_params 本体 ──────────────────────────


def test_unknown_keys_are_dropped_and_known_keys_kept():
    def fn(*, model: str, temperature: float | None = None) -> None: ...

    kwargs: dict[str, Any] = {"model": "m", "temperature": 0.5, "top_k": 3}
    drop_unsupported_params(fn, kwargs)
    assert kwargs == {"model": "m", "temperature": 0.5}, "top_k 不在签名里，必须被裁掉"


def test_var_keyword_signature_gets_everything():
    """签名带 `**kwargs` 时不该替 SDK 做决定 —— 放行，由 SDK 自己判定。"""

    def fn(**kwargs: Any) -> None: ...

    kwargs: dict[str, Any] = {"model": "m", "temperature": 0.5}
    drop_unsupported_params(fn, kwargs)
    assert kwargs == {"model": "m", "temperature": 0.5}


def test_unavailable_signature_gets_everything():
    """取不到签名（内建对象 / 深度装饰）时同样放行，避免误裁掉必要参数。"""
    kwargs: dict[str, Any] = {"temperature": 0.5}
    drop_unsupported_params(object(), kwargs)
    assert kwargs == {"temperature": 0.5}


# ── 假 SDK：两种签名形态 ───────────────────────────────────


class _Usage:
    input_tokens = 3
    output_tokens = 5


class _FinalMessage:
    content: list = []
    stop_reason = "end_turn"
    usage = _Usage()


class _FakeMessageStream:
    """`client.messages.stream()` 的返回物：异步上下文管理器 + 异步迭代器。"""

    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    async def __aenter__(self) -> "_FakeMessageStream":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def __aiter__(self):
        return self._events()

    async def _events(self):
        for event in ():  # 本用例只关心"参数有没有被下发"，事件序列留空
            yield event

    async def get_final_message(self) -> _FinalMessage:
        return _FinalMessage()


class _NewSdkMessages:
    """anthropic 1.x 的形态：`stream()` / `create()` 的签名里**没有** temperature。"""

    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}

    def stream(
        self,
        *,
        model: str,
        messages: list[Any],
        max_tokens: int,
        system: str | None = None,
        tools: list[Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> _FakeMessageStream:
        self.captured.update(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            extra_headers=extra_headers,
        )
        return _FakeMessageStream(self.captured)

    async def create(
        self,
        *,
        model: str,
        messages: list[Any],
        max_tokens: int,
        system: str | None = None,
        tools: list[Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        self.captured.update(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            extra_headers=extra_headers,
        )
        return SimpleNamespace(content=[], stop_reason="end_turn", usage=_Usage())


class _OldSdkMessages(_NewSdkMessages):
    """anthropic 0.x 的形态：采样参数还在签名里（必须继续照发）。"""

    async def create(  # type: ignore[override]
        self,
        *,
        model: str,
        messages: list[Any],
        max_tokens: int,
        temperature: float | None = None,
        system: str | None = None,
        tools: list[Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        resp = await super().create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            extra_headers=extra_headers,
        )
        self.captured["temperature"] = temperature
        return resp

    def stream(  # type: ignore[override]
        self,
        *,
        model: str,
        messages: list[Any],
        max_tokens: int,
        temperature: float | None = None,
        system: str | None = None,
        tools: list[Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> _FakeMessageStream:
        stream = super().stream(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            extra_headers=extra_headers,
        )
        self.captured["temperature"] = temperature
        return stream


class _FakeAnthropicClient:
    def __init__(self, messages: Any) -> None:
        self.messages = messages


def _anthropic_provider(messages: Any) -> AnthropicProvider:
    provider = AnthropicProvider(api_key="sk-fake-for-test")
    provider._client = _FakeAnthropicClient(messages)  # noqa: SLF001 — 测试替身
    return provider


# ── AnthropicProvider：新 SDK 形态不再炸 ────────────────────


async def test_new_sdk_chat_stream_omits_temperature():
    messages = _NewSdkMessages()
    provider = _anthropic_provider(messages)

    chunks = [c async for c in provider.chat_stream(USER, "claude-sonnet-4-5", temperature=0.3)]

    assert messages.captured["model"] == "claude-sonnet-4-5"
    assert messages.captured["max_tokens"] == 4096
    assert "temperature" not in messages.captured, "新 SDK 不接受 temperature，不能下发"
    assert chunks[-1].finish_reason == "stop"


async def test_new_sdk_chat_omits_temperature():
    messages = _NewSdkMessages()
    provider = _anthropic_provider(messages)

    resp = await provider.chat(USER, "claude-sonnet-4-5", temperature=0.3)

    assert "temperature" not in messages.captured
    assert resp.finish_reason == "stop"


async def test_old_sdk_still_receives_temperature():
    """旧 SDK（采样参数尚在）必须继续收到 temperature —— 兼容补丁不能改掉老行为。"""
    messages = _OldSdkMessages()
    provider = _anthropic_provider(messages)

    await provider.chat(USER, "claude-sonnet-4-5", temperature=0.3)
    assert messages.captured["temperature"] == 0.3

    _ = [chunk async for chunk in provider.chat_stream(USER, "claude-sonnet-4-5", temperature=0.7)]
    assert messages.captured["temperature"] == 0.7


# ── OpenAIProvider：同一次依赖升级的另一半 ──────────────────


class _NewSdkCompletions:
    """openai 3.x 形态：`create()` 签名里没有 temperature。"""

    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}

    async def create(
        self,
        *,
        model: str,
        messages: list[Any],
        max_tokens: int,
        stream: bool = False,
        tools: list[Any] | None = None,
        tool_choice: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        self.captured.update(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            stream=stream,
            tools=tools,
            tool_choice=tool_choice,
            extra_headers=extra_headers,
        )
        return self._chunks()

    async def _chunks(self):
        yield SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2))


class _FakeOpenAIClient:
    def __init__(self, completions: Any) -> None:
        self.chat = SimpleNamespace(completions=completions)


async def test_openai_provider_omits_temperature_on_new_sdk():
    completions = _NewSdkCompletions()
    provider = OpenAIProvider(api_key="sk-fake-for-test")
    provider._client = _FakeOpenAIClient(completions)  # noqa: SLF001 — 测试替身

    chunks = [c async for c in provider.chat_stream(USER, "gpt-5", temperature=0.3)]

    assert "temperature" not in completions.captured
    assert completions.captured["stream"] is True
    assert chunks[-1].output_tokens == 2
