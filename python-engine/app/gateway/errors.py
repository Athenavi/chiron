"""provider 错误归一（C2 段 b：上下文溢出识别）。

**为什么要归一**：上下文溢出是唯一"可自动恢复"的 LLM 错误 —— 压缩后重试通常就过了。
而各家的报法完全不同，不归一就只能靠调用点各写一套字符串匹配，必然漂移。

各家的实际形态：

* **OpenAI 系**：`error.code == "context_length_exceeded"`，或 message 含
  `maximum context length`；
* **Anthropic**：`invalid_request_error` + message 含 `prompt is too long`；
* **DeepSeek 等 OpenAI 兼容**：message 含 `maximum context length` / `too many tokens`。

**只重试一次**（调用方负责）：溢出恢复要重发整段上下文，成本翻倍；再失败说明压缩没救回来
（例如单条消息本身就超窗），继续重试只是烧钱。因此这里只回答"是不是溢出"，
"重试几次"由调用方按成本决定。
"""

from __future__ import annotations

#: 溢出标记表（大小写不敏感）。宁可少报也不要误报 —— 误报会把"限流/鉴权失败"当成溢出，
#: 于是白白重发一次昂贵的请求。
_OVERFLOW_MARKERS: tuple[str, ...] = (
    "context_length_exceeded",
    "maximum context length",
    "context window",
    "prompt is too long",
    "too many tokens",
    "reduce the length",
    "input is too long",
)


def is_context_overflow(exc: BaseException, provider: str = "") -> bool:
    """判断异常是否为"上下文超出窗口"。

    Args:
        exc: provider 抛出的异常（可能是 SDK 的封装异常，错误体挂在属性上）。
        provider: 调用方已知的 provider 标识。当前仅用于日志/未来按家分支；
            判定走统一的标记表 —— 各家标记互不冲突，按 provider 分支反而容易漏。

    Returns:
        是否为可恢复的上下文溢出。
    """
    text = _flatten(exc).lower()
    return any(marker in text for marker in _OVERFLOW_MARKERS)


def _flatten(exc: BaseException) -> str:
    """把异常压成一段可搜索的文本（含嵌套错误体与 cause 链）。

    不只看 `str(exc)`：OpenAI/Anthropic 的 SDK 把真正的错误码放在
    `exc.body` / `exc.code` / `exc.response` 上，`str(exc)` 往往只有一句笼统的提示。
    """
    parts: list[str] = [type(exc).__name__, str(exc)]
    for attr in ("body", "code", "error", "message", "response", "status_code"):
        value = getattr(exc, attr, None)
        if value is not None:
            parts.append(str(value))
    cause = exc.__cause__
    if cause is not None:
        parts.append(_flatten(cause))
    return " ".join(parts)
