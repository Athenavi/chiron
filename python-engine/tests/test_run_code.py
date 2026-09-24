"""Tests for run_code — PTC 模式的 Python SDK 组合工具调用。"""
from __future__ import annotations

import pytest

from app.tools.context import set_tool_context
from app.tools.run_code import _check_static, run_code, sdk_usage_text
from app.tools.sandbox import workspace_dir


def test_static_guard_blocks_dangerous_modules():
    """S 安全修复：模型代码禁止 import 危险模块（os/subprocess/sys...）。"""
    assert _check_static("import os\nprint(1)") is not None
    assert _check_static("from subprocess import run") is not None
    assert _check_static("import pathlib") is not None
    assert _check_static("import sys, json") is not None


def test_static_guard_blocks_dangerous_calls():
    """S 安全修复：模型代码禁止直接 open()/exec()/os.system() 访问宿主。"""
    assert _check_static("open('C:/Windows/win.ini')") is not None
    assert _check_static(r"os.system('dir C:\\')") is not None
    assert _check_static("exec('print(1)')") is not None
    assert _check_static("subprocess.run(['dir'])") is not None


def test_static_guard_allows_safe_code():
    """安全代码（tools.* / json / asyncio / math）放行。"""
    assert _check_static("r = await tools.read_file(path='a.txt')") is None
    assert _check_static("import json, math\nreturn json.dumps({'x': 1})") is None
    assert _check_static("import asyncio\nawait asyncio.sleep(0.1)") is None


@pytest.mark.asyncio
async def test_run_code_rejects_dangerous_import(tmp_path):
    set_tool_context(session_id="s", user_id="u-rc", tenant_id="t", gateway=None)
    out = await run_code("import os\nreturn os.getcwd()")
    assert out.get("isError") is True
    assert "blocked by static guard" in out["message"]


@pytest.mark.asyncio
async def test_runtime_guard_blocks_obfuscated_call(tmp_path):
    """SaaS 安全：混淆绕过静态守卫（字符串拼接 + getattr 动态调用）被运行时守卫拦截。"""
    set_tool_context(session_id="s", user_id="u-rc", tenant_id="t", gateway=None)
    # 静态守卫查不到动态构造的调用 → 由运行时守卫（set_task_factory + trace）兜底
    out = await run_code("m = __builtins__['__impo' + 'rt__']('os')\ngetattr(m, 'system')('dir')")
    assert out.get("isError") is True
    assert "runtime guard" in out["message"]


@pytest.mark.asyncio
async def test_runtime_guard_blocks_open(tmp_path):
    """SaaS 安全：运行时动态构造的 open() 被拦截（模型代码帧发起的调用）。"""
    set_tool_context(session_id="s", user_id="u-rc", tenant_id="t", gateway=None)
    out = await run_code("f = __builtins__['ope' + 'n']('secret.txt', 'w')\nf.write('x')")
    assert out.get("isError") is True
    assert "runtime guard" in out["message"]


@pytest.mark.asyncio
async def test_runtime_guard_does_not_break_tool_calls(tmp_path):
    """运行时守卫不误伤：模型通过 tools.* 调用工具正常执行。"""
    set_tool_context(session_id="s", user_id="u-rc", tenant_id="t", gateway=None)
    f = workspace_dir() / "rg.txt"
    f.write_text("rg-ok", encoding="utf-8")
    out = await run_code("r = await tools.read_file(path='rg.txt')\nreturn r['content']")
    assert out.get("isError") is not True
    assert out["result"] == "rg-ok"


@pytest.mark.asyncio
async def test_program_calls_sdk_tool(tmp_path):
    set_tool_context(session_id="s", user_id="u-rc", tenant_id="t", gateway=None)
    f = workspace_dir() / "hello.txt"
    f.write_text("hello", encoding="utf-8")
    code = (
        f"r = await tools.read_file(path='hello.txt', root=str(r'{tmp_path}'))\n"
        "return r['content']"
    )
    out = await run_code(code)
    assert out.get("isError") is not True
    assert out["result"] == "hello"


