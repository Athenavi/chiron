"""recovery — 工具失败后的**恢复路由**（P5）。

对照实现：Reasonix `internal/runtime/recovery/decision.go` 的 `Decide` ——
"纯函数、无锁无模型，7 条固定优先级路由"，且注释强调
**"Nothing here counts attempts"**：它不靠"重试 N 次就熔断"这种计数，而是靠
**失败分类**（同一个失败操作 / 扩大了范围 / 换了策略 / 还有安全重试可用）来路由。

Chiron 此前只有 `app/agent/loop_guard.py` 的**检测**（重复调用 / 无进展 → `LoopVerdict`），
检测出来之后**没有恢复动作**：既不停、也不换策略，只是把一句提示回灌给模型，
于是"卡在同一个失败操作上反复重试"依旧要烧到预算上限才停。

本模块补上"检测 → 决策"的那一半，并保持与 loop_guard 相同的可推理性质：

* **纯函数**：``decide`` 不读时钟、不读网络、不改状态；调用方把状态传进来。
* **fail-closed**：拿不准时返回 ``REVIEW``（要求换一个做法），而不是静默放行重试。
* **指纹而非计数**：判据是"这次调用是不是**同一个失败操作**"，不是"试了几次"。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: 同一失败操作在**换策略之前**允许的自动重试次数（超过即要求评审）。
SAFE_RETRY_BUDGET = 1

#: 只读类动作级别 —— 它们的失败不构成"破坏性失败"，永远可以直接换个读法重试。
_READONLY_LEVELS = frozenset({"read"})


class Action(StrEnum):
    """恢复动作。"""

    #: 直接放行（继续执行）。
    ALLOW = "allow"
    #: 要求**换一个做法**：把失败与"同一操作已被拒"的事实回灌给模型，让它改变策略。
    REVIEW = "review"
    #: 明确拒绝（用于确认会造成破坏的重复操作）。
    DENY = "deny"


class FailureKind(StrEnum):
    """这次调用相对"上一个失败"的关系。"""

    NONE = "none"  # 没有已知的失败
    SAME_OPERATION = "same-operation"  # 与上一个失败是同一次操作（指纹相同）
    DIFFERENT_OPERATION = "different-operation"  # 换了操作/参数
    EXPANDED_SCOPE = "expanded-scope"  # 同一工具但作用范围更大（更危险）

    @property
    def is_repeat(self) -> bool:
        return self is FailureKind.SAME_OPERATION


@dataclass(frozen=True)
class Failure:
    """一次失败的最小记录。"""

    fingerprint: str
    tool_name: str
    level: str
    reason: str = ""


@dataclass
class Decision:
    """一次恢复决策。"""

    action: Action
    kind: FailureKind
    reason: str


@dataclass
class RecoveryState:
    """会话内的恢复状态（**由调用方持有** —— 本模块不存全局状态）。"""

    #: 上一个失败（None = 没有 active failure）。
    last_failure: Failure | None = None
    #: 针对 ``last_failure.fingerprint`` 已消耗的安全重试次数。
    retries_used: int = 0
    #: 已 REVIEW 过的指纹（同一个操作被要求换策略后，别无限重复地再要求一次）。
    reviewed: set[str] = field(default_factory=set)

    def record_failure(self, failure: Failure) -> None:
        """记录一次失败。**同一指纹**保留计数（它就是在重试同一个操作）。"""
        if self.last_failure is not None and self.last_failure.fingerprint == failure.fingerprint:
            self.retries_used += 1
        else:
            self.retries_used = 0
        self.last_failure = failure

    def record_success(self) -> None:
        """成功即清除 active failure —— 失败已被克服。"""
        self.last_failure = None
        self.retries_used = 0


def fingerprint(tool_name: str, args: dict[str, Any] | None) -> str:
    """对"这次操作"取指纹：**同一工具 + 等价参数**视为同一个操作。

    参数按键排序后规范化，避免键序不同被当成不同操作（那会绕过重复判定）。
    """
    try:
        normalized = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        normalized = repr(args)
    raw = f"{tool_name}\x00{normalized}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def decide(
    *,
    tool_name: str,
    level: str,
    args: dict[str, Any] | None,
    state: RecoveryState,
) -> Decision:
    """决定"这次工具调用是否放行"。

    优先级（自上而下，先命中先返回）：

    1. 只读工具：永远放行 —— 读操作失败换个读法再试是正常探索，不构成破坏。
    2. 没有 active failure：放行（没什么要恢复的）。
    3. 换了操作（指纹不同）：放行 —— 换了做法本身就可能是修复。
    4. 同一个失败操作 + 还有安全重试预算：放行并消耗一次预算。
    5. 否则（同一个失败操作、预算已尽）：REVIEW —— 明确要求换策略。
    """
    if level in _READONLY_LEVELS:
        return Decision(Action.ALLOW, FailureKind.NONE, "read-only action: retry is safe")

    last = state.last_failure
    if last is None:
        return Decision(Action.ALLOW, FailureKind.NONE, "no active failure to recover from")

    fp = fingerprint(tool_name, args)
    if fp != last.fingerprint:
        return Decision(
            Action.ALLOW,
            FailureKind.DIFFERENT_OPERATION,
            "a different operation was proposed (previous failure does not apply)",
        )

    if state.retries_used < SAFE_RETRY_BUDGET and fp not in state.reviewed:
        return Decision(
            Action.ALLOW,
            FailureKind.SAME_OPERATION,
            f"safe retry #{state.retries_used + 1} of the same failed operation",
        )

    state.reviewed.add(fp)
    return Decision(
        Action.REVIEW,
        FailureKind.SAME_OPERATION,
        (
            "the same operation already failed and its retry budget is spent — "
            f"change the approach (previous failure: {last.reason[:160]})"
        ),
    )
