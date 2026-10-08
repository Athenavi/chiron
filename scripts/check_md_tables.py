"""Markdown 表格结构守卫：同一表格块内每行的**列数**必须一致。

**为什么需要它**：文档里的表格是手写的，而**列分隔符多一个 / 少一个**不会让 Markdown 报错 ——
它只是**静默地多出一列或少一格**，在编辑器与 GitHub 上看着"差不多"，表格却从此错位。
实测抓到过两处（`docs/development-roadmap.md` §3：一行多了一个 `|`、另一行少了行尾 `|`），
而当时的四个守卫**没有一个覆盖这个类别** —— `check_source_encoding` 管编码、
`check_tool_policy_parity` 管分级表同构、`check_doc_links` 管文档引用。

口径（刻意保守，避免误报）：
* 只查**被 git 跟踪**的 `.md`（跳过 `vendor/`、`node_modules/`、`frontend-vue/dist/`）；
* **块** = 连续的、以 `|` 开头的行；块内每行的**未转义** `|` 数必须与**块首行**一致；
* 行内的字面量竖线必须写成 `\\|`（Markdown 转义）—— 转义的不计入列数；
* 只处理以 `|` 开头的连续块：代码块里的管道、正文里的 `A | B` 一概不管；
* 存量走基线（`scripts/md_tables_baseline.txt`，**只应缩小**），**新增即失败**。

用法：
    python scripts/check_md_tables.py                  # 校验（CI / 本地）
    python scripts/check_md_tables.py --list           # 列出全部存量位置（file:line）
    python scripts/check_md_tables.py --write-baseline # 生成/下调基线（清理一批后执行）
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
BASELINE = REPO / "scripts" / "md_tables_baseline.txt"
SKIP_PREFIXES = ("vendor/", "node_modules/", "frontend-vue/dist/")
UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def tracked_markdown() -> list[str]:
    """被 git 跟踪、且不在跳过前缀下的 `.md`。"""
    out = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return [
        line.strip()
        for line in out.splitlines()
        if line.strip() and not line.strip().startswith(SKIP_PREFIXES)
    ]


def bad_blocks(text: str) -> list[tuple[int, int, int]]:
    """返回 `(块首行号, 期望列数, 实际列数)` —— 每个列数不一致的块一条。

    列数 = 未转义 `|` 的个数（`| a | b |` = 3）。以块**首行**为基准：
    表头写错时整块会被报出来，这正是我们要的（错在哪一行不重要，错在哪个表重要）。
    """
    out: list[tuple[int, int, int]] = []
    start: int | None = None
    expected = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.startswith("|"):
            count = len(UNESCAPED_PIPE.findall(line))
            if start is None:
                start, expected = lineno, count
            elif count != expected:
                out.append((start, expected, count))
                # 一个块只报一次：跳到下一个块，避免同一表被报很多遍
                start, expected = None, 0
        else:
            start, expected = None, 0
    return out


def load_baseline() -> dict[str, int]:
    if not BASELINE.exists():
        return {}
    out: dict[str, int] = {}
    for line in BASELINE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path, _, count = line.partition("\t")
        out[path] = int(count or 0)
    return out


def write_baseline(current: dict[str, int]) -> None:
    lines = [
        "# Markdown 表格结构基线（存量债务）：这些 .md 里存在列数不一致的表格块。",
        "# 由 `python scripts/check_md_tables.py --write-baseline` 生成；**只应缩小**。",
        "# 每清掉一个块就下调对应数字；新增不一致会被守卫直接判失败（不要往这里加）。",
        "# 格式：<仓库相对路径>\\t<允许的不一致块数>",
        "",
    ]
    lines += [f"{path}\t{count}" for path, count in sorted(current.items())]
    BASELINE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写入基线：{len(current)} 个文件 → {BASELINE.relative_to(REPO)}")


def main() -> int:
    found: dict[str, list[tuple[int, int, int]]] = {}
    checked = 0
    for rel in tracked_markdown():
        path = REPO / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        checked += 1
        hits = bad_blocks(text)
        if hits:
            found[rel] = hits

    if "--write-baseline" in sys.argv:
        write_baseline({rel: len(hits) for rel, hits in found.items()})
        return 0

    if "--list" in sys.argv:
        for rel in sorted(found):
            for start, expected, actual in found[rel]:
                print(f"{rel}:{start}  期望 {expected} 个 | ，实际 {actual}")
        print(f"检查了 {checked} 个 .md；不一致块 {sum(len(v) for v in found.values())} 个")
        return 0

    known = load_baseline()
    current = {rel: len(hits) for rel, hits in found.items()}
    new = {rel: n for rel, n in current.items() if n > known.get(rel, 0)}
    resolved = sorted(rel for rel in known if current.get(rel, 0) < known[rel])

    print(f"检查了 {checked} 个 .md；存量基线 {len(known)} 个文件")
    if resolved:
        print(f"提示：这些文件的存量已下降或清零，可下调/删除基线：{resolved}")
    if new:
        print("\nFAIL: 以下 .md 的表格列数不一致（**新增**）：")
        for rel in sorted(new):
            for start, expected, actual in found[rel]:
                print(f"  {rel}:{start}  期望 {expected} 个 | ，实际 {actual}")
        print("\n修好它，或（确实要接受新债务时）跑 --write-baseline。")
        return 1
    if known:
        print(
            f"OK: 无新增表格结构问题（已知 {sum(known.values())} 个块，"
            f"见 {BASELINE.relative_to(REPO)}）"
        )
    else:
        print("OK: 无新增表格结构问题（存量基线为空 —— 新增即失败）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
