"""报告聚合与对标（E2 / E3）。

产出两层数字：

- **pass@k / avg@k**（对位 deepagents `UNIFIED_EVALS.md` 的口径）：同一任务跑 k 次，
  `pass@k` = **至少一次**通过的任务比例，`avg@k` = 各任务通过率的平均。
  两者都要报 —— 只看 pass@k 会把"不稳定但偶尔能过"的任务算成满分。
- **效率指标**（steps / tool_calls / tokens / wall_ms 的均值）：用来发现"做对了但绕远路"。

报告带**逐任务明细**（失败断言 + 备注），失败时可直接定位到具体断言。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from evals.runner import TaskResult

logger = logging.getLogger(__name__)


def _redact(text: str) -> str:
    """报告落盘前脱敏：复用**引擎那份**规则集（`app/subagent/redact.py`）。

    **不新造第二份清单** —— 两份密钥形态清单必然漂移（见 `vendor/规划.md` §4）。

    刻意**惰性 import**：评测套件本身不依赖引擎包（runner 走 HTTP 真实路径，见
    `vendor/规划.md` §6），只有真正聚合报告时才需要它。引擎不在场时**跳过并留痕**
    （fail-soft）—— 缺脱敏不该让评测失败，但**必须可见**，不能静默。
    """
    try:
        from app.subagent.redact import redact_text
    except ImportError:  # 引擎包不在场（独立跑评测套件）
        logger.warning("redact 规则集不可用，报告未脱敏（%d chars）", len(text))
        return text
    safe, _hits = redact_text(text)
    return safe


@dataclass
class TaskSummary:
    """一条任务的跨尝试汇总。"""

    task_id: str
    category: str
    attempts: int
    passes: int
    avg_steps: float
    avg_tool_calls: float
    avg_tokens: float
    avg_wall_ms: float
    failures: list[str] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return self.passes / self.attempts if self.attempts else 0.0

    @property
    def ever_passed(self) -> bool:
        return self.passes > 0

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "category": self.category,
            "attempts": self.attempts,
            "passes": self.passes,
            "pass_rate": round(self.pass_rate, 4),
            "avg_steps": round(self.avg_steps, 2),
            "avg_tool_calls": round(self.avg_tool_calls, 2),
            "avg_tokens": round(self.avg_tokens, 1),
            "avg_wall_ms": round(self.avg_wall_ms, 1),
            "failures": self.failures,
        }


@dataclass
class SuiteReport:
    """一个套件（一个 agent）的完整报告。"""

    suite: str
    agent: str
    tasks: list[TaskSummary]
    repeat: int = 1

    @property
    def pass_at_k(self) -> float:
        """至少一次通过的任务比例。"""
        if not self.tasks:
            return 0.0
        return sum(1 for t in self.tasks if t.ever_passed) / len(self.tasks)

    @property
    def avg_at_k(self) -> float:
        """各任务通过率的平均。"""
        if not self.tasks:
            return 0.0
        return sum(t.pass_rate for t in self.tasks) / len(self.tasks)

    def by_category(self) -> dict[str, dict[str, float]]:
        """按分类聚合（哪类任务强/弱一目了然）。"""
        buckets: dict[str, list[TaskSummary]] = defaultdict(list)
        for task in self.tasks:
            buckets[task.category].append(task)
        out: dict[str, dict[str, float]] = {}
        for category, items in sorted(buckets.items()):
            out[category] = {
                "tasks": float(len(items)),
                "pass_at_k": round(
                    sum(1 for t in items if t.ever_passed) / len(items), 4
                ),
                "avg_at_k": round(sum(t.pass_rate for t in items) / len(items), 4),
                "avg_steps": round(sum(t.avg_steps for t in items) / len(items), 2),
            }
        return out

    def to_dict(self) -> dict[str, object]:
        return {
            "suite": self.suite,
            "agent": self.agent,
            "repeat": self.repeat,
            "pass@k": round(self.pass_at_k, 4),
            "avg@k": round(self.avg_at_k, 4),
            "by_category": self.by_category(),
            "tasks": [t.to_dict() for t in self.tasks],
        }


def summarize(task_id: str, category: str, results: list[TaskResult]) -> TaskSummary:
    """把同一任务的多次尝试汇总为 `TaskSummary`。"""
    count = len(results)
    failures: list[str] = []
    for r in results:
        for check in r.evaluation.failures:
            # 断言 detail 里会带固件文本；异常文本可能含 URL/密钥 —— 两者都先脱敏。
            failures.append(f"attempt{r.attempt}: {check.kind} — {_redact(check.detail)}")
        if r.error:
            failures.append(f"attempt{r.attempt}: error — {_redact(r.error)}")

    def _avg(selector) -> float:
        if not count:
            return 0.0
        return sum(selector(r) for r in results) / count

    return TaskSummary(
        task_id=task_id,
        category=category,
        attempts=count,
        passes=sum(1 for r in results if r.passed),
        avg_steps=_avg(lambda r: r.observation.steps),
        avg_tool_calls=_avg(lambda r: len(r.observation.tool_calls)),
        avg_tokens=_avg(lambda r: r.observation.tokens),
        avg_wall_ms=_avg(lambda r: r.observation.wall_ms),
        failures=failures,
    )


def build_report(
    *, suite: str, agent: str, results: list[TaskResult], repeat: int = 1
) -> SuiteReport:
    """从原始结果构建报告。"""
    grouped: dict[str, list[TaskResult]] = defaultdict(list)
    categories: dict[str, str] = {}
    for result in results:
        grouped[result.task_id].append(result)
        categories[result.task_id] = result.category

    tasks = [
        summarize(task_id, categories[task_id], items)
        for task_id, items in grouped.items()
    ]
    tasks.sort(key=lambda t: t.task_id)
    return SuiteReport(suite=suite, agent=agent, tasks=tasks, repeat=repeat)


def compare(reports: dict[str, SuiteReport]) -> dict[str, object]:
    """对标对照表（E3）：同一任务集在不同 agent 上的 pass@k / avg@k / 效率。"""
    table: dict[str, object] = {}
    for agent, report in reports.items():
        table[agent] = {
            "pass@k": round(report.pass_at_k, 4),
            "avg@k": round(report.avg_at_k, 4),
            "by_category": report.by_category(),
        }

    # 差异清单：哪一类任务谁赢
    categories: set[str] = set()
    for report in reports.values():
        categories |= set(report.by_category())
    diffs: dict[str, dict[str, float]] = {}
    for category in sorted(categories):
        row: dict[str, float] = {}
        for agent, report in reports.items():
            row[agent] = report.by_category().get(category, {}).get("pass_at_k", 0.0)
        if len(row) >= 2:
            row["_spread"] = round(max(row.values()) - min(row.values()), 4)
        diffs[category] = row
    table["by_category_diff"] = diffs
    return table
