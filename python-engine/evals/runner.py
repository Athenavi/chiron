"""评测执行器（E2）。

两条约束决定了它的形状：

1. **走真实用户路径**：真实提交经 HTTP（网关 `/submit` + `/events` SSE），不 import 引擎
   内部类 —— 否则测的不是用户能用的东西；顺带覆盖了网关 / SSE / 租户链路。
2. **可注入**：`SubmitFn` 是一个协议。真实实现走 HTTP；**测试与 PR 门禁用确定性替身**
   （零密钥、零费用）。runner 只依赖协议，因此「落固件 → 提交 → 读产物 → 判定」这条
   核心逻辑可以被完整单测。
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from evals.assertions import Evaluation, Observation, evaluate
from evals.firmware import Task


class SubmitFn(Protocol):
    """提交一次任务并返回观测。

    实现者负责：把 prompt 发给 agent、收集 SSE 事件与最终文本、回报工具调用与用量。
    `workspace` 是本次任务的**独立工作目录**（runner 已把固件写入其中）。
    """

    async def __call__(self, task: Task, workspace: Path) -> Observation: ...


@dataclass
class TaskResult:
    """一条任务一次尝试的结果。"""

    task_id: str
    category: str
    passed: bool
    evaluation: Evaluation
    observation: Observation
    attempt: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "category": self.category,
            "attempt": self.attempt,
            "passed": self.passed,
            "failures": [
                {"kind": c.kind, "detail": c.detail} for c in self.evaluation.failures
            ],
            "efficiency_notes": self.evaluation.efficiency_notes,
            "steps": self.observation.steps,
            "tool_calls": len(self.observation.tool_calls),
            "tokens": self.observation.tokens,
            "wall_ms": self.observation.wall_ms,
            "error": self.error,
        }


def materialize_fixtures(task: Task, workspace: Path) -> None:
    """把固件文件写入 workspace（自动建父目录）。"""
    for rel, content in task.fixture_files.items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def collect_artifacts(task: Task, workspace: Path) -> dict[str, str]:
    """读回断言关心的产物文件。

    只看固件**声明过的路径**，不扫全目录 —— 否则"agent 顺手改了别的文件"会被当成产物，
    断言也会变得不可预测。
    """
    wanted = {a.path for a in task.assertions if a.path}
    out: dict[str, str] = {}
    for rel in wanted:
        target = workspace / rel
        if target.is_file():
            out[rel] = target.read_text(encoding="utf-8", errors="replace")
    return out


class EvalRunner:
    """按任务顺序执行（不并发：评测要的是可复现，不是吞吐）。"""

    def __init__(self, submit: SubmitFn, root: Path, *, keep_workspaces: bool = False) -> None:
        self._submit = submit
        self._root = root
        self._keep = keep_workspaces

    async def run_task(self, task: Task, *, attempt: int = 0) -> TaskResult:
        workspace = self._root / task.id
        workspace.mkdir(parents=True, exist_ok=True)
        materialize_fixtures(task, workspace)

        started = time.monotonic()
        error = ""
        try:
            obs = await self._submit(task, workspace)
        except Exception as exc:  # noqa: BLE001 — 单条任务失败不该中断整个套件
            error = f"{type(exc).__name__}: {exc}"
            obs = Observation()

        # 产物在提交之后读回（agent 可能刚写完）
        obs.files.update(collect_artifacts(task, workspace))
        if obs.wall_ms == 0:
            obs.wall_ms = int((time.monotonic() - started) * 1000)

        evaluation = evaluate(task.assertions, task.efficiency, obs)
        result = TaskResult(
            task_id=task.id,
            category=task.category,
            passed=evaluation.passed and not error,
            evaluation=evaluation,
            observation=obs,
            attempt=attempt,
            error=error,
        )
        if not self._keep:
            shutil.rmtree(workspace, ignore_errors=True)
        return result

    async def run_suite(self, tasks: list[Task], *, repeat: int = 1) -> list[TaskResult]:
        """跑完整个套件；`repeat > 1` 时每条任务跑多次（供 pass@k / avg@k 聚合）。"""
        results: list[TaskResult] = []
        for attempt in range(max(1, repeat)):
            for task in tasks:
                results.append(await self.run_task(task, attempt=attempt))
        return results
