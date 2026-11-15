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

#: 主 Agent 步数上限：一轮里 20 次工具调用是常态，长任务一条工具链跑到上百步很常见。
#: 60 会在"轮次还有富余"时先见底，表现与"轮数太少"完全一样（任务没做完就报
#: `budget_exceeded:steps`）。200 仍能拦住真正的失控（每轮 4 次 × 50 轮 = 200）。
DEFAULT_MAX_STEPS = 200
#: 主 Agent token 上限的**地板值**（不是固定值）。
#:
#: 主 Agent 的实际默认值由 :meth:`TaskBudget.scale_to_window` 按模型窗口算
#: （窗口 × 3）。固定 400k 对 128K 窗口的模型只够 2–3 轮 —— 每轮都要重发整段上下文，
#: 长任务因此报 `budget_exceeded:tokens` 停下。子 Agent 仍以这个量级起步。
DEFAULT_MAX_TOKENS = 400_000
#: token 轴的上限封顶。窗口联动后可能算出很大的数（1M 窗口 × 3 = 3M），
#: 但预算的职责是"拦明显失控"而不是"精确计费"—— 封顶保证失控仍能被拦住。
DEFAULT_MAX_TOKENS_CEILING = 8_000_000
#: token 轴的默认上限 = **模型窗口 × 这个倍数**。
#:
#: 1 ⇒ 一轮都跑不完（每轮都要重发整段历史，单轮 input 就可能接近窗口）；
#: 3 ⇒ 约 3–4 轮的净消耗，配合下面的续段机制足以跑完长任务。
TOKENS_PER_WINDOW = 3
#: 越界续段的**段数上限**。token 撞线只抬上限、不动已用量，新开一段继续跑；
#: 8 段足够覆盖超长任务，同时保证真正的失控（无限循环烧钱）仍会被停下。
MAX_TOKEN_SEGMENTS = 8
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


