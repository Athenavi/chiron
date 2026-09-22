"""per-run 预算（tokens / wall / cost）的判定与来源优先级。

钉住三条约定：
  1. 每轴 0 = 不限；全 0 时预算等于不存在（不参与判定）；
  2. 来源优先级：显式参数 > 环境变量 > 默认；
  3. 越界原因要能被 runner 直接当作 error 用（`budget_exceeded:<轴>`）。
"""
import time

from app.subagent.budget import (DEFAULT_MAX_TOKENS, BudgetExceeded, TaskBudget,
                                 from_env)


def test_全零表示不限():
    budget = TaskBudget(tokens=0, wall=0, cost=0.0)
    assert budget.enabled is False
    assert budget.exceeded(tokens=10 ** 9) == ""


def test_tokens_轴越界():
    budget = TaskBudget(tokens=1000, wall=0, cost=0.0)
    assert budget.enabled is True
    assert budget.exceeded(tokens=999) == ""
    assert budget.exceeded(tokens=1000) == "tokens"


def test_wall_轴越界():
    budget = TaskBudget(tokens=0, wall=10, cost=0.0)
    budget.started_at = time.time() - 11
    assert budget.exceeded(tokens=0) == "wall"
    budget.started_at = time.time()
    assert budget.exceeded(tokens=0) == ""


def test_cost_轴越界():
    budget = TaskBudget(tokens=0, wall=0, cost=1.5)
    assert budget.exceeded(tokens=0, cost=1.4) == ""
    assert budget.exceeded(tokens=0, cost=1.5) == "cost"


def test_多轴同时越界时优先报_tokens():
    budget = TaskBudget(tokens=10, wall=1, cost=1.0)
    budget.started_at = time.time() - 100
    assert budget.exceeded(tokens=10, cost=99) == "tokens"


def test_from_env_优先级(monkeypatch):
    # 环境变量生效
    monkeypatch.setenv("SUBAGENT_MAX_TOKENS", "1234")
    monkeypatch.setenv("SUBAGENT_MAX_SECONDS", "60")
    assert from_env().tokens == 1234
    assert from_env().wall == 60
    # 显式参数覆盖环境变量
    assert from_env(max_tokens=50, max_seconds=5).tokens == 50
    assert from_env(max_tokens=50, max_seconds=5).wall == 5


def test_from_env_非法值回落默认(monkeypatch):
    monkeypatch.setenv("SUBAGENT_MAX_TOKENS", "not-a-number")
    assert from_env().tokens == DEFAULT_MAX_TOKENS
    monkeypatch.delenv("SUBAGENT_MAX_TOKENS", raising=False)
    assert from_env().tokens == DEFAULT_MAX_TOKENS


def test_越界异常的消息即_error_字段():
    exc = BudgetExceeded("tokens")
    assert str(exc) == "budget_exceeded:tokens"
    assert exc.axis == "tokens"


def test_describe_便于排障():
    assert TaskBudget(tokens=100, wall=0, cost=0.0).describe() == "tokens<=100"
    assert TaskBudget(tokens=0, wall=0, cost=0.0).describe() == "none"
