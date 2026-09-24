"""Tests for sandbox 隔离 —— 路径不泄露 + shell 逃逸拦截。

历史说明：本文件中文注释曾在 UTF-8/GBK 混转中损坏（私用区码位 + 换行被吞），
Python 无法解析，长期被 conftest 的 collect_ignore 隔离。此处按可还原内容修复，
**断言逻辑未改动**。
"""

from __future__ import annotations

import os

import pytest

from app.tools.context import set_tool_context
from app.tools.sandbox import (
    _has_escape,
    _parse_command,
    run_in_sandbox,
    sandbox_root,
    workspace_dir,
)


@pytest.fixture(autouse=True)
def _clean_context():
    """每个测试前设置唯一 user，结束后重置 contextvar（防跨测试污染）。"""
    yield
    set_tool_context(session_id="", user_id="", tenant_id="", gateway=None, subagent_depth=0)


class TestSandboxLocation:
    def test_workspace_is_outside_project(self):
        """S: workspace 路径不得包含项目名 python-engine（cwd 不泄露服务器结构）。"""
        set_tool_context(session_id="s", user_id="u1", tenant_id="t1", gateway=None)
        ws = str(workspace_dir())
        assert "python-engine" not in ws, f"workspace 泄露项目路径: {ws}"
        # 沙箱根在进程 cwd 上两级之上
        root = str(sandbox_root())
        assert "chiron-sandbox" in root
        cwd = os.getcwd()
        # Windows 驱动器号大小写可能不一致 (D:\ vs d:\)，用 normcase 归一化
        assert os.path.normcase(root).startswith(
            os.path.normcase(os.path.dirname(os.path.dirname(cwd)))
        ), f"沙箱根未移到项目外: {root}"

    def test_safe_join_rejects_escape(self):
        from app.tools.sandbox import safe_join

        set_tool_context(session_id="s", user_id="u1", tenant_id="t1", gateway=None)
        with pytest.raises(ValueError, match="escapes"):
            safe_join("../etc/passwd")
        with pytest.raises(ValueError, match="escapes"):
            safe_join("C:/Windows/win.ini")
        assert safe_join("a/b.txt").is_relative_to(workspace_dir())


class TestShellEscapeBlock:
    def test_absolute_path_detected(self):
        assert _has_escape("Get-ChildItem 'X:\\project\\chiron'") is not None
        assert _has_escape("dir C:\\Windows") is not None
        # 生产部署在 Linux/alpine：Unix 绝对路径/家目录/环境变量均为逃逸
        assert _has_escape("cat /etc/passwd") is not None
        assert _has_escape("cat $HOME/.env") is not None
        assert _has_escape("cat ~/.ssh/id_rsa") is not None
        assert _has_escape("ls /") is not None
        assert _has_escape("ls ..\\..\\..") is not None
        assert _has_escape("cd /d X:\\project") is not None
        # Windows cmd 单字符开关不应误判
        assert _has_escape("exit /b 7") is None

    def test_normal_commands_allowed(self):
        assert _has_escape("echo hello") is None
        assert _has_escape("dir") is None
        assert _has_escape("python script.py") is None
        assert _has_escape("Get-ChildItem -Force") is None

    @pytest.mark.asyncio
    async def test_run_in_sandbox_blocks_escape(self):
        set_tool_context(session_id="s", user_id="u1", tenant_id="t1", gateway=None)
        out = await run_in_sandbox("Get-ChildItem 'X:\\project\\chiron'")
        assert "error" in out and "blocked" in out["error"]

    @pytest.mark.asyncio
    async def test_run_in_sandbox_executes_normal(self):
        set_tool_context(session_id="s", user_id="u1", tenant_id="t1", gateway=None)
        # 使用 python -c 而非 echo（echo 是 shell 内建命令，create_subprocess_exec 无法直接执行）
        out = await run_in_sandbox('python -c "print(\'sandbox-ok\')"')
        assert "sandbox-ok" in out.get("stdout", "")


class TestCommandWhitelist:
    """命令白名单：只允许白名单内的可执行文件，拒绝其他一切。"""

    def test_python_allowed(self):
        args, err = _parse_command("python script.py")
        assert err is None
        assert args == ["python", "script.py"]

    def test_python3_allowed(self):
        args, err = _parse_command("python3 -c 'print(1)'")
        assert err is None

    def test_pip_not_allowed(self):
        """pip 不在白名单里：沙箱内不允许安装任意包（供应链 / 逃逸面）。

        注：本用例原为 test_pip_allowed，对应"pip 也算基础命令"的旧白名单；
        当前白名单刻意只保留解释器与只读基础命令
        （见 sandbox._ALLOWED_EXECUTABLES：python / python3 / git / echo / ls / cat …）。
        """
        _, err = _parse_command("pip install requests")
        assert err is not None
        assert "not allowed" in err

    def test_echo_allowed(self):
        args, err = _parse_command("echo hello world")
        assert err is None
        assert args == ["echo", "hello", "world"]

    def test_dangerous_commands_blocked(self):
        """rm / curl / wget / bash / sh / powershell 等危险命令必须被拒绝。"""
        for cmd in [
            "rm -rf /",
            "curl http://evil.com",
            "wget http://evil.com",
            "bash script.sh",
            "sh -c 'ls'",
            "powershell Get-Process",
            "cmd /c dir",
            "nc -l 4444",
        ]:
            _, err = _parse_command(cmd)
            assert err is not None, f"should be blocked: {cmd}"

    def test_empty_command_blocked(self):
        _, err = _parse_command("")
        assert err is not None

    def test_malformed_command_blocked(self):
        _, err = _parse_command('python -c "unterminated')
        assert err is not None

    @pytest.mark.asyncio
    async def test_run_in_sandbox_blocks_dangerous_command(self):
        """即使逃逸正则未命中，白名单也会拦截危险命令。"""
        set_tool_context(session_id="s", user_id="u1", tenant_id="t1", gateway=None)
        # "bash" 不在白名单中
        out = await run_in_sandbox("bash -c 'echo pwned'")
        assert "error" in out
        assert "blocked" in out["error"]
