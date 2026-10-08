"""文档断链守卫：仓库里被引用的设计文档必须真的存在（存量债务走基线白名单）。

为什么需要它：`docs/session-map-branch-design.md` 曾被 6 处源码引用却**不在仓库里**；同类问题
一次扫出 **19 份**从未提交过的设计文档（`git log --diff-filter=D` 无删除记录 ⇒ 不是被删）。
现有编码守卫与工具策略守卫都拦不住这类断链（2026-10-08 发现）。

**为什么用基线而不是一次性全改**：存量 19 处散落在 ~40 个文件的注释里，逐条改引用是一场大
churn；而"新断链"必须当场拦住。所以：
* 基线内（`scripts/doc_link_baseline.txt`）⇒ 只统计、不失败；
* 基线外 ⇒ **失败**（退出码 1），并打印引用方；
* 基线里已经存在的条目 ⇒ 提示可删（债务在缩小）。

`python scripts/check_doc_links.py --write-baseline` 可刷新基线（只在**有意**接受新债务时用）。

口径（刻意保守，避免误报）：
* 只查**被 git 跟踪**的文本文件（`git ls-files`），跳过 `vendor/`、`node_modules/`、`dist/`；
* 跳过测试/评测 fixture（`_test.go`/`.spec.ts`/`test_*`/`/tests/`/`/evals/`）——那里的
  `docs/a.md` 是**合成路径**，不是"指向某份设计文档"；
* 显式列出合成占位路径（`docs/a.md` 之类），理由同上；
* 接受 vendor 下**对标项目自己的**同名文档（形如 `docs/<名字>.md`，只要 vendor 里真有）。
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
BASELINE = REPO / "scripts" / "doc_link_baseline.txt"
DOC_REF = re.compile(r"(?<![\w./-])docs/([A-Za-z0-9_./-]+\.md)")
SKIP_PREFIXES = ("vendor/", "node_modules/", "frontend-vue/dist/", "python-engine/data/")
SKIP_PARTS = ("/tests/", "/__tests__/", "/evals/", "/testdata/")
SKIP_NAME_SUFFIXES = ("_test.go", ".spec.ts", ".test.ts", "_test.py")
SYNTHETIC_REFS = {"docs/a.md", "docs/b.md", "docs/1.md", "docs/2.md", "docs/3.md"}
TEXT_SUFFIXES = {".go", ".py", ".ts", ".vue", ".md", ".yml", ".yaml", ".json", ".toml", ".sql", ".sh"}


def load_baseline() -> set[str]:
    if not BASELINE.exists():
        return set()
    out = set()
    for line in BASELINE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line)
    return out


def write_baseline(missing: dict[str, list[str]]) -> None:
    lines = [
        "# 文档断链基线（存量债务）：这些 docs/*.md 被引用但不在仓库里。",
        "# 由 `python scripts/check_doc_links.py --write-baseline` 生成；**只应缩小**。",
        "# 每清掉一条就删一行；新增引用不存在的文档会被守卫直接判失败（不要往这里加）。",
        "",
    ]
    lines += sorted(missing)
    BASELINE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写入基线：{len(missing)} 条 → {BASELINE.relative_to(REPO)}")


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def main() -> int:
    missing: dict[str, list[str]] = {}
    checked = 0
    for rel in tracked_files():
        if rel.startswith(SKIP_PREFIXES) or pathlib.Path(rel).suffix not in TEXT_SUFFIXES:
            continue
        if any(part in f"/{rel}" for part in SKIP_PARTS):
            continue
        if rel.endswith(SKIP_NAME_SUFFIXES) or pathlib.Path(rel).name.startswith("test_"):
            continue
        try:
            text = (REPO / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name in DOC_REF.findall(text):
            ref = f"docs/{name}"
            if ref in SYNTHETIC_REFS:
                continue
            checked += 1
            if (REPO / ref).exists() or next(REPO.glob(f"vendor/**/{ref}"), None) is not None:
                continue
            missing.setdefault(ref, []).append(rel)

    if "--write-baseline" in sys.argv:
        write_baseline(missing)
        return 0

    if "--show-known" in sys.argv:
        # 清债用：列出每条存量断链**被谁引用**，便于逐条改引用/删引用
        for ref in sorted(missing):
            print(f"{ref}  （{len(set(missing[ref]))} 处引用）")
            for r in sorted(set(missing[ref])):
                print(f"    ← {r}")
        return 0

    known = load_baseline()
    new_missing = {ref: refs for ref, refs in missing.items() if ref not in known}
    still_missing = sorted(ref for ref in missing if ref in known)
    # 基线里已经存在的条目 = 债务已还，提示删掉（不失败）
    resolved = sorted(ref for ref in known if not (REPO / ref).exists() and ref not in missing)

    print(f"检查了 {checked} 处 docs/ 引用；存量基线 {len(known)} 条")
    if resolved:
        print(f"提示：基线里这些已不再被引用或已存在，可删：{resolved}")
    if new_missing:
        print("\nFAIL: 以下文档被引用但不存在（**新增断链**）：")
        for ref, refs in sorted(new_missing.items()):
            print(f"  {ref}")
            for r in sorted(set(refs)):
                print(f"      ← {r}")
        print("\n要么补文档，要么改引用；确实要接受新债务时才用 --write-baseline。")
        return 1

    print(f"OK: 无新增断链（已知债务 {len(still_missing)} 条，见 scripts/doc_link_baseline.txt）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
