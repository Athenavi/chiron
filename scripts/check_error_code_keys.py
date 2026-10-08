#!/usr/bin/env python3
"""错误码键集守卫：Go `Code*` ↔ 三语言 `errors.ts`。

**为什么需要它**：`docs/error-codes.md` 把"三语言 `errors.ts` 的键必须与后端 `Code*`
一一对应、且三语言键集完全相同"写成**硬要求**（缺键时客户端只能回退 `error` 原文，
用户看到英文/中文原文而不是本地化文案），**但该文档自己承认这一步"目前靠人工核对"**：

> **仍未覆盖**：三语言 `errors.ts` 与 `Code*` 常量的**键集一致性**目前靠人工核对。

人工核对的代价很具体：新增一个 `Code*` 却漏补某一种语言，**不会有任何报错** ——
只有该语言的用户会看到未本地化的原文。本脚本把那份清单变成断言。

判定口径（只查**契约键**，不查 `errors.ts` 里的其它界面文案键 —— 那些不属于错误码契约）：

  1. `internal/api/error_codes.go` 里每个 `Code* = "value"` 的 **value**，必须在
     `frontend-vue/src/locales/{zh-CN,en-US,ar}/errors.ts` 里都存在同名键；
  2. 文档点名的 4 个**前端专用**键（`timeout` / `network_error` / `payload_too_large` /
     `http_status`）也必须在三语言里都存在（它们由客户端网络层产生，后端不产生）；
  3. **三语言的键集必须完全相同**（多一种语言多一个键也是漂移）。

用法：
    python scripts/check_error_code_keys.py
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
GO_FILE = ROOT / "internal" / "api" / "error_codes.go"
LANGS = ("zh-CN", "en-US", "ar")

#: 由**客户端网络层**产生、后端不产生的键（`docs/error-codes.md` 的"前端专用"一行）。
FRONTEND_ONLY = ("timeout", "network_error", "payload_too_large", "http_status")

#: `errors.ts` 里顶层键的形态：两空格缩进 + `key:`（不匹配嵌套对象里的键）。
_TS_KEY = re.compile(r"(?m)^  ([a-z][a-z0-9_]*)\s*:")


def go_codes() -> list[str]:
    text = GO_FILE.read_text(encoding="utf-8")
    # ⚠ 值里**允许数字**（`[a-z0-9_]+`）：只写 `[a-z_]+` 的话，像 `not_found_v2` 这样的码会被
    # **静默漏掉** ⇒ 守卫就不再要求它 —— 2026-10-09 变异验证时踩到（改名成 `not_found_v2`
    # 后守卫仍然绿，正是因为新值没被解析出来）。
    return sorted(set(re.findall(r'(?m)^\s*Code[A-Za-z0-9_]*\s*=\s*"([a-z0-9_]+)"', text)))


def ts_keys(lang: str) -> set[str]:
    path = ROOT / "frontend-vue" / "src" / "locales" / lang / "errors.ts"
    if not path.exists():
        return set()
    return set(_TS_KEY.findall(path.read_text(encoding="utf-8")))


def main() -> int:
    if not GO_FILE.exists():
        print(f"FAIL: 找不到 {GO_FILE}", file=sys.stderr)
        return 1

    codes = go_codes()
    if not codes:
        print("FAIL: 从 error_codes.go 解析不到任何 Code* 常量（写法变了？）", file=sys.stderr)
        return 1

    locales = {lang: ts_keys(lang) for lang in LANGS}
    required = set(codes) | set(FRONTEND_ONLY)

    problems: list[str] = []
    for lang, keys in locales.items():
        if not keys:
            problems.append(f"{lang}: 解析不到任何键（文件缺失或写法变了？）")
            continue
        missing = sorted(required - keys)
        if missing:
            problems.append(f"{lang}: 缺契约键 {missing}")

    present = {lang: keys for lang, keys in locales.items() if keys}
    if len(present) == len(LANGS):
        base_lang = LANGS[0]
        for lang in LANGS[1:]:
            only_here = sorted(present[lang] - present[base_lang])
            only_base = sorted(present[base_lang] - present[lang])
            if only_here or only_base:
                problems.append(
                    f"三语言键集不一致：只在 {lang} {only_here or '—'}；"
                    f"只在 {base_lang} {only_base or '—'}"
                )

    if problems:
        print("FAIL: 错误码键集漂移（docs/error-codes.md 的硬要求）：", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        print(
            "\n补法见 docs/error-codes.md「新增 / 修改错误码的清单」：先写 zh-CN，再补 en-US / ar。",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: 错误码键集一致 —— Go Code* {len(codes)} 个 + 前端专用 {len(FRONTEND_ONLY)} 个"
        f" 在 {'/'.join(LANGS)} 三语言中都存在，且三语言键集完全相同"
        f"（各 {len(present[LANGS[0]])} 键）。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
