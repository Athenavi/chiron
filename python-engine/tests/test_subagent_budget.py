"""per-run 预算（tokens / wall / cost）的判定与来源优先级。

钉住三条约定：
  1. 每轴 0 = 不限；全 0 时预算等于不存在（不参与判定）；
  2. 来源优先级：显式参数 > 环境变量 > 默认；
  3. 越界原因要能被 runner 直接当作 error 用（`budget_exceeded:<轴>`）。
"""
import time

from app.subagent.budget import DEFAULT_MAX_TOKENS, BudgetExceeded, TaskBudget, from_env


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
    monkeypatch.setenv("SUBAGENT_RUN_MAX_TOKENS", "1234")
    monkeypatch.setenv("SUBAGENT_MAX_SECONDS", "60")
    assert from_env().tokens == 1234
    assert from_env().wall == 60
    # 显式参数覆盖环境变量
    assert from_env(max_tokens=50, max_seconds=5).tokens == 50
    assert from_env(max_tokens=50, max_seconds=5).wall == 5


def test_from_env_非法值回落默认(monkeypatch):
    monkeypatch.setenv("SUBAGENT_RUN_MAX_TOKENS", "not-a-number")
    assert from_env().tokens == DEFAULT_MAX_TOKENS
    monkeypatch.delenv("SUBAGENT_RUN_MAX_TOKENS", raising=False)
    assert from_env().tokens == DEFAULT_MAX_TOKENS


def test_越界异常的消息即_error_字段():
    exc = BudgetExceeded("tokens")
    assert str(exc) == "budget_exceeded:tokens"
    assert exc.axis == "tokens"


def test_describe_便于排障():
    assert TaskBudget(tokens=100, wall=0, cost=0.0).describe() == "tokens<=100"
    assert TaskBudget(tokens=0, wall=0, cost=0.0).describe() == "none"


# ── token 轴：窗口自适应与越界续段 ───────────────────────────────────────────
#
# 背景：token 轴按「累计重发历史」计数 —— 每轮把完整历史重发一次，第 n 轮的 input 随 n
# 线性增长，累计约呈 n²。单轮 5k→60k 时第 40 轮累计约 1.3M，而旧的固定上限是 400k：
# 于是 40 轮的任务在第 12–15 轮就报 `budget_exceeded:tokens`，后面 25 轮用不到。
# 更糟的是越界走 failed 收尾，而自动续跑继承已用量 ⇒ 接上来立刻再越界，永远跑不完。


def test_scale_to_window_按窗口放大且不缩小(monkeypatch):
    from app.agent.task_budget import DEFAULT_MAX_TOKENS, TOKENS_PER_WINDOW

    # 大窗口：放大到 窗口 × TOKENS_PER_WINDOW
    big = TaskBudget(tokens=DEFAULT_MAX_TOKENS)
    big.scale_to_window(1_000_000)
    assert big.tokens == 1_000_000 * TOKENS_PER_WINDOW

    # 小窗口：**不缩小**，仍是地板值（128k 的模型不该被放大，也不该被压低）
    small = TaskBudget(tokens=DEFAULT_MAX_TOKENS)
    small.scale_to_window(128_000)
    assert small.tokens == DEFAULT_MAX_TOKENS

    # 未知窗口（0）⇒ 不动
    unknown = TaskBudget(tokens=DEFAULT_MAX_TOKENS)
    unknown.scale_to_window(0)
    assert unknown.tokens == DEFAULT_MAX_TOKENS


def test_scale_to_window_步长固定在窗口值上():
    """续段步长不能跟着上限涨 —— 否则 8 段下来是指数膨胀。"""
    budget = TaskBudget(tokens=400_000)
    budget.scale_to_window(200_000)
    assert budget.token_step == 200_000
    budget.grant_next_segment()
    before = budget.token_step
    budget.scale_to_window(200_000)  # 每轮开头都会调用一次
    assert budget.token_step == before


