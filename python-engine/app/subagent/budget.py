"""子 Agent 的 per-run 预算 —— 三轴：tokens / wall / cost。

对应 Reasonix 的 ``TaskBudget``（`vendor/DeepSeek-Reasonix/internal/agent/run_budget.go:18-21`，
同样是三轴 ``{Cost, Wall, Tokens}``）。**每轴 0 = 不限**，与那边"每个轴默认关闭"的取向一致：
默认只拦"明显失控"，而不是替用户决定任务该跑多久。

为什么必须有它（我们此前的真空）：子 Agent 一轮可以吃掉几十万 token，而唯一的限制是
``max_turns``（≤10）—— 单轮多长完全不受约束；`followup` 自动轮又会派新的子 Agent，于是
**成本没有任何上界**。这里的预算就是给"每个子 agent run"装一个刹车。

触发时的语义：**不是"取消"，而是"失败"** —— 越界由 runner 抛 :class:`BudgetExceeded`，
走既有的失败收尾（status=failed、error=``budget_exceeded:<轴>``），前端能看到原因。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

#: 默认 tokens 上界：只在"失控"时兜底（实测一个正常 run 约 2k–20k token，200k 很宽松）
DEFAULT_MAX_TOKENS = 200_000
#: wall 默认不限：该不该限时由调用方/看门狗决定（看门狗见 app/subagent/registry.py）
DEFAULT_MAX_SECONDS = 0
#: cost 默认不限：计价口径随 provider 变，交给上层显式传
DEFAULT_MAX_COST = 0.0


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def from_env(*, max_tokens: int = 0, max_seconds: int = 0, max_cost: float = 0.0) -> "TaskBudget":
    """按「显式参数 > 环境变量 > 默认」构造预算。

    显式传 0 表示"用默认"（工具层这样调用最自然：不传就是默认策略）。
    """
    tokens = int(max_tokens or 0) or _env_int("SUBAGENT_MAX_TOKENS", DEFAULT_MAX_TOKENS)
    wall = int(max_seconds or 0) or _env_int("SUBAGENT_MAX_SECONDS", DEFAULT_MAX_SECONDS)
    cost = float(max_cost or 0.0) or float(_env_int("SUBAGENT_MAX_COST", int(DEFAULT_MAX_COST)))
    return TaskBudget(tokens=tokens, wall=wall, cost=cost)


@dataclass
class TaskBudget:
    """一次子 Agent run 的预算（0 = 该轴不限）。"""

    tokens: int = DEFAULT_MAX_TOKENS
    wall: int = DEFAULT_MAX_SECONDS
    cost: float = DEFAULT_MAX_COST
    started_at: float = field(default_factory=time.time)

    @property
    def enabled(self) -> bool:
        return bool(self.tokens) or bool(self.wall) or bool(self.cost)

    def elapsed(self) -> float:
        return time.time() - self.started_at

    def exceeded(self, *, tokens: int, cost: float = 0.0) -> str:
        """返回越界的轴名（``tokens`` / ``wall`` / ``cost``），未越界返回空串。

        判定顺序固定（tokens → wall → cost）：同时越界时给出更"贵"的那个原因，
        便于前端解释"为什么被中止"。
        """
        if self.tokens and tokens >= self.tokens:
            return "tokens"
        if self.wall and self.elapsed() >= self.wall:
            return "wall"
        if self.cost and cost >= self.cost:
            return "cost"
        return ""

    def describe(self) -> str:
        """人类可读摘要（写进日志/事件，便于排障）。"""
        parts = []
        if self.tokens:
            parts.append(f"tokens<={self.tokens}")
        if self.wall:
            parts.append(f"wall<={self.wall}s")
        if self.cost:
            parts.append(f"cost<={self.cost}")
        return ",".join(parts) or "none"


class BudgetExceeded(RuntimeError):
    """预算越界。消息即 ``budget_exceeded:<轴>`` —— 会被 runner 写进 run 的 error。"""

    def __init__(self, axis: str):
        super().__init__(f"budget_exceeded:{axis}")
        self.axis = axis
