"""任务预算 —— 主 Agent 与子 Agent **共用一套**实现。

**为什么需要它**

子 Agent 早就有 tokens / wall / cost 三轴预算（`SUBAGENT_MAX_*`），主 Agent 却只有 `max_turns`
一个轮次上限：一轮里可以塞任意多次工具调用，token 与成本没有任何引擎侧上限（唯一兜底是网关
的回合超时）。于是"主 Agent 卡在循环里烧钱"根本没有闸门。

**五轴**

| 轴 | 含义 | 主 Agent 默认 |
|---|---|---|
| ``steps`` | 工具调用总次数 —— `max_turns` 是"轮"，一轮内可调很多次工具，这是最直接的漏口 | 60 |
| ``turns`` | LLM 往返轮数（即既有 `max_turns`，保留） | 0（由 task.max_turns 管） |
| ``wall``  | 墙钟（秒） | 0 = 不限（由网关回合超时兜底） |
| ``tokens`` | input + output 累计 | 400k |
| ``cost``  | 按 provider 计价换算 | 0 = 不限 |

**语义（与子 Agent 一致）**：超限是"**失败**"而不是"取消" —— 走失败收尾
（`status=failed`、`error=budget_exceeded:<轴>`），前端能看到原因，而不是静默中断。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

#: 主 Agent 步数上限：一轮里 20 次工具调用是常态，60 足够跑完多轮工具链
DEFAULT_MAX_STEPS = 60
#: 主 Agent token 上限：只拦"明显失控"（正常长任务在数十万 token 量级）
DEFAULT_MAX_TOKENS = 400_000
#: wall 默认不限：该不该限时由调用方 / 网关的回合超时决定，引擎侧不重复限制
DEFAULT_MAX_SECONDS = 0
#: 成本默认不限：计价口径随 provider 变，交给上层显式传
DEFAULT_MAX_COST = 0.0


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass
class TaskBudget:
    """一次 agent run 的预算（每轴 0 = 该轴不限）。"""

    #: 工具调用总次数上限。**dataclass 默认 0（不限）**，由 :func:`from_env` 决定默认值：
    #: 主 Agent 60、子 Agent 0（子 Agent 的步数由 `max_turns` 与看门狗管，不需要这一轴）。
    steps: int = 0
    turns: int = 0
    tokens: int = DEFAULT_MAX_TOKENS
    wall: int = DEFAULT_MAX_SECONDS
    cost: float = DEFAULT_MAX_COST
    started_at: float = field(default_factory=time.time)

    @property
    def enabled(self) -> bool:
        return bool(self.steps or self.turns or self.tokens or self.wall or self.cost)

    def elapsed(self) -> float:
        return time.time() - self.started_at

    def exceeded(
        self, *, steps: int = 0, turns: int = 0, tokens: int = 0, cost: float = 0.0
    ) -> str:
        """返回越界的轴名（``steps`` / ``turns`` / ``tokens`` / ``wall`` / ``cost``）；未越界返回空串。

        判定顺序固定（steps → turns → tokens → wall → cost）：同时越界时给出"最直接可归因"的
        那个轴，便于前端解释"为什么被中止"。
        """
        if self.steps and steps >= self.steps:
            return "steps"
        if self.turns and turns >= self.turns:
            return "turns"
        if self.tokens and tokens >= self.tokens:
            return "tokens"
        if self.wall and self.elapsed() >= self.wall:
            return "wall"
        if self.cost and cost >= self.cost:
            return "cost"
        return ""

    def describe(self) -> str:
        """人类可读摘要（写日志 / 事件，便于排障）。"""
        parts = []
        if self.steps:
            parts.append(f"steps<={self.steps}")
        if self.turns:
            parts.append(f"turns<={self.turns}")
        if self.tokens:
            parts.append(f"tokens<={self.tokens}")
        if self.wall:
            parts.append(f"wall<={self.wall}s")
        if self.cost:
            parts.append(f"cost<={self.cost}")
        return ",".join(parts) or "none"


class BudgetExceeded(RuntimeError):  # noqa: N818 — 名字由 subagent/budget.py 的 re-export 与 runner 的 error 文本固定，改名属 API 变更
    """预算越界。消息即 ``budget_exceeded:<轴>`` —— 会被 runner 写进 run 的 error。"""

    def __init__(self, axis: str):
        super().__init__(f"budget_exceeded:{axis}")
        self.axis = axis


def from_env(
    prefix: str = "AGENT",
    *,
    steps: int = 0,
    turns: int = 0,
    tokens: int = 0,
    seconds: int = 0,
    cost: float = 0.0,
    default_tokens: int = DEFAULT_MAX_TOKENS,
    default_steps: int = DEFAULT_MAX_STEPS,
) -> TaskBudget:
    """按「显式参数 > 环境变量（``{prefix}_MAX_*``）> 默认」构造预算。

    显式传 0 表示"用默认"（调用方不传就是默认策略）。主 Agent 用默认前缀 ``AGENT``；
    子 Agent 传 ``prefix="SUBAGENT"``、自己的 token 默认值，以及 ``default_steps=0``
    （它不需要步数轴）—— 同一个实现、两套阈值来源，避免两份会各自漂移的预算代码。
    """
    return TaskBudget(
        steps=int(steps or 0) or _env_int(f"{prefix}_MAX_STEPS", default_steps),
        turns=int(turns or 0) or _env_int(f"{prefix}_MAX_TURNS", 0),
        tokens=int(tokens or 0) or _env_int(f"{prefix}_MAX_TOKENS", default_tokens),
        wall=int(seconds or 0) or _env_int(f"{prefix}_MAX_SECONDS", DEFAULT_MAX_SECONDS),
        cost=float(cost or 0.0) or float(_env_int(f"{prefix}_MAX_COST", int(DEFAULT_MAX_COST))),
    )
