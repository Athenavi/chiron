"""文档断链守卫：Markdown 里被引用的其它文档必须真的存在。

扫描范围**只有被 git 跟踪的 `.md`**。代码文件里的 `'docs/xxx.md'` 绝大多数是示例字符串
（工具参数说明、提示词样例），不是引用；把它们扫进来只会制造误报，而误报会逼人写
`docs/a.md` 这类无意义的占位路径来绕过门禁。

口径：
* 只查被 git 跟踪的 `.md`（`git ls-files`），跳过 `vendor/`、`node_modules/`、`dist/`；
* 跳过测试/评测 fixture 目录——那里的路径是合成的；
* 围栏代码块整块跳过；模板占位行（全大写字段名 + 冒号）整行跳过，二者都是示例文本；
* 两种写法都查：`docs/<名字>.md` 与 `[x](y.md)`。相对写法以**引用方所在目录**为基准，
  命中后归一成仓库相对路径，与前者共用同一份基线；
* 存量债务走 `scripts/doc_link_baseline.txt`，**新增即失败**，基线只许缩小。

`--write-baseline` 刷新基线（只在有意接受新债务时用）；`--show-known` 列出存量断链的引用方。
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
BASELINE = REPO / "scripts" / "doc_link_baseline.txt"
DOC_REF = re.compile(r"(?<![\w./-])docs/([A-Za-z0-9_./-]+\.md)")
REL_LINK = re.compile(r"\]\(\s*([^)\s]+?)\s*(?:\"[^\"]*\"|'[^']*')?\s*\)")
FENCE = re.compile(r"^\s*(```|~~~)")
TEMPLATE_FIELD = re.compile(r"^\s*[A-Z][A-Z0-9_]{2,}\s*:")
SKIP_PREFIXES = ("vendor/", "node_modules/", "frontend-vue/dist/", "python-engine/data/")
SKIP_PARTS = ("/tests/", "/__tests__/", "/evals/", "/testdata/")
SYNTHETIC_REFS = {"docs/a.md", "docs/b.md", "docs/1.md", "docs/2.md", "docs/3.md"}
#: 上游对标项目自己仓库里的文档名。它们出现在 `docs/archive/reasonix-*.md` 的逐文件枚举里，
#: 描述的是**别人的仓库**。写死名单而不是豁免整个 `docs/archive/`：后者会让归档文档里
#: 指向本仓真实文档的引用一起失去检查。
#: ⚠ 只写文件名，不带 `docs/` 前缀 —— 带了会被本守卫自己读成一条引用。
UPSTREAM_DOC_REFS = frozenset(
    "docs/" + name
    for name in (
        "BILLING.md",
        "CLI.zh-CN.md",
        "DOCS_STANDARD.md",
        "GUIDE.zh-CN.md",
        "MIGRATING.md",
        "ROADMAP.md",
        "SESSION_MEMORY_RETRIEVAL.md",
        "SPEC.md",
        "STUDIO_PARITY.md",
        "STUDIO_SHELL_BOUNDARIES.md",
        "THEME_ASSETS.md",
        "THEME_AUTHOR_GUIDE.md",
        "THEME_PACK.md",
        "TOOL_APPROVAL_MODES.md",
        "USAGE_CATALOG.md",
        "design/TRUSTED_EXECUTION.md",
    )
)
SCAN_SUFFIX = ".md"


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
    checked_rel = 0
    for rel in tracked_files():
        if not rel.endswith(SCAN_SUFFIX) or rel.startswith(SKIP_PREFIXES):
            continue
        if any(part in f"/{rel}" for part in SKIP_PARTS):
            continue
        try:
            text = (REPO / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        in_fence = False
        for lineno, line in enumerate(text.splitlines(), start=1):
            if FENCE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue

            # ① `docs/<名字>.md` 口径
            if not TEMPLATE_FIELD.match(line):
                for name in DOC_REF.findall(line):
                    ref = f"docs/{name}"
                    if ref in SYNTHETIC_REFS or ref in UPSTREAM_DOC_REFS:
                        continue
                    checked += 1
                    if (REPO / ref).exists():
                        continue
                    missing.setdefault(ref, []).append(f"{rel}:{lineno}")

            # ② 相对链接口径：解析基准 = 引用方所在目录
            for target in REL_LINK.findall(line):
                if target.startswith(("#", "/", "http://", "https://", "mailto:")):
                    continue
                clean = target.split("#", 1)[0]
                if not clean.endswith(".md"):
                    continue
                checked_rel += 1
                resolved = (REPO / rel).parent / clean
                if resolved.exists():
                    continue
                try:
                    key = resolved.resolve().relative_to(REPO).as_posix()
                except ValueError:
                    key = clean  # 解析到仓库外：按原样记，别静默放过
                missing.setdefault(key, []).append(f"{rel}:{lineno}")

    if "--write-baseline" in sys.argv:
        write_baseline(missing)
        return 0

    if "--show-known" in sys.argv:
        for ref in sorted(missing):
            print(f"{ref}  （{len(set(missing[ref]))} 处引用）")
            for r in sorted(set(missing[ref])):
                print(f"    ← {r}")
        return 0

    known = load_baseline()
    new_missing = {ref: refs for ref, refs in missing.items() if ref not in known}
    still_missing = sorted(ref for ref in missing if ref in known)
    resolved = sorted(ref for ref in known if not (REPO / ref).exists() and ref not in missing)

    print(f"检查了 {checked} 处 docs/ 引用 + {checked_rel} 处相对 .md 链接；存量基线 {len(known)} 条")
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
