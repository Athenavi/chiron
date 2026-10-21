"""M24 下半：引擎 cost 轴 —— 单价**由网关下发**，引擎按 token 换算并参与判定。

现场：`TaskBudget.exceeded()` 一直支持 `cost` 判定、`_budget_snapshot` 也报它的 limit，
但调用方**从不传 `used`** ⇒ cost 轴恒不触发（"机制就绪、没有输入"）。

三条不变量：
1. 换算口径与网关的 `DeductTokens` 一致（credits = tokens / 1000 × 单价）；
2. `_budget_snapshot` 报的是**真值**，不再是恒 0；
3. 网关没下发单价时为 0 ⇒ 该轴不触发（行为与 M24 之前一致，直连引擎也不会误拦）。
"""
from __future__ import annotations

from app.agent.runtime import AgentRuntime
from app.agent.task_budget import TaskBudget


def _runtime() -> AgentRuntime:
    return AgentRuntime(gateway=None)


def test_cost_axis_blocks_when_spend_reaches_the_limit() -> None:
    budget = TaskBudget(cost=1.0)

    assert budget.exceeded(cost=0.99) == ""
    assert budget.exceeded(cost=1.0) == "cost", "cost 轴必须能拦（此前 used 恒 0）"


def test_budget_snapshot_reports_the_real_cost() -> None:
    rt = _runtime()
    snap = rt._budget_snapshot(TaskBudget(cost=2.5), turns_used=1, tokens_used=10, cost_used=1.25)

    assert snap["cost"]["used"] == 1.25, "快照必须报真值，而不是恒 0"
    assert snap["cost"]["limit"] == 2.5


def test_budget_snapshot_defaults_cost_to_zero_without_a_price() -> None:
    """网关没下发单价（旧网关 / 直连引擎）⇒ 不编造数字，行为与 M24 之前一致。"""
    rt = _runtime()
    snap = rt._budget_snapshot(TaskBudget(cost=2.5), turns_used=1, tokens_used=10)

    assert snap["cost"]["used"] == 0.0
