#!/usr/bin/env python3
"""**复核工具**（不是门禁）：按「值」扫 `docs/*.md`，找文档里过期/互相矛盾的事实型数值。

**为什么需要它**：文档会把同一批事实抄在多个小节（`hook-protocol-design.md` 的 §1 与 §10.2、
`reasonix-ui-ux-gap-analysis.md` 的第 61 行与 §7.2 索引）⇒ **按小节核会漏，按值扫才不会**。
本工具在 2026-10-09 的第 96–99 轮里就是这样找出并修掉了 **9 处**过期数值。

**⚠ 它刻意不做成门禁**：实测**精确率不高**，三类已知假阳性 ——
  ① **窗口串味**：路径后 60 字内的数字可能属于**另一个**文件（把 `MessageList.vue` 的
     「`:83` 窗口化阈值 150 行」读成"文件 150 行"）；
  ② **词内数字**：`check_source_encoding.py` 那行注释里的 **`UTF-8 BOM`** 被读成"记 8 B"；
  ③ **"原记 …"历史引用**：按本工具的产出把旧值标注成"原记 X"后，X 仍会被报成不符（**这是有意的** ——
     历史引用应保留；复核时按行判断即可）。
⇒ 输出**永远 exit 0**，只当线索用；落笔前**必须逐条回看那一行**。

用法：
    python scripts/doc_value_sweep.py            # 打印报告
    python scripts/doc_value_sweep.py --conflicts # 只看"同一路径多个不同值"
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PATH_RE = r"[A-Za-z0-9_./-]+\.(?:py|go|ts|vue|mjs|sql|yml|yaml|json|md)"
PATH = re.compile(PATH_RE)
NUM = re.compile(r"(约\s*)?([0-9][0-9,]{0,7})\s*(行|B|KB|字节)")
WINDOW = 60


def collect() -> tuple[list[tuple[str, str, int, str, int]], list[tuple[str, int, str, int]]]:
    docs = sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md"]
    claims: list[tuple[str, str, int, str, int]] = []
    refs: list[tuple[str, int, str, int]] = []
    for doc in docs:
        if not doc.exists():
            continue
        text = doc.read_text(encoding="utf-8", errors="replace")
        for m in PATH.finditer(text):
            path = m.group(0)
            if "/" not in path:
                continue
            window = text[m.end(): m.end() + WINDOW]
            docline = text[: m.start()].count("\n") + 1
            for nm in NUM.finditer(window):
                claims.append((path, nm.group(3), int(nm.group(2).replace(",", "")), doc.name, docline))
            rm = re.match(r":(\d{1,6})(?:-(\d{1,6}))?", window)
            if rm:
                refs.append((path, int(rm.group(2) or rm.group(1)), doc.name, docline))
    return claims, refs


def real_value(path: str, unit: str) -> int | None:
    p = ROOT / path
    if not p.exists() or unit == "KB":
        return None
    if unit == "行":
        return len(p.read_text(encoding="utf-8", errors="replace").splitlines())
    return p.stat().st_size


def main() -> int:
    only_conflicts = "--conflicts" in sys.argv
    claims, refs = collect()

    by_key: dict[tuple[str, str], list[tuple[int, str, int]]] = {}
    for path, unit, value, doc, ln in claims:
        by_key.setdefault((path, unit), []).append((value, doc, ln))

    conflicts = {k: v for k, v in by_key.items() if len({x[0] for x in v}) > 1}
    print(f"抽到 {len(claims)} 条数值断言 + {len(refs)} 条行号引用；"
          f"其中「同一路径多个不同值」{len(conflicts)} 组")
    print("（⚠ 复核工具：假阳性见文件头三类；落笔前逐条回看原行）\n")

    print(f"=== 同一路径被写成多个不同值（{len(conflicts)} 组）===")
    for (path, unit), vals in sorted(conflicts.items()):
        print(f"  {path}  [{unit}]")
        for value, doc, ln in sorted(vals):
            print(f"      {value:>10,}  ← {doc}:{ln}")

    if only_conflicts:
        return 0

    print("\n=== 与真实值不符 ===")
    n = 0
    for (path, unit), vals in sorted(by_key.items()):
        real = real_value(path, unit)
        if real is None:
            continue
        for value, doc, ln in vals:
            if value != real:
                n += 1
                print(f"  {path} 记 {value:,} {unit}，真实 {real:,}  ← {doc}:{ln}")
    print(f"  （合计 {n} 处）")

    print("\n=== 行号引用超出文件总行数 ===")
    oob = 0
    for path, lineno, doc, docline in refs:
        p = ROOT / path
        if not p.exists():
            continue
        total = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        if lineno > total:
            oob += 1
            print(f"  {path}:{lineno} 超出（共 {total} 行） ← {doc}:{docline}")
    print(f"  （合计 {oob} 处）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