def test_grant_next_segment_只抬上限不动已用量():
    budget = TaskBudget(tokens=1000)
    budget.scale_to_window(1000, floor=1000)  # 上限 → max(地板, 窗口×3) = 3000，步长 = 1000
    assert budget.tokens == 3000
    assert budget.grant_next_segment() is True
    assert budget.tokens == 4000  # 只加一个**窗口**步长，不是翻倍
    assert budget.segments == 1
    # 上限变了，但"已用"是计费口径 —— 它不在 budget 里，由调用方传入且保持累加。
    assert budget.exceeded(tokens=1500) == ""


def test_grant_next_segment_段数用尽后不再续():
    from app.agent.task_budget import MAX_TOKEN_SEGMENTS

    budget = TaskBudget(tokens=1000)
    budget.scale_to_window(1000, floor=1000)
    for _ in range(MAX_TOKEN_SEGMENTS):
        assert budget.grant_next_segment() is True
    assert budget.segments == MAX_TOKEN_SEGMENTS
    assert budget.grant_next_segment() is False


def test_显式配置是硬限制_自适应与续段都不越过它():
    explicit = TaskBudget(tokens=400_000, tokens_explicit=True)
    explicit.scale_to_window(1_000_000)  # 自适应不许放大
    assert explicit.tokens == 400_000
    assert explicit.grant_next_segment() is False  # 续段也不许
    assert explicit.tokens == 400_000


def test_显式配置为_0_表示不限且不被自适应打开(monkeypatch):
    """`AGENT_RUN_MAX_TOKENS=0` 的语义是「该轴不限」，不是「用默认值」。"""
    monkeypatch.setenv("SUBAGENT_RUN_MAX_TOKENS", "0")
    budget = from_env()
    assert budget.tokens == 0
    assert budget.tokens_explicit is True  # 显式关闭也是显式配置
    budget.scale_to_window(1_000_000)  # 不许把它变回有限值
    assert budget.tokens == 0
    assert budget.exceeded(tokens=10 ** 12) == ""
    assert budget.grant_next_segment() is False


def test_未配_tokens_时不算显式配置(monkeypatch):
    monkeypatch.delenv("SUBAGENT_RUN_MAX_TOKENS", raising=False)
    budget = from_env()
    assert budget.tokens_explicit is False
    assert budget.tokens == DEFAULT_MAX_TOKENS


# ── 段数跨续跑继承 ────────────────────────────────────────────────────────────
#
# 续段是"撞线时放宽 per-run 额度"，段数上限是这个放宽动作的闸门。预算对象每次 run 都新建、
# 段数默认归零 —— 不继承的话"崩溃 → 续跑"就能把 8 段重新领一遍，而续跑次数本身不限，
# 于是段数上限形同不存在：token 轴又回到"可以无限续"的状态。


def test_carry_segments_继承已用段数():
    from app.agent.task_budget import MAX_TOKEN_SEGMENTS

    budget = TaskBudget(tokens=1000)
    budget.scale_to_window(1000, floor=1000)
    budget.carry_segments(3)
    assert budget.segments == 3
    # 只剩 MAX-3 段可领，领完就停
    for _ in range(MAX_TOKEN_SEGMENTS - 3):
        assert budget.grant_next_segment() is True
    assert budget.segments == MAX_TOKEN_SEGMENTS
    assert budget.grant_next_segment() is False, "继承的段数必须真的计入上限"


def test_carry_segments_不吞掉本段已领的段():
    budget = TaskBudget(tokens=1000)
    budget.scale_to_window(1000, floor=1000)
    assert budget.grant_next_segment() is True  # 本段先领了 1 段
    budget.carry_segments(0)
    assert budget.segments == 1, "继承值更小不该把已领的段数抹掉"


def test_carry_segments_夹住脏数据():
    """`usage` 是 JSONB，人工能写歪：超上限的段数会让这个 run 一段都领不到（白扣额度）。"""
    from app.agent.task_budget import MAX_TOKEN_SEGMENTS

    budget = TaskBudget(tokens=1000)
    budget.scale_to_window(1000, floor=1000)

    budget.carry_segments(-5)
    assert budget.segments == 0

    budget.carry_segments(10**6)
    assert budget.segments == MAX_TOKEN_SEGMENTS

    budget.carry_segments("not-a-number")  # type: ignore[arg-type]
    assert budget.segments == MAX_TOKEN_SEGMENTS, "脏值不该改动已有段数"
