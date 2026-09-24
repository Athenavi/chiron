"""插件 subprocess 沙箱测试 — run_plugin_in_sandbox 隔离与 fail-loud。

覆盖：
- 正常执行（独立子进程，非进程内 exec）
- 静态守卫（import os 拦截）/ 运行时守卫（open() 拦截）
- 超时 kill / 输出超限 / 缺 main 导出 / 插件异常结构化返回
- 审计日志 JSONL 落盘
"""
from __future__ import annotations

import json

import pytest

from app.api.plugins import SANDBOX_CONFIG, run_plugin_in_sandbox


@pytest.fixture(autouse=True)
def _restore_config():
    """保存沙箱配置，测试内修改后恢复。"""
    snapshot = {
        "timeout_seconds": SANDBOX_CONFIG.timeout_seconds,
        "max_output_bytes": SANDBOX_CONFIG.max_output_bytes,
        "audit_log_enabled": SANDBOX_CONFIG.audit_log_enabled,
    }
    yield
    for k, v in snapshot.items():
        setattr(SANDBOX_CONFIG, k, v)


GOOD_PLUGIN = """
def main(input):
    return {"echo": input.get("msg", ""), "pid_isolated": True}
"""


@pytest.mark.asyncio
async def test_sandbox_runs_plugin_in_subprocess():
    result = await run_plugin_in_sandbox("p_ok", "u1", GOOD_PLUGIN, {"msg": "hi"})
    assert result["success"] is True
    assert result["output"]["echo"] == "hi"


@pytest.mark.asyncio
async def test_sandbox_blocks_os_import():
    code = "import os\n\ndef main(input):\n    return os.getpid()\n"
    result = await run_plugin_in_sandbox("p_os", "u1", code, {})
    assert result["success"] is False
    assert "static guard" in result["error"] and "os" in result["error"]


@pytest.mark.asyncio
async def test_sandbox_blocks_open_runtime_guard():
    code = """
def main(input):
    f = open("C:/Windows/win.ini")
    return f.read()
"""
    result = await run_plugin_in_sandbox("p_open", "u1", code, {})
    assert result["success"] is False
    # 直接 open() 调用被静态守卫拦截（混淆调用则由运行时守卫兜底，见下一用例）
    assert "static guard" in result["error"] and "open" in result["error"]


@pytest.mark.asyncio
async def test_sandbox_blocks_obfuscated_open():
    # 运行时守卫兜底：动态构造 open 调用（静态层无法识别）
    code = """
def main(input):
    b = __builtins__ if isinstance(__builtins__, dict) else __builtins__.__dict__
    fn = b['ope' + 'n']
    return fn("/etc/passwd").read()
"""
    result = await run_plugin_in_sandbox("p_obf", "u1", code, {})
    assert result["success"] is False
    # 关键是被拦住，而不是被**哪一层**拦住：`__builtins__.__dict__` 这类对象内省
    # 现在会被静态守卫更早识别（"attribute '__dict__' is not allowed"），
    # 运行时守卫仍作为纵深防御存在（见 app/plugin_sandbox 与 app/tools/code_guard）。
    err = result["error"]
    assert "block" in err.lower() and ("static guard" in err or "runtime guard" in err)


@pytest.mark.asyncio
async def test_sandbox_timeout_kills_plugin():
    SANDBOX_CONFIG.timeout_seconds = 2
    code = "def main(input):\n    while True:\n        pass\n"
    result = await run_plugin_in_sandbox("p_loop", "u1", code, {})
    assert result["success"] is False
    assert "timed out" in result["error"]


@pytest.mark.asyncio
async def test_sandbox_output_size_limit():
    SANDBOX_CONFIG.max_output_bytes = 64
    code = "def main(input):\n    return 'x' * 10000\n"
    result = await run_plugin_in_sandbox("p_big", "u1", code, {})
    assert result["success"] is False
    assert "exceeds limit" in result["error"]


@pytest.mark.asyncio
async def test_sandbox_missing_main_export():
    result = await run_plugin_in_sandbox("p_nomain", "u1", "x = 1\n", {})
    assert result["success"] is False
    assert "main" in result["error"]


@pytest.mark.asyncio
async def test_sandbox_plugin_exception_structured():
    code = "def main(input):\n    raise ValueError('boom')\n"
    result = await run_plugin_in_sandbox("p_raise", "u1", code, {})
    assert result["success"] is False
    assert "ValueError" in result["error"] and "boom" in result["error"]


@pytest.mark.asyncio
async def test_sandbox_audit_log_written(tmp_path, monkeypatch):
    SANDBOX_CONFIG.audit_log_enabled = True
    monkeypatch.chdir(tmp_path)

    await run_plugin_in_sandbox("p_audit", "u9", GOOD_PLUGIN, {"msg": "x"})
    log_file = tmp_path / "logs" / "plugin_audit.jsonl"
    assert log_file.exists()
    entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert entries, "audit log is empty"
    assert entries[-1]["plugin"] == "p_audit"
    assert entries[-1]["user"] == "u9"
    assert entries[-1]["success"] is True
    assert "duration_ms" in entries[-1]
