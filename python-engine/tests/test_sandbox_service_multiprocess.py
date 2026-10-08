"""跨进程验证 B 层沙箱服务：**真的把服务当独立进程起起来**，再从引擎侧走一遍执行。

为什么需要它（与 `test_sandbox_service.py` 的分工）：
- 那套用 `TestClient` 在**同一进程**里调 handler，验的是契约与基线复用；
- 这一套把服务用 **uvicorn 起成独立进程**，验证的才是"把执行挪出引擎进程"这件事本身：
  ① 引擎侧客户端（`RemoteExecBackend`）能真的把命令交给它；
  ② 服务进程在自己的工作区里执行、**写自己那份审计**（共享卷 + 审计落盘）；
  ③ 身份跨进程仍然到得了（审计里能答出"谁执行的"）。

无需 Docker：独立进程 + 环境变量即可（部署形态──容器、共享卷、只内网可达──仍待真实验证）。
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parents[2] / "sandbox-service"
TOKEN = "multiprocess-test-token"
HEALTH_PATH = "/v1/internal/exec/health"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _launcher(port: int) -> str:
    """在子进程里起 uvicorn（把 sandbox-service 放进 sys.path 后导入 service）。"""
    return (
        "import sys, uvicorn;"
        f"sys.path.insert(0, {str(SERVICE_DIR)!r});"
        "import service;"
        f"uvicorn.run(service.app, host='127.0.0.1', port={port}, log_level='warning')"
    )


async def _wait_healthy(url: str, timeout: float = 20.0) -> None:
    import httpx

    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=1.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{url}{HEALTH_PATH}", headers={"X-Internal-Token": TOKEN})
                if resp.status_code == 200:
                    return
            except Exception:  # noqa: BLE001 - 还没起来，继续等
                pass
            await asyncio.sleep(0.2)
    raise AssertionError(f"沙箱服务在 {timeout}s 内没有就绪：{url}")


@pytest.fixture(autouse=True)
def _restore_tool_context():
    """本文件的用例会设置工具身份（租户/用户/会话）；跑完必须**还原**。

    否则身份会留在同进程的上下文里，污染后续用例 —— 实测：残留 `tenant_id=t1` 会让
    `test_skills_run` 找不到按默认租户保存的技能（"Skill not found"）。仓库自带
    `get_all()` / `restore_context()` 就是为这种场景准备的。
    """
    from app.tools.context import get_all, restore_context

    snapshot = get_all()
    yield
    restore_context(snapshot)


@pytest.fixture
def sandbox_service(tmp_path, monkeypatch):
    """起一个**独立进程**的服务，工作区与审计目录都指向 tmp_path（两个进程共享）。"""
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    sandbox_root = tmp_path / "sandbox"
    logs = tmp_path / "logs"
    sandbox_root.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "INTERNAL_TOKEN": TOKEN,
        "SANDBOX_ROOT": str(sandbox_root),
        # 服务与引擎**共享同一个工作区卷**（CompositeBackend 的契约要求）
        "CHIRON_ALLOW_LOCAL_SANDBOX": "true",
    }
    proc = subprocess.Popen(
        [sys.executable, "-c", _launcher(port)],
        env=env,
        # cwd 决定服务进程把 `logs/exec_audit.jsonl` 写到哪里（审计目录是 cwd 相对的，
        # 与引擎同款约定）——指向共享的 tmp_path，这样本用例能读到**服务进程**写的那份。
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        asyncio.run(_wait_healthy(url))
    except Exception:
        proc.kill()
        out = ""
        if proc.stdout is not None:
            out = proc.stdout.read()[-2000:]
        raise AssertionError(f"服务进程未能就绪，输出：\n{out}") from None
    yield url, tmp_path
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_engine_client_executes_in_a_separate_service_process(sandbox_service, monkeypatch):
    """引擎侧客户端 → 独立服务进程 → 真的执行了，且**服务进程**写下了自己的审计。"""
    from app.backends.local import LocalWorkspaceBackend
    from app.backends.remote_exec import RemoteExecBackend
    from app.tools.context import set_tool_context

    url, tmp_path = sandbox_service
    set_tool_context(tenant_id="t1", user_id="u1", session_id="s1")

    backend = RemoteExecBackend(
        LocalWorkspaceBackend(),
        url=url,
        token=TOKEN,
        tenants=["t1"],  # 白名单租户才走服务
    )
    assert backend.should_use_service() is True

    result = asyncio.run(backend.execute("echo hello-from-service"))

    assert result.exit_code == 0, f"服务返回失败：{result.output}"
    assert "hello-from-service" in result.output

    # 服务进程写了审计（在共享卷里），并且**身份跨进程到达**了
    audit_file = tmp_path / "logs" / "exec_audit.jsonl"
    assert audit_file.exists(), "服务侧没有落审计 —— 隔离不能以'看不见'为代价"
    entries = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    last = entries[-1]
    assert last["tool"] == "shell_exec"
    assert (last["tenant"], last["user"], last["session"]) == ("t1", "u1", "s1")


def test_non_whitelisted_tenant_stays_in_engine_process(sandbox_service, monkeypatch):
    """白名单外的租户**不得**被送到服务 —— 它必须留在引擎进程里执行（安全默认）。"""
    from app.backends.local import LocalWorkspaceBackend
    from app.backends.remote_exec import RemoteExecBackend
    from app.tools.context import set_tool_context

    url, tmp_path = sandbox_service
    set_tool_context(tenant_id="t2", user_id="u2", session_id="s2")

    backend = RemoteExecBackend(LocalWorkspaceBackend(), url=url, token=TOKEN, tenants=["t1"])
    assert backend.should_use_service() is False

    result = asyncio.run(backend.execute("echo hello-from-engine"))
    assert result.exit_code == 0
    assert "hello-from-engine" in result.output

    audit_file = tmp_path / "logs" / "exec_audit.jsonl"
    if audit_file.exists():
        entries = [
            json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        assert all(e.get("tenant") != "t2" for e in entries), "白名单外租户不应出现在服务侧审计里"
