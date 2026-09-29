"""评测固件模型与加载（E1）。

**为什么用 JSON 而不是 YAML**：`pyyaml` 不在 `requirements.txt` / `pyproject.toml` 的
依赖清单里（本机可用是被其它依赖传递装上的，CI 未必有），而评测骨架不该引入清单外
依赖。JSON 是标准库，对"任务描述 + 断言 + 固件文件"这三样完全够用。

固件形状（`suites/*.json`）：

```json
{
  "suite": "smoke",
  "tasks": [
    {
      "id": "file-edit-rename-config",
      "category": "file_edit",
      "tiers": ["smoke"],
      "prompt": "把 config.ini 里的 debug=false 改成 debug=true",
      "fixture_files": {"config.ini": "debug=false\n"},
      "assertions": [
        {"kind": "file_contains", "path": "config.ini", "value": "debug=true"}
      ],
      "efficiency": {"max_steps": 8, "max_tool_calls": 6}
    }
  ]
}
```
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 允许的断言类别。前 7 个是 **success**（不满足即任务失败），最后一个也属 success。
SUCCESS_KINDS: frozenset[str] = frozenset(
    {
        "final_text_contains",
        "final_text_not_contains",
        "file_equals",
        "file_contains",
        "tool_called",
        "tool_denied",
        "event_emitted",
        "guardrail_blocked",
    }
)

#: 允许的 tier（`smoke` 用于 PR 门禁，`full` 用于 nightly）
TIERS: frozenset[str] = frozenset({"smoke", "full"})

#: 允许的分类（覆盖 Chiron 的真实能力面，见方案 02 §6.1）
CATEGORIES: frozenset[str] = frozenset(
    {
        "file_edit",
        "long_task",
        "subagent",
        "memory",
        "skill",
        "rag",
        "guardrail",
    }
)


@dataclass(frozen=True)
class Assertion:
    """单条断言。

    Attributes:
        kind: `SUCCESS_KINDS` 之一。
        value: 期望出现的文本 / 期望的相等值。
        path: 针对产物文件时使用（相对 workspace）。
        tool: 针对工具调用时使用（工具名）。
    """

    kind: str
    value: str = ""
    path: str = ""
    tool: str = ""


@dataclass(frozen=True)
class Efficiency:
    """效率期望 —— **只记录，不失败**（对位 deepagents 的双层断言模型）。

    这样"任务做对了但绕了远路"不会被当成失败，同时能看出回归（步数/成本上升）。
    """

    max_steps: int | None = None
    max_tool_calls: int | None = None
    max_tokens: int | None = None
    max_wall_ms: int | None = None


@dataclass(frozen=True)
class Task:
    """一条评测任务。"""

    id: str
    category: str
    tiers: tuple[str, ...]
    prompt: str
    assertions: tuple[Assertion, ...]
    fixture_files: dict[str, str] = field(default_factory=dict)
    efficiency: Efficiency = field(default_factory=Efficiency)


def _parse_assertion(raw: dict[str, Any], *, task_id: str) -> Assertion:
    kind = str(raw.get("kind", ""))
    if kind not in SUCCESS_KINDS:
        msg = (
            f"task {task_id!r}: unknown assertion kind {kind!r} "
            f"(allowed: {sorted(SUCCESS_KINDS)})"
        )
        raise ValueError(msg)
    return Assertion(
        kind=kind,
        value=str(raw.get("value", "")),
        path=str(raw.get("path", "")),
        tool=str(raw.get("tool", "")),
    )


def _parse_efficiency(raw: dict[str, Any] | None) -> Efficiency:
    if not raw:
        return Efficiency()
    return Efficiency(
        max_steps=raw.get("max_steps"),
        max_tool_calls=raw.get("max_tool_calls"),
        max_tokens=raw.get("max_tokens"),
        max_wall_ms=raw.get("max_wall_ms"),
    )


def parse_task(raw: dict[str, Any]) -> Task:
    """把一条原始任务解析为 `Task`（校验必填与枚举，失败即报错而不是静默跳过）。"""
    task_id = str(raw.get("id", "")).strip()
    if not task_id:
        msg = "task is missing 'id'"
        raise ValueError(msg)

    category = str(raw.get("category", "")).strip()
    if category not in CATEGORIES:
        msg = f"task {task_id!r}: unknown category {category!r} (allowed: {sorted(CATEGORIES)})"
        raise ValueError(msg)

    tiers = tuple(str(t) for t in (raw.get("tiers") or ["full"]))
    unknown = [t for t in tiers if t not in TIERS]
    if unknown:
        msg = f"task {task_id!r}: unknown tier(s) {unknown} (allowed: {sorted(TIERS)})"
        raise ValueError(msg)

    prompt = str(raw.get("prompt", "")).strip()
    if not prompt:
        msg = f"task {task_id!r}: 'prompt' is required"
        raise ValueError(msg)

    raw_assertions = raw.get("assertions") or []
    if not raw_assertions:
        msg = f"task {task_id!r}: at least one assertion is required"
        raise ValueError(msg)

    return Task(
        id=task_id,
        category=category,
        tiers=tiers,
        prompt=prompt,
        assertions=tuple(_parse_assertion(a, task_id=task_id) for a in raw_assertions),
        fixture_files={
            str(k): str(v) for k, v in (raw.get("fixture_files") or {}).items()
        },
        efficiency=_parse_efficiency(raw.get("efficiency")),
    )


def load_suite(path: Path) -> list[Task]:
    """加载一个固件文件，返回其中的任务列表。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_tasks = payload.get("tasks") or []
    tasks = [parse_task(raw) for raw in raw_tasks]

    seen: set[str] = set()
    for task in tasks:
        if task.id in seen:
            msg = f"duplicate task id in {path.name}: {task.id!r}"
            raise ValueError(msg)
        seen.add(task.id)
    return tasks


def select_tasks(tasks: list[Task], tier: str) -> list[Task]:
    """按 tier 过滤（`full` 含所有标了 full 的任务；`smoke` 只含 smoke）。"""
    if tier not in TIERS:
        msg = f"unknown tier: {tier!r} (allowed: {sorted(TIERS)})"
        raise ValueError(msg)
    return [t for t in tasks if tier in t.tiers]
