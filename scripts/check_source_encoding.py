#!/usr/bin/env python3
"""源码编码守卫：禁止 U+FFFD（REPLACEMENT CHARACTER）与非法 UTF-8 字节。

**为什么需要它**：`python-engine/pyproject.toml` 曾被非 UTF-8 工具改写，4 处中文注释
变成 U+FFFD。其中 3 处把**依赖声明吞进了注释行** —— `anthropic>=0.52.0`、
`pymilvus>=2.5.0`、`prometheus-client>=0.22.0` 从 `[project].dependencies` 中消失
（`tomllib` 解析后只有 23 项），而代码里 `app/providers/anthropic.py`、
`app/rag/`、`app/observability/metrics.py` 都在用它们。

乱码注释看起来无害，实际让 `pip install .` 少装三个真实依赖；又因为 CI 与镜像都走
`requirements.txt`（自带前两个、缺第三个），这个缺陷长期没有被任何检查发现。
U+FFFD 不是「字体问题」，而是**字节已经损坏**的信号 —— 必须在合入前阻断。

判定口径：
  1. 被 git 跟踪的文本文件必须能按 UTF-8 解码；
  2. 解码后不得含 U+FFFD。

用 `git ls-files` 而不是自己遍历目录，是为了天然跳过未跟踪内容（`vendor/` 下的参考源码、
`node_modules/`、构建产物等），只对真正入库的源码负责。

用法：
    python scripts/check_source_encoding.py            # 校验（CI）
    python scripts/check_source_encoding.py --verbose  # 同时打印扫描总数
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

REPLACEMENT = "\ufffd"


def tracked_files() -> list[str]:
    """返回被 git 跟踪的文件路径（NUL 分隔，兼容非 ASCII 文件名）。"""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            capture_output=True,
            check=True,
        ).stdout
    except FileNotFoundError:
        print("FATAL: 未找到 git —— 本检查依赖 git ls-files 枚举被跟踪文件", file=sys.stderr)
        raise SystemExit(2)
    except subprocess.CalledProcessError as exc:  # pragma: no cover - 仅环境异常
        print(f"FATAL: git ls-files 失败：{exc.stderr.decode('utf-8', 'replace')}", file=sys.stderr)
        raise SystemExit(2)
    return [os.fsdecode(name) for name in out.split(b"\x00") if name]


def check(path: str) -> list[str]:
    """返回该文件的问题列表（空列表 = 干净）。二进制文件直接跳过。"""
    try:
        data = pathlib.Path(path).read_bytes()
    except OSError as exc:
        return [f"无法读取：{exc}"]

    # 二进制文件（含 NUL 字节）不参与文本编码检查
    if b"\x00" in data:
        return []

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [f"非法 UTF-8 字节：{exc}"]

    if REPLACEMENT not in text:
        return []

    return [f"第 {n} 行含 U+FFFD（字节已损坏）：{line.strip()[:100]}"
            for n, line in enumerate(text.splitlines(), 1)
            if REPLACEMENT in line]


def main() -> int:
    verbose = "--verbose" in sys.argv[1:]
    files = tracked_files()
    problems: list[tuple[str, str]] = []

    for path in files:
        for issue in check(path):
            problems.append((path, issue))

    if problems:
        print("编码检查失败 —— 以下文件含 U+FFFD 或非法 UTF-8，属**字节级损坏**，不是排版问题：", file=sys.stderr)
        for path, issue in problems:
            print(f"  {path}: {issue}", file=sys.stderr)
        print("", file=sys.stderr)
        print("修复方式：用 UTF-8 重写该处文本（U+FFFD 无法自动还原）；"
              "若乱码出现在配置里，务必确认没有被它吞掉的条目。", file=sys.stderr)
        return 1

    if verbose:
        print(f"编码检查通过：{len(files)} 个被跟踪文件均为合法 UTF-8 且不含 U+FFFD")
    else:
        print("编码检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
