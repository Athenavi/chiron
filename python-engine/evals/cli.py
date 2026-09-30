"""评测 CLI（E2 / E3）。

```bash
# 替身 provider：零密钥、零费用 —— 这是它能作 PR 门禁的前提
python -m evals.cli run --suite smoke --submit scripted --report out/smoke.json

# 真实栈（经网关，走用户实际路径）
python -m evals.cli run --suite smoke --submit http \
    --base-url http://127.0.0.1:8080 --api-key "$CHIRON_API_KEY" --report out/chiron.json

# 与对标结果对照（E3）
python -m evals.cli compare out/chiron.json out/deepagents.json
```

退出码：**0 = 全部通过；1 = 有任务失败**（CI 可直接用）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from evals.firmware import Task, load_suite, select_tasks
from evals.report import SuiteReport, build_report, compare
from evals.runner import EvalRunner, SubmitFn

SUITES_DIR = Path(__file__).resolve().parent / "suites"


def _load_tasks(suite: str, tier: str | None) -> list[Task]:
    """加载套件任务（`--suite` 支持逗号分隔多个固件）。

    `--suite full` 会**自动并入 smoke**：tier 的语义是"这条任务属于哪一级门禁"，而不是
    "它写在哪个文件里"。`smoke.json` 里有大量任务标着 `["smoke","full"]`，跑 full 集时若漏掉
    它们，nightly 门禁就比 PR 门禁还窄 —— 越靠后的门禁覆盖越少，这是危险的荒谬。
    """
    names = [s.strip() for s in str(suite).split(",") if s.strip()]
    if "full" in names and "smoke" not in names:
        names.insert(0, "smoke")

    tasks: list[Task] = []
    seen: set[str] = set()
    for name in names:
        path = SUITES_DIR / f"{name}.json"
        if not path.is_file():
            raise SystemExit(f"suite not found: {path}")
        for task in load_suite(path):
            # 跨文件查重：`load_suite` 只保证**单文件内**唯一
            if task.id in seen:
                raise SystemExit(f"duplicate task id across suites: {task.id!r}")
            seen.add(task.id)
            tasks.append(task)
    return select_tasks(tasks, tier) if tier else tasks


def _load_scripted(suite: str) -> dict[str, dict[str, Any]]:
    """加载替身脚本（`suites/<suite>.scripted.json`；多个套件按顺序合并）。

    脚本声明"每条任务发什么事件、落什么文件" —— 它验证的是 **runner / 断言 / 报告链路**，
    而不是 agent 的能力（后者必须用真实模型跑）。README 里对此有明确说明。
    """
    scripts: dict[str, dict[str, Any]] = {}
    for name in [s.strip() for s in str(suite).split(",") if s.strip()]:
        path = SUITES_DIR / f"{name}.scripted.json"
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        # 只取**值为对象**的键：脚本文件里允许放 `_comment` 之类的元数据（字符串），
        # 它们不是任务脚本。用类型判断而不是键名前缀，避免元数据改个名就炸。
        scripts.update(
            {str(k): dict(v) for k, v in payload.items() if isinstance(v, dict)}
        )
    return scripts


def _build_submit(args: argparse.Namespace) -> SubmitFn:
    if args.submit == "http":
        from evals.submit import HttpSubmit

        if not args.base_url:
            raise SystemExit("--base-url is required with --submit http")
        return HttpSubmit(args.base_url, args.api_key or "")
    from evals.submit import ScriptedSubmit

    return ScriptedSubmit(_load_scripted(args.suite))


def _print_summary(report: SuiteReport) -> None:
    print(f"suite={report.suite} agent={report.agent} repeat={report.repeat}")
    print(f"  pass@k = {report.pass_at_k:.2%}   avg@k = {report.avg_at_k:.2%}")
    print("  by category:")
    for category, row in report.by_category().items():
        print(
            f"    {category:<10} pass@k={row['pass_at_k']:.2%} "
            f"avg@k={row['avg_at_k']:.2%} avg_steps={row['avg_steps']:.1f}"
        )
    failed = [t for t in report.tasks if not t.ever_passed]
    if failed:
        print("  failures:")
        for task in failed:
            print(f"    - {task.task_id}")
            for detail in task.failures[:3]:
                print(f"        {detail}")


async def _cmd_run(args: argparse.Namespace) -> int:
    tasks = _load_tasks(args.suite, args.tier)
    if not tasks:
        print(f"no tasks selected (suite={args.suite} tier={args.tier})", file=sys.stderr)
        return 1

    submit = _build_submit(args)
    root = Path(args.workdir or "out/eval-workspaces")
    root.mkdir(parents=True, exist_ok=True)
    runner = EvalRunner(submit, root, keep_workspaces=args.keep_workspaces)
    results = await runner.run_suite(tasks, repeat=args.repeat)

    report = build_report(
        suite=args.suite, agent=args.agent, results=results, repeat=args.repeat
    )
    _print_summary(report)

    if args.report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"report written: {out}")

    return 0 if report.pass_at_k == 1.0 else 1


def _cmd_compare(args: argparse.Namespace) -> int:
    reports: dict[str, SuiteReport] = {}
    for raw_path in args.reports:
        payload = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        reports[str(payload.get("agent", raw_path))] = _report_from_dict(payload)
    table = compare(reports)
    print(json.dumps(table, ensure_ascii=False, indent=2))
    return 0


def _report_from_dict(payload: dict[str, Any]) -> SuiteReport:
    """从报告 JSON 还原 `SuiteReport`（`compare` 只需要聚合数字与分类）。"""
    from evals.report import TaskSummary

    tasks = [
        TaskSummary(
            task_id=str(t.get("task_id", "")),
            category=str(t.get("category", "")),
            attempts=int(t.get("attempts", 1)),
            passes=int(t.get("passes", 0)),
            avg_steps=float(t.get("avg_steps", 0.0)),
            avg_tool_calls=float(t.get("avg_tool_calls", 0.0)),
            avg_tokens=float(t.get("avg_tokens", 0.0)),
            avg_wall_ms=float(t.get("avg_wall_ms", 0.0)),
            failures=[str(f) for f in (t.get("failures") or [])],
        )
        for t in (payload.get("tasks") or [])
    ]
    return SuiteReport(
        suite=str(payload.get("suite", "")),
        agent=str(payload.get("agent", "")),
        tasks=tasks,
        repeat=int(payload.get("repeat", 1)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals", description="Chiron 评测套件")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="跑一个套件")
    run.add_argument("--suite", required=True)
    run.add_argument("--agent", default="chiron")
    run.add_argument("--tier", choices=["smoke", "full"], default=None)
    run.add_argument("--submit", choices=["scripted", "http"], default="scripted")
    run.add_argument("--base-url", default="")
    run.add_argument("--api-key", default="")
    run.add_argument("--repeat", type=int, default=1, help="每条任务跑几次（pass@k）")
    run.add_argument("--report", default="")
    run.add_argument("--workdir", default="")
    run.add_argument("--keep-workspaces", action="store_true")

    cmp_cmd = sub.add_parser("compare", help="对照多份报告")
    cmp_cmd.add_argument("reports", nargs="+")

    args = parser.parse_args(argv)
    if args.command == "run":
        return asyncio.run(_cmd_run(args))
    return _cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
