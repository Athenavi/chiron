"""注释卫生守卫：只检查**本次改动新增的行**，让注释债务只减不增。

为什么只查 diff：存量里大量注释在解释「为什么」（取舍、踩过的坑），全量清理必然误伤。
新写/改动的代码按 `CONTRIBUTING.md` §5 第 6 条来，旧代码保持不动。

**两级判定**——分开是为了不制造假阳性：

* **error**（`--strict` 时退出码 1）：分隔装饰线 `// ─────`、无主 TODO/FIXME。
  这两类零争议：装饰线没有信息量，无主 TODO 是没有收件人的许愿。
* **warn**（永远不失败）：**函数体内**的连续注释 ≥6 行。顶层注释（模块说明、
  导出符号文档注释）豁免 —— `go doc` 与读者都要靠它，报它等于把护栏变成噪音源。

用法：
    python scripts/check_comment_hygiene.py                # 对比 origin/main
    python scripts/check_comment_hygiene.py --base HEAD~1
    python scripts/check_comment_hygiene.py --strict       # 有 error 时失败
    COMMENT_HYGIENE_BASE=<sha> ...                         # CI 用（PR 的 base / push 的 before）

CI 里找不到可比基线时**直接失败**：否则这门禁会静默变成空转。
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
MAX_BLOCK = 6
#: 只认本仓自己的源码；vendor 与生成物不属于我们的注释债。
SKIP_PREFIXES = ("vendor/", "node_modules/", "frontend-vue/dist/", "python-engine/data/")
SOURCE_SUFFIXES = {".go", ".py"}
COMMENT = re.compile(r"^\s*(//|#)")
DECOR = re.compile(r"^\s*(//|#)\s*[─━=\-—_*]{4,}\s*$")
#: 只认**标记**形态（标记词后紧跟冒号或左括号），不认行文里出现的这几个词 ——
#: 否则解释规则的注释会被规则自己判违规。
WISH = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b\s*[:(]")
#: 带责任人 / issue / 文档链接的 TODO 算已认领，不报。
OWNED_WISH = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b[^\n]*?(\(@|#[0-9]+|https?://)")


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    # 显式 UTF-8：Windows 上 text=True 会按 GBK 解码，中文注释直接抛 UnicodeDecodeError
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def base_of(argv: list[str]) -> str:
    for i, a in enumerate(argv):
        if a == "--base" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--base="):
            return a.split("=", 1)[1]
    env = os.environ.get("COMMENT_HYGIENE_BASE", "").strip()
    if env.strip("0"):  # 首次 push 时 before 是全 0，不可用
        return env
    for cand in ("origin/main", "origin/master", "HEAD~1"):
        if _git("rev-parse", "--verify", "--quiet", cand).returncode == 0:
            return cand
    return ""


def merge_base(base: str) -> str | None:
    r = _git("merge-base", base, "HEAD")
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None


def changed_added_lines(base: str) -> dict[str, set[int]] | None:
    """返回 {文件: 新增行号}；比较范围是分叉点到**工作区**，未提交改动也算。"""
    anchor = merge_base(base) or base
    r = _git("diff", "--unified=0", "--no-color", anchor)
    if r.returncode != 0:
        return None
    out = r.stdout or ""
    files: dict[str, set[int]] = {}
    cur = ""
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:].strip()
            continue
        m = re.search(r"^@@ [^+]*\+(\d+)(?:,(\d+))?", line)
        if not cur or not m:
            continue
        start, count = int(m.group(1)), int(m.group(2) or 1)
        files.setdefault(cur, set()).update(range(start, start + count))
    return files


def is_source(path: str) -> bool:
    return pathlib.Path(path).suffix in SOURCE_SUFFIXES and not path.startswith(SKIP_PREFIXES)


def untracked_sources() -> set[str]:
    """未跟踪的源码文件：`git diff` 看不见它们，不单独收进来就会漏检。"""
    r = _git("ls-files", "--others", "--exclude-standard")
    if r.returncode != 0:
        return set()
    return {p for p in (r.stdout or "").splitlines() if is_source(p)}


def scan(path: str, added: set[int]) -> tuple[list[str], list[str]]:
    try:
        body = (REPO / path).read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return [], []
    errors: list[str] = []
    warns: list[str] = []
    block: list[tuple[int, str]] = []

    def flush() -> None:
        # 块内必须有新增行才算本次引入；只碰相邻旧块不该被反复报
        if len(block) >= MAX_BLOCK and any(n in added for n, _ in block):
            first_no, first_raw = block[0]
            # 顶层缩进为 0：模块说明 / 导出符号文档注释，豁免
            if len(first_raw) - len(first_raw.lstrip()) > 0:
                warns.append(
                    f"{path}:{first_no}-{block[-1][0]} 函数体内连续注释 {len(block)} 行"
                    " —— 讲设计取舍/背景推导的应移入 docs/"
                )
        block.clear()

    for no, raw in enumerate(body, start=1):
        if not COMMENT.match(raw):
            flush()
            continue
        if no in added:
            if DECOR.match(raw):
                errors.append(f"{path}:{no} 分隔装饰线：{raw.strip()[:60]}")
            el = WISH.search(raw)
            if el and not OWNED_WISH.search(raw):
                errors.append(f"{path}:{no} 无主 {el.group(1)}：{raw.strip()[:70]}")
        block.append((no, raw))
    flush()
    return errors, warns


def main() -> int:
    base = base_of(sys.argv)
    if not base:
        # CI 中静默跳过 = 伪门禁，比没有检查更糟
        print("FAIL: 找不到可比基线（用 --base 或 COMMENT_HYGIENE_BASE 指定）")
        return 1
    added = changed_added_lines(base)
    if added is None:
        # env 给的 base 可能因 force push 变成孤儿提交：回退到分叉点，
        # 既不空转也不误红。仍失败才判错，因为"无法对比"≈门禁失效。
        for cand in ("origin/master", "origin/main", "HEAD~1"):
            if _git("rev-parse", "--verify", "--quiet", cand).returncode != 0:
                continue
            added = changed_added_lines(cand)
            if added is not None:
                print(f"note: 基线 {base} 不可达，回退到 {cand}")
                base = cand
                break
    if added is None:
        print(f"FAIL: 无法对比基线 {base}（git diff 失败）；CI 里需 fetch-depth: 0")
        return 1
    targets = sorted(
        p for p in added
        if is_source(p)
    )
    # 未跟踪文件整个都是本次新增：所有行都算改动行，否则新文件里的装饰线会漏检
    fresh = untracked_sources()
    for p in fresh:
        try:
            total = len((REPO / p).read_text(encoding="utf-8").splitlines())
        except (UnicodeDecodeError, OSError):
            continue
        added[p] = set(range(1, total + 1))
    targets = sorted(set(targets) | fresh)
    errors: list[str] = []
    warns: list[str] = []
    for p in targets:
        e, w = scan(p, added[p])
        errors += e
        warns += w

    print(f"注释卫生（对比 {base}）：检查 {len(targets)} 个改动的源码文件")
    for line in errors:
        print(f"  error {line}")
    for line in warns:
        print(f"  warn  {line}")
    if not errors and not warns:
        print("OK: 无新增注释问题")
        return 0
    print(f"\n{len(errors)} 条 error、{len(warns)} 条 warn。"
          "error 在 --strict 下失败；warn 仅提示。")
    return 1 if (errors and "--strict" in sys.argv) else 0


if __name__ == "__main__":
    sys.exit(main())
