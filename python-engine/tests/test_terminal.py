"""Tests for persistent_shell — 持久终端状态跨调用。"""
from __future__ import annotations

import sys

import pytest

from app.tools.context import set_tool_context
from app.tools.terminal import _terminal, persistent_shell


@pytest.mark.asyncio
async def test_basic_command_output():
    set_tool_context(session_id="t-sess-1")
    out = await persistent_shell("echo hello-persist")
    assert out.get("error") is None
    assert "hello-persist" in out["output"]
    assert out["persistent"] is True
    await _terminal.close_all()


@pytest.mark.asyncio
async def test_cwd_persists_across_calls():
    """cd 一次后，后续调用在同一目录执行（deepseek persistent-bash 语义）。

    只能 cd 到沙箱 workspace 内（逃逸拦截会阻止沙箱外路径）。
    """
    set_tool_context(session_id="t-sess-2")
    from app.tools.sandbox import workspace_dir
    sub = workspace_dir() / "persist-sub"
    sub.mkdir(parents=True, exist_ok=True)
    await persistent_shell("cd persist-sub")
    if sys.platform == "win32":
        pwd = await persistent_shell("echo %CD%")
    else:
        pwd = await persistent_shell("pwd")
    assert str(sub) in pwd["output"], f"expected cwd {sub}, got {pwd}"
    await _terminal.close_all()


@pytest.mark.asyncio
async def test_session_isolation():
    set_tool_context(session_id="t-sess-a")
    await persistent_shell("echo AA")
    set_tool_context(session_id="t-sess-b")
    out = await persistent_shell("echo BB")
    assert "BB" in out["output"]
    await _terminal.close_all()


@pytest.mark.asyncio
async def test_timeout_resets_shell():
    set_tool_context(session_id="t-sess-3")
    # S5-1 起准入走白名单：`ping` / `sleep` 都不在白名单内，改用跨平台的 python 长命令
    # （python 本就在白名单里）。测试意图不变：制造一个必然超时的命令。
    cmd = 'python -c "import time; time.sleep(30)"'
    out = await persistent_shell(cmd, timeout=1)
    assert out["reset"] is True and "timed out" in out["output"]
    # 重置后仍可执行
    out2 = await persistent_shell("echo after-reset")
    assert "after-reset" in out2["output"]
    await _terminal.close_all()


@pytest.mark.asyncio
async def test_exit_code_captured():
    set_tool_context(session_id="t-sess-4")
    ok = await persistent_shell("echo ok")
    assert ok["exit_code"] == 0
    if sys.platform == "win32":
        bad = await persistent_shell("exit /b 7")
    else:
        bad = await persistent_shell("exit 7")
    assert bad["exit_code"] == 7
    await _terminal.close_all()


# ── S5-1 回归：persistent_shell 的准入检查必须与 shell_exec 同源 ──
#
# 修复前它只做逃逸拦截、**没有可执行名白名单**，于是 `persistent_shell("rm -rf x")`
# 与 `shell_exec("rm -rf x")` 的判定不一致；而持久 shell 是**长驻进程**，缺口比
# 一次性 shell 更危险（命中一次即在整个会话里可用）。


def test_check_command_text_rejects_non_whitelisted_executable():
    from app.tools.sandbox import check_command_text

    reason = check_command_text("rm -rf workspace")
    assert reason is not None
    assert "not allowed" in reason


def test_check_command_text_checks_every_segment_of_a_pipeline():
    """管道里的**每个**子命令都要过白名单，不能只看第一个。"""
    from app.tools.sandbox import check_command_text

    assert check_command_text("echo a | grep a") is None  # 两段都合法 → 放行
    reason = check_command_text("echo a | rm b")
    assert reason is not None and "rm" in reason


def test_check_command_text_rejects_escape_patterns_and_empty():
    from app.tools.sandbox import check_command_text

    assert check_command_text("cat /etc/passwd") is not None  # Unix 绝对路径
    assert check_command_text("cd ../..") is not None  # 父目录跳转
    assert check_command_text("   ") is not None  # 空命令


@pytest.mark.asyncio
async def test_persistent_shell_blocks_non_whitelisted_command():
    set_tool_context(session_id="t-sess-guard")
    out = await persistent_shell("rm -rf workspace")
    assert out["exit_code"] == -1
    assert "blocked" in out["output"]
    assert out["persistent"] is True
    # 拦截不影响后续合法命令（长驻 shell 未被污染、无需重建）
    ok = await persistent_shell("echo still-alive")
    assert "still-alive" in ok["output"]
    await _terminal.close_all()