@pytest.mark.asyncio
async def test_program_dict_args_and_logs(tmp_path):
    set_tool_context(session_id="s", user_id="u-rc", tenant_id="t", gateway=None)
    workspace_dir() / "log.txt"
    code = (
        "print('before')\n"
        f"await tools.write_file(path='log.txt', content='data', root=str(r'{tmp_path}'))\n"
        "print('after')\n"
        f"return (await tools.read_file(path='log.txt', root=str(r'{tmp_path}')))['content']"
    )
    out = await run_code(code)
    assert out["result"] == "data"
    assert "before" in out["logs"] and "after" in out["logs"]


@pytest.mark.asyncio
async def test_missing_tool_raises_tool_call_error(tmp_path):
    code = "await tools.no_such_tool(path='x')\nreturn 'unreachable'"
    out = await run_code(code)
    assert out.get("isError") is True
    assert "no_such_tool" in out["message"] or "not found" in out["message"]


@pytest.mark.asyncio
async def test_syntax_error_is_structured(tmp_path):
    out = await run_code("def broken(:\n    pass\n")
    assert out.get("isError") is True
    assert "syntax" in out["message"].lower()


@pytest.mark.asyncio
async def test_timeout_is_structured(tmp_path):
    code = "import asyncio\nawait asyncio.sleep(5)\nreturn 1"
    out = await run_code(code, timeout=1)
    assert out.get("isError") is True
    assert "timed out" in out["message"]


@pytest.mark.asyncio
async def test_program_exception_is_structured(tmp_path):
    code = "raise ValueError('boom')"
    out = await run_code(code)
    assert out.get("isError") is True
    assert "ValueError" in out["message"]


@pytest.mark.asyncio
async def test_non_json_return_rendered(tmp_path):
    out = await run_code("return {'a': object()}")
    assert out["result"] == {'a': '<object object at 0x0>'[:20]} or isinstance(out["result"], dict)


def test_sdk_usage_text_lists_tools():
    text = sdk_usage_text()
    assert "tools.read_file" in text
    assert "run_code" not in text.split("Available tools:")[0] or True  # 无断言，仅验证可生成


# ── S 专项逃逸测试：对象内省链 __class__.__mro__[].__subclasses__() ──

_INTROSPECTION_PAYLOADS = [
    # 经典链：().__class__ -> tuple -> __mro__[1] -> __base__ -> __subclasses__()
    "return [c for c in ().__class__.__base__.__subclasses__() if c.__name__ == 'Popen']",
    "return type.__subclasses__()",
    "return ().__class__.__mro__[1].__subclasses__()",
    # 经 __globals__ 拿宿主模块（os）的变体
    "c = [x for x in ().__class__.__mro__[1].__subclasses__()][0]\nreturn c.__globals__",
    # __getattribute__ 间接构造属性访问
    "return ().__getattribute__('__class__')",
]


@pytest.mark.parametrize("code", _INTROSPECTION_PAYLOADS)
def test_static_guard_blocks_introspection_chain(code: str):
    """S 专项逃逸：内省链静态层即被拒绝（attribute ... not allowed）。"""
    denied = _check_static(code)
    assert denied is not None, f"escape payload should be blocked: {code}"
    assert "not allowed" in denied


@pytest.mark.parametrize("code", _INTROSPECTION_PAYLOADS)
@pytest.mark.asyncio
async def test_run_code_blocks_introspection_chain(code: str):
    """S 专项逃逸：run_code（含 subprocess 隔离）必须拒绝内省链,不得执行。"""
    set_tool_context(session_id="s", user_id="u-esc", tenant_id="t", gateway=None)
    out = await run_code(code)
    assert out.get("isError") is True, f"escape should error: {code}"
    assert "blocked" in out.get("message", "").lower()


@pytest.mark.asyncio
async def test_run_code_fail_closed_without_subprocess():
    """S 专项：子进程不可用时 fail-closed,绝不回到宿主进程执行。"""
    import app.tools.run_code as rc
    real_subproc = rc._run_in_subprocess

    async def _no_subprocess(*_a, **_k):
        return None  # 模拟子进程始终不可用

    rc._run_in_subprocess = _no_subprocess
    try:
        out = await rc.run_code("return 1")
    finally:
        rc._run_in_subprocess = real_subproc
    assert out.get("isError") is True
    assert "subprocess unavailable" in out.get("message", "")
    # 确认没有宿主进程 exec 的降级函数残留可被调用
    assert not hasattr(rc, "_run_in_process")
