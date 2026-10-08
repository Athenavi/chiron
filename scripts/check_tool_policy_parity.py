#!/usr/bin/env python3
"""Go ↔ Python 工具动作分级表的一致性检查。

背景
----
服务端（Go，``internal/api/tool_policy.go``）是**权威判定**，引擎侧（Python，
``python-engine/app/agent/tool_policy.py``）是同构的**保守镜像**（给前端展示级别 +
本地快速拒绝）。两者语言不同、无法共享代码，只能各写一份 —— 于是两边都写着
"改动其一必须同步另一个"，但这条不变量**此前没有任何机械化断言**，并且**已经漂移**
（``list_subagent_runs`` / ``read_tool_result`` / ``rerun_subagent`` 三处）。

本脚本把那条注释变成断言：按源码解析两张表，逐级别比对名称集合与命令类工具集合。
不做运行期的事（不 import 任何一侧），因此 Go 侧与 Python 侧都能跑。

用法
----
    python scripts/check_tool_policy_parity.py

退出码 0 = 两侧一致；1 = 有漂移（打印差异）。
规格化：仓库根（本文件的上两级）。

维护提示：断言失败时**不要**改这里的解析规则去迁就漂移 —— 要么补齐那一侧，
要么在两侧同时把某个名字移进"有意保留"的注释区（并在
``python-engine/tests/test_tool_policy_coverage.py`` 里点名）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GO_FILE = ROOT / "internal" / "api" / "tool_policy.go"
PY_FILE = ROOT / "python-engine" / "app" / "agent" / "tool_policy.py"

LEVELS = ("READ", "WRITE", "DELETE", "EXTERNAL")
_NAME_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"')


def _strip_comment(line: str, marker: str) -> str:
    """去掉行内注释。工具名里不会出现 marker，因此不必处理字符串内的 marker。"""
    idx = line.find(marker)
    return line[:idx] if idx >= 0 else line


def parse_go_table(text: str) -> tuple[dict[str, set[str]], set[str]]:
    """解析 ``add(ToolLevelX, ...)``（常量是 ``ToolLevelRead`` 这种驼峰）与 ``commandTools``。"""
    table: dict[str, set[str]] = {lvl: set() for lvl in LEVELS}
    command: set[str] = set()

    lines = [_strip_comment(raw, "//") for raw in text.splitlines()]
    current: str | None = None
    depth = 0
    for line in lines:
        if current is not None:
            depth += line.count("(") - line.count(")")
            table[current].update(_NAME_RE.findall(line))
            if depth <= 0:
                current = None
            continue
        match = re.search(r"\badd\(ToolLevel(Read|Write|Delete|External)\s*,", line)
        if match:
            current = match.group(1).upper()
            depth = line.count("(") - line.count(")")
            table[current].update(_NAME_RE.findall(line[match.end():]))
            if depth <= 0:
                current = None

    block = re.search(r"var commandTools = map\[string\]bool\{(.*?)\n\}", text, re.S)
    if block:
        body = "\n".join(_strip_comment(raw, "//") for raw in block.group(1).splitlines())
        command = set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*true', body))
    return table, command


def parse_py_table(text: str) -> tuple[dict[str, set[str]], set[str]]:
    """解析 ``_X_TOOLS: frozenset(...)`` 与 ``COMMAND_TOOLS: frozenset(...)``。

    同一个按括号深度推进的状态机同时覆盖多行块与单行块（``_DELETE_TOOLS`` 是单行）。
    """
    table: dict[str, set[str]] = {lvl: set() for lvl in LEVELS}
    command: set[str] = set()

    lines = [_strip_comment(raw, "#") for raw in text.splitlines()]
    bucket: set[str] | None = None
    depth = 0
    for line in lines:
        if bucket is not None:
            depth += line.count("(") - line.count(")")
            bucket.update(_NAME_RE.findall(line))
            if depth <= 0:
                bucket = None
            continue
        match = re.match(
            r"(_?(?:READ|WRITE|DELETE|EXTERNAL)_TOOLS|COMMAND_TOOLS)\s*:.*?frozenset\(",
            line.strip(),
        )
        if not match:
            continue
        key = match.group(1)
        bucket = command if key == "COMMAND_TOOLS" else table[key.lstrip("_").split("_")[0]]
        depth = line.count("(") - line.count(")")
        bucket.update(_NAME_RE.findall(line[line.index("frozenset(") :]))
        if depth <= 0:
            bucket = None
    return table, command


def main() -> int:
    if not GO_FILE.exists() or not PY_FILE.exists():
        print(f"FAIL: 找不到策略表文件（{GO_FILE} / {PY_FILE}）", file=sys.stderr)
        return 1

    go_table, go_command = parse_go_table(GO_FILE.read_text(encoding="utf-8"))
    py_table, py_command = parse_py_table(PY_FILE.read_text(encoding="utf-8"))

    problems: list[str] = []
    for level in LEVELS:
        go, py = go_table[level], py_table[level]
        if go != py:
            only_go = sorted(go - py)
            only_py = sorted(py - go)
            problems.append(
                f"{level}: 只在 Go {only_go or '—'}；只在 Python {only_py or '—'}"
            )
    if go_command != py_command:
        problems.append(
            f"COMMAND: 只在 Go {sorted(go_command - py_command) or '—'}；"
            f"只在 Python {sorted(py_command - go_command) or '—'}"
        )

    total = sum(len(v) for v in go_table.values())
    if problems:
        print("FAIL: Go 与 Python 的工具分级表已漂移（两侧必须同构）：")
        for problem in problems:
            print("  - " + problem)
        print("\n同步规则见两个文件开头的注释；权威侧是 Go。")
        return 1

    print(
        f"OK: 两侧同构 —— read/write/delete/external 共 {total} 条，"
        f"命令类 {len(go_command)} 个。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
