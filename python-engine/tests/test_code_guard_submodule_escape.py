"""沙箱逃逸回归：**白名单模块夹带的危险子模块**必须被拦。

背景（实测 2026-10-08）：`asyncio` 是给模型代码用的白名单模块（`await asyncio.sleep()`），
但它夹带 `asyncio.subprocess` —— `create_subprocess_exec/shell` 就是**任意命令执行**，
绕开工具白名单与 exec 审计。此前：

- 静态层只比较模块名的**根段**（`name.split(".")[0]`），而 `DANGEROUS_MODULES` 里的
  `asyncio.tasks` / `http.client` 是**点分条目** ⇒ 这些条目从未生效，`import asyncio.subprocess`
  一路放行；
- 运行时层 `_guarded_import` 同样只看根段（`asyncio` 在白名单里）⇒ 真实 import 成功。

现在静态与运行时两层都按**逐级前缀**判定。本文件把这个洞钉住，同时确保没有误伤
（`asyncio` 本身、`json`/`math` 等仍可用）。
"""

import pytest

from app.tools.code_guard import check_static, safe_builtins

DANGEROUS = [
    "import asyncio.subprocess",  # 起进程 = 沙箱逃逸主路径
    "import asyncio.subprocess as sp",
    "from asyncio import subprocess",
    "import asyncio.tasks",  # 点分条目：修改前形同虚设，一并钉住
    "import http.client",
]

SAFE = ["import asyncio", "import json", "import math", "import datetime", "import re"]

ROOTS = ["import os", "import subprocess", "import socket", "import importlib"]


@pytest.mark.parametrize("code", DANGEROUS)
def test_static_blocks_dangerous_submodules(code: str) -> None:
    assert check_static(code) is not None, f"{code} 必须被静态守卫拒绝"


@pytest.mark.parametrize("code", ROOTS)
def test_static_still_blocks_dangerous_roots(code: str) -> None:
    assert check_static(code) is not None, f"{code} 必须被静态守卫拒绝"


@pytest.mark.parametrize("code", SAFE)
def test_static_does_not_break_safe_modules(code: str) -> None:
    assert check_static(code) is None, f"{code} 是模型常用能力，不能误伤"


def test_runtime_guard_blocks_asyncio_subprocess() -> None:
    namespace: dict = {"__builtins__": safe_builtins()}
    with pytest.raises(RuntimeError, match="blocked by runtime guard"):
        exec("import asyncio.subprocess", namespace)  # noqa: S102 — 被测对象就是沙箱执行路径


def test_runtime_guard_keeps_asyncio_usable() -> None:
    namespace: dict = {"__builtins__": safe_builtins()}
    exec("import asyncio\nok = asyncio.sleep", namespace)  # noqa: S102
    assert namespace["ok"] is not None
