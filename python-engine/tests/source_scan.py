"""源码守卫的公共判据工具。

**为什么需要**：源码守卫常靠"文件里有没有这段字面量"来断言接线，但**注释也能满足字面量匹配** ——
把接线注释掉（`# await …start_periodic_cleanup(on_expired=…)`）时子串仍在，守卫照样通过，
而那正是它要防的回归（"接线没生效"）。

2026-10-09 实测到两处这样的漏洞（都先证伪、再修）：

* `tests/test_session_cleanup_trigger.py` 的装配断言 —— 注释掉 `main.py` 的接线后仍 exit 0；
* `tests/test_thinking_event.py` 的两条分派断言 —— 注释掉 `subagent_runner.py` /
  `collaboration.py` 的分支后仍 exit 0。

因此判据要建立在**可执行代码**上：先剥注释再匹配。剥法刻意**逐字符保留其余文本**
（而不是 `" ".join(token.string)`）—— 后者会插入空格，把 `fn(on_expired=` 这类子串拆开，
反而让断言永远匹配不上。
"""

from __future__ import annotations

import io
import pathlib
import tokenize


def without_comments(src: str) -> str:
    """删掉所有 `#` 注释，其余文本逐字符保留（`#` 注释不跨行，故按列裁剪即可）。"""
    lines = src.splitlines(keepends=True)
    spans = [
        (tok.start, tok.end)
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type == tokenize.COMMENT
    ]
    # 从后往前删：前面的删除会影响后面 token 的列号
    for (row, col), (_, end_col) in reversed(spans):
        line = lines[row - 1]
        lines[row - 1] = line[:col] + line[end_col:]
    return "".join(lines)


def code_of(path: str | pathlib.Path) -> str:
    """读源码文件并剥注释 —— 源码守卫的常用入口。"""
    return without_comments(pathlib.Path(path).read_text(encoding="utf-8"))
