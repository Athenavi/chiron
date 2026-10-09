#!/usr/bin/env python
"""把仓库内的 `docs/wiki/**` 同步到 GitHub Wiki（**派生镜像**，仓库内才是单一事实源）。

## 为什么需要它

GitHub Wiki 是**独立仓库**（`<repo>.wiki.git`），而且它的页面是**扁平**的 —— **没有子目录**。
所以本脚本做两件事：

1. **把目录树编码进页名**：`core/architecture.md` → `Core-Architecture.md`；
   `README.md` → `Home.md`（wiki 首页）。
2. **改写链接**，让页面在 wiki 里点得动：
   * 指向 **wiki 内部**的页 → 换成页名（`../overview.md` → `Overview`）；
   * 指向 **仓库文件**的链接（`../transcript-contract.md`）→ 换成 GitHub blob URL
     （wiki 页面服务不了仓库文件，只能给绝对链接）；
   * 绝对 URL 与纯锚点 → 原样保留。

另外生成 `_Sidebar.md`（每个页面都会渲染的导航）与 `_Footer.md`（标注"请勿直接编辑 wiki"）。

## 用法

    python scripts/sync_github_wiki.py             # 预演：生成到临时目录并打印，不推送
    python scripts/sync_github_wiki.py --push      # 真正推送到 <repo>.wiki.git
    python scripts/sync_github_wiki.py --repo owner/name --branch main --push

## 安全约定

* **默认预演**，`--push` 才会推送；推送前会打印将要写入的页名清单。
* **不做 force push**；无变化则不提交。
* 推送失败会打印**可手动执行的命令**，不吞错误。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WIKI_SRC = REPO_ROOT / "docs" / "wiki"

LINK = re.compile(r"\]\(([^)\s]+)(#[^)\s]*)?\)")
FOOTER = (
    "---\n\n"
    "_本页由仓库内 `docs/wiki/` 同步生成（`scripts/sync_github_wiki.py`）。"
    "**请不要直接编辑本 wiki** —— 改动会下次同步时被覆盖；请改仓库里的对应文件。_\n"
)


def page_name(rel: pathlib.Path) -> str:
    """`core/architecture.md` → `Core-Architecture.md`；`README.md` → `Home.md`。"""
    if rel.name == "README.md" and rel.parent == pathlib.Path("."):
        return "Home.md"
    parts = []
    for seg in rel.with_suffix("").parts:
        parts.extend(w.capitalize() for w in seg.split("-"))
    return "-".join(parts) + ".md"


def detect_repo() -> tuple[str, str]:
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=REPO_ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO_ROOT,
                                capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "", "main"
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+)", url)
    return (m.group(1) if m else ""), (branch or "main")


def build_pages(repo: str, branch: str) -> dict[str, str]:
    sources = sorted(p for p in WIKI_SRC.rglob("*.md") if p.is_file())
    if not sources:
        raise SystemExit(f"没有找到任何源文件：{WIKI_SRC}")

    # 先建 相对路径 → 页名 的映射，供链接改写用
    to_page = {p.relative_to(WIKI_SRC).as_posix(): page_name(p.relative_to(WIKI_SRC)) for p in sources}

    pages: dict[str, str] = {}
    for p in sources:
        rel = p.relative_to(WIKI_SRC)
        text = p.read_text(encoding="utf-8")
        name = to_page[rel.as_posix()]

        def repl(m: re.Match, here: pathlib.Path = rel) -> str:
            target, anchor = m.group(1), m.group(2) or ""
            if target.startswith(("http://", "https://", "mailto:", "#")):
                return m.group(0)
            # 先按「相对 wiki 根的绝对路径」规范化 —— 不要手写字符串剥离，
            # 那样会把 `../x.md` 里的 `docs/` 前缀丢掉（第一版就是这么错的）。
            resolved = pathlib.Path(os.path.normpath(WIKI_SRC / here.parent / target))
            try:
                repo_rel = resolved.relative_to(REPO_ROOT).as_posix()
            except ValueError:
                return m.group(0)                     # 指到仓库外，保持原样
            if repo_rel.startswith("docs/wiki/"):
                page = to_page.get(repo_rel[len("docs/wiki/"):])
                if page:
                    return f"]({page}{anchor})"        # wiki 内部页 → 页名
            if repo:
                return f"](https://github.com/{repo}/blob/{branch}/{repo_rel}{anchor})"
            return m.group(0)

        pages[name] = LINK.sub(repl, text).rstrip() + "\n\n" + FOOTER

    pages["_Sidebar.md"] = build_sidebar(to_page)
    return pages


def build_sidebar(to_page: dict[str, str]) -> str:
    lines = ["### Chiron Wiki", "", "- [首页](Home)"]
    groups: dict[str, list[tuple[str, str]]] = {}
    for rel, name in sorted(to_page.items()):
        if rel == "README.md":
            continue
        parts = rel.split("/")
        if len(parts) == 1:
            groups.setdefault("", []).append((rel, name))
        else:
            groups.setdefault(parts[0], []).append((rel, name))
    for group, items in groups.items():
        lines.append("")
        if group:
            lines.append(f"**{group.replace('-', ' ').title()}**")
        for rel, name in items:
            lines.append(f"- [{pathlib.PurePosixPath(rel).stem.replace('-', ' ').title()}]({name[:-3]})")
    lines.append("")
    return "\n".join(lines)


def push(pages: dict[str, str], repo: str, dry_run: bool) -> int:
    if not repo:
        print("!! 无法识别 GitHub 仓库（git remote origin）⇒ 只能预演。用 --repo owner/name 指定。")
        return 2

    staging = pathlib.Path(tempfile.mkdtemp(prefix="chiron-wiki-"))
    try:
        print(f"预演目录：{staging}")
        for name, body in sorted(pages.items()):
            (staging / name).write_text(body, encoding="utf-8")
            print(f"  {name:<34} {len(body):>6} 字符")

        if dry_run:
            print("\n（预演结束，未推送。加 --push 才会推送到 "
                  f"https://github.com/{repo}.wiki.git）")
            return 0

        clone = staging / "_wiki_repo"
        wiki_url = f"https://github.com/{repo}.wiki.git"
        print(f"\n克隆 {wiki_url} …")
        r = subprocess.run(["git", "clone", "--depth", "1", wiki_url, str(clone)])
        if r.returncode != 0:
            print("!! 克隆失败。可能是：① wiki 还没初始化（先在 GitHub 上建一个页面）"
                  "② 需要 PAT 认证。\n   手动替代："
                  f"\n     git clone {wiki_url} /tmp/chiron-wiki"
                  f"\n     cp <预演目录>/*.md /tmp/chiron-wiki/ && cd /tmp/chiron-wiki"
                  "\n     git add -A && git commit -m 'docs: sync wiki from docs/wiki' && git push")
            return 3

        for f in clone.glob("*.md"):
            f.unlink()
        for name, body in pages.items():
            (clone / name).write_text(body, encoding="utf-8")

        subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
        diff = subprocess.run(["git", "status", "--porcelain"], cwd=clone,
                              capture_output=True, text=True, check=True).stdout.strip()
        if not diff:
            print("无变化，不提交。")
            return 0
        subprocess.run(["git", "commit", "-m", "docs: sync wiki from docs/wiki"],
                       cwd=clone, check=True)
        r = subprocess.run(["git", "push", "origin", "HEAD"], cwd=clone)
        if r.returncode != 0:
            print("!! 推送失败（多为认证问题：wiki 是独立仓库，通常需要 PAT）。")
            return 4
        print("✓ 已推送到 GitHub Wiki。")
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="同步 docs/wiki/ 到 GitHub Wiki（默认预演）")
    ap.add_argument("--push", action="store_true", help="真正推送（默认只预演）")
    ap.add_argument("--repo", default="", help="owner/name，默认从 git remote origin 推断")
    ap.add_argument("--branch", default="", help="仓库默认分支，默认取当前分支")
    args = ap.parse_args()

    repo, branch = detect_repo()
    repo = args.repo or repo
    branch = args.branch or branch
    print(f"仓库：{repo or '(未识别)'} · 分支：{branch} · 源：{WIKI_SRC}")

    pages = build_pages(repo, branch)
    return push(pages, repo, dry_run=not args.push)


if __name__ == "__main__":
    raise SystemExit(main())
