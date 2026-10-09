#!/usr/bin/env python
"""文档行号引用**复核工具**（review tool，**不进 CI、永远 exit 0**）。

## 它查什么

把 `docs/**/*.md` 与 `vendor/规划.md` 里的 `路径:行号` 断言抽出来，只报**硬错误**：
**引用的行号超出了该文件的实际长度**（例如 `gateway_router.go:660` 而该文件只有 499 行）。

这一条**零假阳性**：行号大于总行数时，那段代码**不可能在那里**。

## 为什么只做"超出长度"这一种判定

行号**会随任何一次插入而漂移**（哪怕只是加一行注释），所以：

* **不能做成 CI 门禁** —— 它会在**无关改动**上变红，护栏随即被人调大或关掉（本仓的教训：假阳性会杀死护栏）；
* **也不能去校验"那一行的内容对不对"** —— 那需要读懂语义，正是文档与代码各自负责的部分。

⇒ 它是**探针**：跑一次、逐条回看、改掉真的错。**永远 exit 0**（与 `doc_value_sweep.py` 同一定位）。

## 已知的"看起来像错、其实不是"

* **带日期的历史引用**：文档会**故意保留旧值**（「原记 …」「此前记 …」「复测」「再测」）作为数值/行号
  的变迁链 —— 这些行**整行跳过**（`HIST` 正则）。若不过滤，刚更正完的行会立刻被自己报出来。
* **简写/聚合引用**（`response.go:67` 而仓库里有多个同名文件、或路径写的是 `transcript*.ts` 这类通配）
  ⇒ 解析不到就**跳过并计数**，不猜。

## 用法

    python scripts/check_doc_line_refs.py            # 列出硬错误
    python scripts/check_doc_line_refs.py --all      # 连"解析不到"的也列出来
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: `路径.ext:行号` 或 `路径.ext:起-止`
PAT = re.compile(r"([A-Za-z0-9_./\-]+\.(?:ts|vue|py|go|md|mjs|json|yml|yaml|css|jinja2)):(\d+)(?:-(\d+))?")

#: 带日期的历史引用 —— 整行跳过（见文件头）
HIST = re.compile(r"(原记|此前记|复测|再测|原为|曾记)")

#: 文档里的路径常是简写，按这些前缀依次尝试
PREFIXES = ["", "frontend-vue/src/", "frontend-vue/", "python-engine/", "internal/", "docs/"]


def resolve(path_text: str) -> pathlib.Path | None:
    for pre in PREFIXES:
        cand = ROOT / (pre + path_text)
        if cand.is_file():
            return cand
    name = pathlib.PurePosixPath(path_text).name
    hits = [
        p for p in ROOT.rglob(name)
        if p.is_file() and "node_modules" not in str(p) and ".pnpm-store" not in str(p)
    ]
    return hits[0] if len(hits) == 1 else None


def main() -> int:
    show_all = "--all" in sys.argv

    candidates: dict[tuple[str, int, int | None], list[str]] = {}
    docs = list((ROOT / "docs").glob("*.md"))
    plan = ROOT / "vendor" / "规划.md"
    if plan.exists():
        docs.append(plan)

    for f in docs:
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if HIST.search(line):
                continue
            for m in PAT.finditer(line):
                path, start, end = m.group(1), int(m.group(2)), m.group(3)
                candidates.setdefault((path, start, int(end) if end else None), []).append(f"{f.name}:{i}")

    over: list[tuple[str, int, int | None, int, pathlib.Path, list[str]]] = []
    unresolved: list[tuple[str, int, list[str]]] = []
    for (path, start, end), refs in sorted(candidates.items()):
        real = resolve(path)
        if real is None:
            unresolved.append((path, start, refs))
            continue
        total = len(real.read_text(encoding="utf-8", errors="replace").splitlines())
        if max(start, end or 0) > total:
            over.append((path, start, end, total, real.relative_to(ROOT), refs))

    print(f"抽到 {len(candidates)} 条 (路径, 行号) 断言（已跳过带日期的历史引用）")
    print(f"路径解析不了：{len(unresolved)} 条（简写/聚合引用，跳过）")
    print(f"**行号超出文件长度：{len(over)} 条**")
    for path, start, end, total, real, refs in over:
        span = f"{start}" if end is None else f"{start}-{end}"
        print(f"  [X] {path}:{span} → {real} 实际只有 {total} 行   ← 引用自 {', '.join(refs[:3])}")

    if show_all and unresolved:
        print("\n解析不到的（--all）：")
        for path, start, refs in unresolved:
            print(f"  ? {path}:{start}   ← 引用自 {', '.join(refs[:2])}")

    print("\n（复核工具：只报「超出文件长度」这种零假阳性的硬错误；改完请重跑）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