def _env_set(name: str) -> bool:
    """环境变量是否**显式给了可解析的值**（空串 / 非法值都算"没配"）。

    为什么需要它：``AGENT_RUN_MAX_TOKENS=0`` 的语义是"该轴不限"，而"没配"要回落默认值。
    两者在 ``int`` 结果上都是 0 —— 只看数值会把用户的显式关闭当成没配，于是回落到默认
    值，等于用户根本关不掉这一轴。
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return False
    try:
        int(raw)
    except ValueError:
        return False
    return True


@dataclass
class TaskBudget:
    """一次 agent run 的预算（每轴 0 = 该轴不限）。"""

    #: 工具调用总次数上限。**dataclass 默认 0（不限）**，由 :func:`from_env` 决定默认值：
    #: 主 Agent 200、子 Agent 0（子 Agent 的步数由 `max_turns` 与看门狗管，不需要这一轴）。
    steps: int = 0
    turns: int = 0
    tokens: int = DEFAULT_MAX_TOKENS
    wall: int = DEFAULT_MAX_SECONDS
    cost: float = DEFAULT_MAX_COST
    started_at: float = field(default_factory=time.time)

    #: token 轴的续段步长（由 :meth:`scale_to_window` 固定为**模型窗口**）。
    #:
    #: 步长必须与"当前上限"解耦：每次续段都按窗口抬一截，而不是按当前上限成倍放大 ——
    #: 否则 8 段下来是 `窗口 × 2^8` 的指数膨胀，预算就不再是闸门。
    token_step: int = 0
    #: 已续段数（上限 :data:`MAX_TOKEN_SEGMENTS`）。只用于"段数用完就真的停"。
    segments: int = 0
    #: token 上限是否来自**显式配置**（`AGENT_RUN_MAX_TOKENS` 或调用方参数）。
    #:
    #: 显式配置是**硬限制**：自适应（:meth:`scale_to_window`）与续段
    #: （:meth:`grant_next_segment`）都不越过它 —— 否则把它悄悄放大等于它没生效。
    tokens_explicit: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.steps or self.turns or self.tokens or self.wall or self.cost)

    def elapsed(self) -> float:
        return time.time() - self.started_at

    def scale_to_window(self, window: int, *, floor: int = DEFAULT_MAX_TOKENS) -> None:
        """按**模型窗口**放大 token 轴（只放大、不缩小；显式配置时不越过它）。

        为什么需要：token 轴按"累计重发历史"计费 —— 第 n 轮的 input 随 n 线性增长，
        累计起来约呈 n²。固定 400k 在 5k→60k 的增长里跑到第 12–15 轮就撞线，而任务
        还有一半（`max_turns=40`）没跑。窗口自适应让"能跑多少轮"跟着模型的实际容量走：
        1M 窗口的模型得到 3M，128k 的模型仍是地板值 —— 小窗口不会被放大。

        ``floor`` 是该轴的**地板值**，由调用方给：主 Agent 400k、子 Agent 200k。
        不要在这里硬编码主 Agent 的值 —— 那会把子 Agent 更紧的额度悄悄放宽一倍。

        上限来源（取最小）：窗口 × :data:`TOKENS_PER_WINDOW`、
        :data:`DEFAULT_MAX_TOKENS_CEILING`、显式配置。
        """
        if window <= 0:
            return
        # 步长**固定在窗口值**上：续段不随上限成倍膨胀（见 token_step 的注释）。
        self.token_step = max(1, int(window))
        if self.tokens_explicit:
            # 显式配置是硬限制：连"放大到地板值"都不做。
            return
        if not self.tokens:
            # 0 = 该轴不限（用户显式关掉这一轴）：不要把它变成有限值。
            return
        target = min(
            DEFAULT_MAX_TOKENS_CEILING,
            max(int(floor), int(window * TOKENS_PER_WINDOW)),
        )
        if target > self.tokens:
            self.tokens = target

    def grant_next_segment(self) -> bool:
        """token 轴撞线时**开下一段**：只抬上限、不动已用量。

        已用量保持累加是计费口径 —— 清零会让网关少扣费（`_tokens_spent()` 同时喂
        预算判定与 `budget.tokens.used`）。

        返回 True = 已抬上限、可以继续跑；False = 不该续（该轴不限 / 显式硬限制 /
        段数用尽 / 已到封顶）。
        """
        if not self.tokens or self.tokens_explicit:
            return False
        if self.segments >= MAX_TOKEN_SEGMENTS:
            return False
        step = self.token_step or DEFAULT_MAX_TOKENS
        target = min(DEFAULT_MAX_TOKENS_CEILING, self.tokens + step)
        if target <= self.tokens:
            return False
        self.tokens = target
        self.segments += 1
        return True

    def carry_segments(self, used: int) -> None:
        """从快照继承**已用段数**（续跑/接管路径调用）。

        为什么段数也要跨续跑：续段是"撞线时放宽 per-run 额度"，而
        :data:`MAX_TOKEN_SEGMENTS` 是这个放宽动作的闸门。预算对象每次 run 新建、
        :attr:`segments` 默认归零 —— 不继承的话，"崩溃 → 续跑"就能把 8 段重新领一遍，
        而续跑次数本身不限 ⇒ 段数上限等于不存在，token 轴又回到"没有任何上限"。

        ``used`` 先夹进 ``[0, MAX_TOKEN_SEGMENTS]``：快照里的 ``usage`` 是 JSONB，
        人工修数据就能写歪；一个超大的段数会让这个 run **一段都领不到**，
        而那是"白扣额度"，与"白送额度"是同一类错误的两个方向。
        取 ``max`` 是为了不与"本段已经领过的段"互相抵消。
        """
        try:
            carried = int(used)
        except (TypeError, ValueError):
            return
        self.segments = max(self.segments, min(max(carried, 0), MAX_TOKEN_SEGMENTS))

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

    ⚠ **tokens / turns 两轴的名字带 `RUN_`**（``AGENT_RUN_MAX_TOKENS`` /
    ``SUBAGENT_RUN_MAX_TOKENS`` / ``..._RUN_MAX_TURNS``），与其余三轴不同。这不是手滑：
    那两轴的裸名（``AGENT_MAX_TOKENS`` / ``AGENT_MAX_TURNS``）在**网关**里是另一个意思
    （单次调用的 max output / 每次提交的默认回合数），同名不同义会让共享 env 的部署
    把引擎的 run 预算顶成"单次输出上限"（M20）。steps / seconds / cost 三轴只由引擎读，
    不与网关撞名，因此保留原名 —— 改名要精确到"真正会撞的那两个"。

    **显式配置是硬限制**：函数参数给了非零值，或环境变量给了可解析的值（**含 0**），
    都会把 ``tokens_explicit`` 置位 —— 于是窗口自适应与续段都不会越过它。``=0`` 仍然
    保持"该轴不限"的语义（不是"用默认"）。
    """
    env_tokens = f"{prefix}_RUN_MAX_TOKENS"
    explicit_tokens = bool(tokens) or _env_set(env_tokens)
    # 显式 0（"不限"）必须原样保留：`int(tokens or 0) or _env_int(...)` 会把 0 当"没配"
    # 回落默认值，用户就再也关不掉这条轴。
    if tokens:
        tokens_value = int(tokens)
    elif _env_set(env_tokens):
        tokens_value = _env_int(env_tokens, default_tokens)
    else:
        tokens_value = default_tokens
    return TaskBudget(
        steps=int(steps or 0) or _env_int(f"{prefix}_MAX_STEPS", default_steps),
        turns=int(turns or 0) or _env_int(f"{prefix}_RUN_MAX_TURNS", 0),
        tokens=tokens_value,
        wall=int(seconds or 0) or _env_int(f"{prefix}_MAX_SECONDS", DEFAULT_MAX_SECONDS),
        cost=float(cost or 0.0) or float(_env_int(f"{prefix}_MAX_COST", int(DEFAULT_MAX_COST))),
        tokens_explicit=explicit_tokens,
    )
