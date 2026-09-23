"""子 Agent 的 per-run 预算 —— 实现已上移到 :mod:`app.agent.task_budget`（与主 Agent 共用一套）。

保留本模块只为不破坏既有 import 路径（``subagent_runner`` / 测试），并固定**子 Agent 自己的**
阈值来源：环境变量前缀 ``SUBAGENT_*``、token 默认 200k（主 Agent 是 400k）。

语义不变：**越界是"失败"而不是"取消"** —— runner 抛 :class:`BudgetExceeded`，走既有失败收尾
（``status=failed``、``error=budget_exceeded:<轴>``）。
"""

from __future__ import annotations

from app.agent.task_budget import (  # noqa: F401 - 供既有调用方复用
    BudgetExceeded,
    TaskBudget,
)

#: 子 Agent 的 token 上界：只在"失控"时兜底（实测一个正常 run 约 2k–20k token，200k 很宽松）
DEFAULT_MAX_TOKENS = 200_000
#: wall 默认不限：该不该限时由调用方/看门狗决定（看门狗见 app/subagent/registry.py）
DEFAULT_MAX_SECONDS = 0
#: cost 默认不限：计价口径随 provider 变，交给上层显式传
DEFAULT_MAX_COST = 0.0


def from_env(*, max_tokens: int = 0, max_seconds: int = 0, max_cost: float = 0.0) -> TaskBudget:
    """按「显式参数 > 环境变量（SUBAGENT_*）> 默认」构造预算。"""
    from app.agent.task_budget import from_env as _from_env

    return _from_env(
        prefix="SUBAGENT",
        tokens=max_tokens,
        seconds=max_seconds,
        cost=max_cost,
        default_tokens=DEFAULT_MAX_TOKENS,
        # 子 Agent 不要步数轴：它的步数由 `max_turns` 与注册表看门狗管
        default_steps=0,
    )
