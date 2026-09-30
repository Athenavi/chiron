"""结构化结局与错误分类（R3，方案见 `vendor/规划.md` §4.5）。

## 为什么需要它

在 R3 之前，"这次失败能不能重试"是**拼接字符串**的副产物：`SubagentRunResult.error` 是
`" | ".join(errors)`，而 `retryable` 由 `status in ("failed", "lost")` 粗判 —— 于是
"上下文溢出"（压缩后可重试）与"空任务"（重试一万次也一样）被归成同一类。

对齐的参照是 Reasonix 的 `subagent_outcome.go` + `subagentErrorDisposition`：把"发生了什么"变成
**机器可读的码**，把"能不能重试"变成**可判定的结论**。它还带一点值得照抄的取舍：错误对象**保留部分
答案与引用**（`SubagentRunError.SubagentOutput()`）—— 失败不等于什么都没产出。

## 一条安全默认

**未知错误一律不可重试。** 重试要花 token 与钱，还可能重复副作用（写文件、发请求）。所以"拿不准"
必须落在"不重试"这一侧 —— 让调用方**主动**决定要不要重来，而不是我们替它冒险。

## 与 A5 的关系

A5 已经把 `retryable` 落进 `subagent_runs`（迁移 0006）。本模块是**它的判定来源**：runner 不再自己
判，而是调 `classify_error()` —— 这样"落库的那个值"与"调用方看到的那个值"**同源**，不会出现两套判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ── 错误码（稳定取值，落库到 subagent_runs.error_code）──────────────────
#
# 为什么是**有限枚举**而不是自由文本：调用方要能对它做分支（重试 / 换模型 / 报给用户），
# 而自由文本只能被"读"，不能被"判断"。
CODE_CANCELLED = "cancelled"
CODE_BUDGET_EXCEEDED = "budget_exceeded"
CODE_CONTEXT_OVERFLOW = "context_overflow"
CODE_PROVIDER_RETRY = "provider_retry"
CODE_TIMEOUT = "timeout"
CODE_TOOL_FAILURE = "tool_failure"
CODE_INVALID_INPUT = "invalid_input"
CODE_NOT_FOUND = "not_found"
CODE_UNKNOWN = "unknown"

ERROR_CODES: tuple[str, ...] = (
    CODE_CANCELLED,
    CODE_BUDGET_EXCEEDED,
    CODE_CONTEXT_OVERFLOW,
    CODE_PROVIDER_RETRY,
    CODE_TIMEOUT,
    CODE_TOOL_FAILURE,
    CODE_INVALID_INPUT,
    CODE_NOT_FOUND,
    CODE_UNKNOWN,
)

#: 哪些码**值得重试**。其余一律不重试（含 `unknown`）—— 见模块文档的"安全默认"。
RETRYABLE_CODES: frozenset[str] = frozenset(
    {
        CODE_CONTEXT_OVERFLOW,  # 压缩之后可重试
        CODE_PROVIDER_RETRY,  # provider 明确说了"稍后再来"
        CODE_TIMEOUT,  # 常是偶发（上游挂起 / 排队）
    }
)


def _is_budget_exceeded(exc: BaseException) -> bool:
    try:
        from app.subagent.budget import BudgetExceeded
    except Exception:  # noqa: BLE001 — 拿不到类型时退化为名字匹配（不能因此失去分类能力）
        return type(exc).__name__ == "BudgetExceeded"
    return isinstance(exc, BudgetExceeded)


def _is_tool_error(exc: BaseException) -> bool:
    try:
        from app.tools.run_code import ToolCallError
    except Exception:  # noqa: BLE001
        return type(exc).__name__ == "ToolCallError"
    return isinstance(exc, ToolCallError)


def classify_error(exc: BaseException | None) -> tuple[str, bool]:
    """把异常映射成 `(error_code, retryable)`。`None` 视为未知（**不重试**）。

    顺序有意义：先判**最具体**的（取消 > 预算 > 溢出 > provider > 工具），再落未知。
    `context_overflow` 放在 provider 重试之前，因为溢出是 provider 重试的**特例**——
    它的处置不同（要先压缩，而不是原样重发）。
    """
    if exc is None:
        return CODE_UNKNOWN, False

    import asyncio

    # 取消不是"失败"：它是**被停掉**的。重试等于违逆调用方的意愿。
    if isinstance(exc, asyncio.CancelledError):
        return CODE_CANCELLED, False

    if _is_budget_exceeded(exc):
        return CODE_BUDGET_EXCEEDED, False

    # provider 侧：溢出要"压缩后重试"，与"稍后原样重试"不是一回事
    try:
        from app.gateway.errors import is_context_overflow

        if is_context_overflow(exc):
            return CODE_CONTEXT_OVERFLOW, True
    except Exception:  # noqa: BLE001 — 判定不可用时跳过，不影响其余分类
        pass

    if type(exc).__name__ == "RetryLaterError":
        return CODE_PROVIDER_RETRY, True

    if _is_tool_error(exc):
        # 工具报的错（参数非法 / 沙箱拒绝）不会因为重试而变好
        return CODE_TOOL_FAILURE, False

    if isinstance(exc, TimeoutError):
        return CODE_TIMEOUT, True

    if isinstance(exc, (ValueError, TypeError, KeyError)):
        # 任务本身有问题（空任务 / 参数不合法）——重试无意义
        return CODE_INVALID_INPUT, False

    return CODE_UNKNOWN, False


@dataclass(frozen=True)
class SubagentOutcome:
    """一次委派的**结构化结局**。

    与 `SubagentRunResult` 的分工：后者是"给我看的结果"（output/summary/用量），本对象是"给机器判的
    结论"（码 / 可否重试 / 部分产出是否可用）。两者可以并存 —— `from_result()` 从前者派生后者。
    """

    status: str
    error_code: str = CODE_UNKNOWN
    retryable: bool = False
    #: 失败时**仍然可用的部分产出**（对位 Reasonix 的 `SubagentRunError.SubagentOutput()`）。
    #: 失败不等于什么都没产出 —— 丢掉它等于把已经花掉的 token 一起丢掉。
    partial_output: str = ""
    #: 结果引用（run_id），供调用方回查。
    ref: str = ""
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status in ("failed", "lost")

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "error_code": self.error_code,
            "retryable": self.retryable,
            "partial_output": self.partial_output,
            "result_ref": self.ref,
            "detail": self.detail,
        }


#: 状态 → 默认错误码（没有异常可依据时用）。`partial` 与 `cancelled` 都不是"错误"，但要有码。
_STATUS_DEFAULT_CODES: dict[str, str] = {
    "completed": "",
    "partial": "",
    "cancelled": CODE_CANCELLED,
    "failed": CODE_UNKNOWN,
    "lost": CODE_TIMEOUT,  # 孤儿收口：进程没了，等价于超时
}


def outcome_of(
    *,
    status: str,
    run_id: str = "",
    error: str = "",
    partial_output: str = "",
    exc: BaseException | None = None,
) -> SubagentOutcome:
    """组装结局。有异常时以异常为准，否则按 `status` 取默认码。"""
    if exc is not None:
        code, retryable = classify_error(exc)
    else:
        code = _STATUS_DEFAULT_CODES.get(status, CODE_UNKNOWN)
        retryable = code in RETRYABLE_CODES and status in ("failed", "lost")
    # 成功/部分完成不该被标成"可重试的失败"
    if status in ("completed", "partial"):
        retryable = False
    return SubagentOutcome(
        status=status,
        error_code=code,
        retryable=retryable,
        partial_output=partial_output,
        ref=run_id,
        detail=(error or "")[:200],
    )


def format_outcome(outcome: SubagentOutcome) -> str:
    """一行式摘要（日志与工具结果用）。码在前、人能读的细节在后。"""
    parts = [f"status={outcome.status}", f"code={outcome.error_code or '-'}"]
    parts.append("retryable=yes" if outcome.retryable else "retryable=no")
    if outcome.detail:
        parts.append(outcome.detail)
    return " ".join(parts)


__all__ = [
    "CODE_BUDGET_EXCEEDED",
    "CODE_CANCELLED",
    "CODE_CONTEXT_OVERFLOW",
    "CODE_INVALID_INPUT",
    "CODE_NOT_FOUND",
    "CODE_PROVIDER_RETRY",
    "CODE_TIMEOUT",
    "CODE_TOOL_FAILURE",
    "CODE_UNKNOWN",
    "ERROR_CODES",
    "RETRYABLE_CODES",
    "SubagentOutcome",
    "classify_error",
    "format_outcome",
    "outcome_of",
]
