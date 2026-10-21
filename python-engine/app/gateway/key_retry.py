"""换 key 重试（M23）：一次请求内遇到坏 key / 限流时，换一把 key 再试一次。

**现场**：KeyRing V2 有严格 LRU 轮转与软/硬冷却，但 provider 层失败后只
`report_failure`（让**下一次请求**换 key），同一次请求直接失败或走 provider 级降级
（用户可见地换了模型）。而 401/403（key 本身失效）、429（该 key 限流）、5xx 这几类，
**换一把 key 往往立刻就好** —— 用户不该为此看到失败或模型降级。

**两条硬约束**（来自 gRPC / Google AIP-194 的透明重试与 LiteLLM 的 `max_retries=0`）：

* **提交点**：只在"还没向调用方产出任何东西"之前换 key。对 `chat_stream` 来说，
  提交点就是 `create()` **成功返回**那一刻 —— 一旦开始吐 chunk，重试就会重复输出与
  重复计费。所以本模块只包住那一次 await，绝不包 chunk 循环。
* **预算**：默认最多换 **1** 把 key（`KEY_RETRY_MAX`，0 = 关）。上限低是刻意的：
  重试会放大上游压力，而 LiteLLM 把 `max_retries` 钉成 0 正是因为两层重试会叠成
  `(1+N)²` 的放大。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: 一次请求最多换几把 key 的默认值（0 = 关掉整个机制）。
DEFAULT_KEY_RETRY_MAX = 1

#: 值得**换 key** 的状态码：key 本身失效（401/403）、该 key 限流（429）、
#: 请求超时/冲突（408/409，通常与这一把 key 的链路有关）；5xx 单独按区间判。
RETRYABLE_STATUS = frozenset({401, 403, 408, 409, 429})

#: 传输类异常（换一把 key 通常换了一条链路）。按**类名**判，不 import 各家 SDK：
#: 引擎同时接 openai / anthropic / httpx 三套异常层次，逐个 import 只会把耦合写进这里。
_TRANSPORT_HINTS = ("timeout", "connection", "connecterror", "transport", "remoteprotocol")


def key_retry_max() -> int:
    """一次请求最多换几把 key（0 = 关）。配置读不到时用默认值，**不**抛。"""
    try:
        from app.config import settings

        raw = getattr(settings, "key_retry_max", DEFAULT_KEY_RETRY_MAX)
        return max(0, int(raw))
    except Exception:  # noqa: BLE001 — 配置读取失败不该让调用路径失败
        return DEFAULT_KEY_RETRY_MAX


def _status_of(exc: BaseException) -> int | None:
    """从异常里取 HTTP 状态码（各家 SDK 的字段名不同，逐级尝试）。"""
    for attr in ("status_code", "http_status", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    return None


def is_retryable_key_error(exc: BaseException) -> bool:
    """这个异常是否值得**换一把 key** 再试一次。

    不重试的一类同样重要：400/422（请求本身错）、上下文超限、内容策略 —— 换 key
    结果一样，重试只是白花一次调用并掩盖真正的错误。
    """
    status = _status_of(exc)
    if status is not None:
        return status in RETRYABLE_STATUS or 500 <= status < 600
    name = type(exc).__name__.lower()
    return any(hint in name for hint in _TRANSPORT_HINTS)


def _count(provider: str, outcome: str) -> None:
    """打点失败绝不影响调用路径（指标是观测，不是前置条件）。"""
    try:
        from app.observability.metrics import LLM_KEY_RETRY

        LLM_KEY_RETRY.labels(provider=provider, outcome=outcome).inc()
    except Exception:  # noqa: BLE001
        pass


async def call_with_key_retry(
    *,
    provider: str,
    resolve: Callable[[], Awaitable[tuple[Any, Any]]],
    call: Callable[[Any], Awaitable[T]],
    report_failure: Callable[[Any], Awaitable[None]],
) -> T:
    """取 key → 调用；可重试的错误且预算未用尽 ⇒ 换 key 再试一次。

    `resolve()` 每次都会重新取 key（KeyRing 的 LRU + 刚上报的冷却会让它换到另一把）；
    `call(client)` 必须是**尚未向调用方产出任何东西**的那一次 await（见模块文档的"提交点"）。

    失败路径：先把旧 key 上报冷却（**无论**是否重试都要报 —— 那是跨实例共享的事实），
    再决定换不换。预算用尽或错误不可重试时原样抛出，调用方（provider → router）仍可按
    既有语义降级。
    """
    attempts = key_retry_max() + 1
    for attempt in range(attempts):
        client, item = await resolve()
        try:
            result = await call(client)
        except Exception as exc:
            await report_failure(item)
            can_retry = item is not None and attempt + 1 < attempts and is_retryable_key_error(exc)
            if not can_retry:
                _count(provider, "skipped")
                raise
            logger.warning(
                "key retry: %s 的第 %d 把 key 失败（%s），换 key 再试一次",
                provider,
                attempt + 1,
                type(exc).__name__,
            )
            _count(provider, "retried")
            continue
        if attempt:
            _count(provider, "ok")
        return result
    # 循环必然以 return 或 raise 收尾；这里只是让类型收敛。
    raise RuntimeError("key retry: unreachable")
