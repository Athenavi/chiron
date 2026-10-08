"""`sandbox-service/service.py`：独立的执行沙箱服务（S5-(e) 的 B 层）。

它存在的意义是**把「执行」从引擎进程挪出去**（故障/资源/审计隔离），而**基线必须与引擎同源** ——
所以这里同时钉两件事：① 契约（内部令牌 fail-closed、请求/响应形状、被拦下与超时都变成非零退出码）；
② 基线确实来自引擎那一份 `run_in_sandbox`（白名单/逃逸拦截/审计都在，不是服务侧另写一套）。
"""

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.tools import exec_audit

SERVICE_DIR = Path(__file__).resolve().parents[2] / "sandbox-service"


@pytest.fixture
def service(tmp_path, monkeypatch):
    """加载服务应用；工作区与审计落盘都指向 tmp_path（不污染仓库）。"""
    monkeypatch.syspath_prepend(str(SERVICE_DIR))
    monkeypatch.setenv("INTERNAL_TOKEN", "test-internal-token")
    monkeypatch.setenv("SANDBOX_ROOT", str(tmp_path / "sandbox"))
    monkeypatch.setenv("CHIRON_ALLOW_LOCAL_SANDBOX", "true")
    monkeypatch.setattr(exec_audit, "AUDIT_DIR", tmp_path / "logs")
    module = importlib.import_module("service")
    return TestClient(module.app)


TOKEN = {"X-Internal-Token": "test-internal-token"}


def _audit_entries(tmp_path) -> list[dict]:
    path = tmp_path / "logs" / exec_audit.EXEC_AUDIT_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_health_requires_token(service):
    assert service.get("/v1/internal/exec/health").status_code == 401
    resp = service.get("/v1/internal/exec/health", headers=TOKEN)
    assert resp.status_code == 200 and resp.json()["status"] == "ok"


def test_run_requires_token(service):
    assert service.post("/v1/internal/exec/run", json={"command": "echo hi"}).status_code == 401
    assert (
        service.post(
            "/v1/internal/exec/run",
            json={"command": "echo hi"},
            headers={"X-Internal-Token": "wrong"},
        ).status_code
        == 401
    )


def test_run_executes_in_the_engine_baseline(service, tmp_path):
    resp = service.post("/v1/internal/exec/run", json={"command": "echo hi"}, headers=TOKEN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["exit_code"] == 0
    assert "hi" in body["stdout"] + body["stderr"]
    # 基线那一份审计照样生效（不是服务侧另写一套）
    entries = _audit_entries(tmp_path)
    assert entries and entries[-1]["tool"] == "shell_exec"


def test_blocked_command_becomes_nonzero_exit(service, tmp_path):
    """被基线拦下的命令必须给出**非零退出码 + 原因**，而不是空结果。"""
    resp = service.post(
        "/v1/internal/exec/run", json={"command": "cat ../../../etc/passwd"}, headers=TOKEN
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["exit_code"] not in (0, None)
    assert "blocked" in body["stderr"].lower()


def test_identity_is_restored_so_audit_can_name_who(service, tmp_path):
    """带上身份 ⇒ 审计流水里必须能答出"谁执行的"（否则隔离服务的审计等于白记）。"""
    resp = service.post(
        "/v1/internal/exec/run",
        json={"command": "echo hi", "tenant_id": "t1", "user_id": "u1", "session_id": "s1"},
        headers=TOKEN,
    )
    assert resp.status_code == 200
    last = _audit_entries(tmp_path)[-1]
    assert (last["tenant"], last["user"], last["session"]) == ("t1", "u1", "s1")


def test_missing_token_config_fails_closed(tmp_path, monkeypatch):
    """没配令牌就**拒绝服务**（fail-closed）—— 不能变成"没配就是不需要认证"。"""
    monkeypatch.syspath_prepend(str(SERVICE_DIR))
    monkeypatch.delenv("INTERNAL_TOKEN", raising=False)
    monkeypatch.setenv("SANDBOX_ROOT", str(tmp_path / "sandbox"))
    module = importlib.import_module("service")
    client = TestClient(module.app)
    assert client.get("/v1/internal/exec/health", headers=TOKEN).status_code == 503
