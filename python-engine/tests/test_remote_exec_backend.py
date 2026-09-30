"""S5-(e)：远程执行后端的分流与失败语义。

对应 `vendor/设计-独立sandbox服务.md` §7 的验收口径。四条硬要求：

1. **`local`（默认）行为逐字不变** —— 引入这个机制不该让任何既有部署多发一个请求；
2. **白名单外的租户走本地** —— 不是"没配就全走服务"；
3. **服务不可用时失败、不回退** —— 静默回退会让"隔离已生效"失真；
4. **两侧白名单同源** —— 断言**同一份实现**，而不是"看起来一样"。
"""

from __future__ import annotations

import pathlib
from typing import Any

import httpx
import pytest

from app.backends.protocol import ExecuteResult
from app.backends.remote_exec import RemoteExecBackend
from app.tools.context import set_tool_context

TENANT = "tenant-a"


class _Inner:
    """内层后端替身：记录 execute 是否被调用，并支持文件方法委托断言。"""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.read_calls: list[str] = []

    async def execute(self, command: str, *, timeout: float | None = None) -> ExecuteResult:
        self.executed.append(command)
        return ExecuteResult(output="local-ran", exit_code=0)

    async def read(self, path: str, *, offset: int = 0, limit: int = 200) -> Any:
        self.read_calls.append(path)
        return "inner-read-result"

    def supports_execution(self) -> bool:
        return True


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """最小 httpx.AsyncClient 替身：记录请求，返回预设响应（或抛异常）。"""

    calls: list[dict[str, Any]] = []
    response: _FakeResponse | None = None
    raises: Exception | None = None

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def post(self, url: str, *, json: Any = None, headers: Any = None) -> _FakeResponse:
        type(self).calls.append({"url": url, "json": json, "headers": headers})
        if type(self).raises is not None:
            raise type(self).raises
        assert type(self).response is not None
        return type(self).response


@pytest.fixture(autouse=True)
def _reset_fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.calls = []
    _FakeClient.response = _FakeResponse(
        {"stdout": "service-ran\n", "stderr": "", "exit_code": 0, "truncated": False}
    )
    _FakeClient.raises = None
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    set_tool_context(tenant_id=TENANT)


# ── 1. local（默认）逐字不变 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_url_never_calls_service():
    """url 为空 ⇒ 即使 backend=service 也不分流（配置不完整时按本地走，且不发请求）。"""
    inner = _Inner()
    backend = RemoteExecBackend(inner, url="", token="t", tenants=[TENANT])

    result = await backend.execute("ls")

    assert result.output == "local-ran"
    assert _FakeClient.calls == []
    assert inner.executed == ["ls"]


@pytest.mark.asyncio
async def test_local_default_is_identity_for_files_too():
    inner = _Inner()
    backend = RemoteExecBackend(inner, url="", tenants=[])

    assert await backend.read("a.txt") == "inner-read-result"
    assert inner.read_calls == ["a.txt"]


# ── 2/3. 白名单决定走哪边 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_whitelisted_tenant_routes_to_service():
    inner = _Inner()
    backend = RemoteExecBackend(inner, url="http://sandbox:9000", token="tk", tenants=[TENANT])

    result = await backend.execute("ls -la")

    assert [c["url"] for c in _FakeClient.calls] == ["http://sandbox:9000/v1/internal/exec/run"]
    assert _FakeClient.calls[0]["headers"]["X-Internal-Token"] == "tk"
    assert _FakeClient.calls[0]["json"] == {"command": "ls -la"}
    assert result.output == "service-ran\n"
    assert result.exit_code == 0
    # 关键：**没有**在本地执行
    assert inner.executed == []


@pytest.mark.asyncio
async def test_tenant_outside_whitelist_stays_local():
    inner = _Inner()
    backend = RemoteExecBackend(inner, url="http://sandbox:9000", tenants=["someone-else"])

    result = await backend.execute("ls")

    assert result.output == "local-ran"
    assert _FakeClient.calls == []


@pytest.mark.asyncio
async def test_empty_whitelist_routes_nobody():
    """空名单 = 谁都不走服务。这是刻意的安全默认（而不是"没配就全走服务"）。"""
    inner = _Inner()
    backend = RemoteExecBackend(inner, url="http://sandbox:9000", tenants=[])

    assert backend.should_use_service(tenant=TENANT) is False
    await backend.execute("ls")
    assert _FakeClient.calls == []
    assert inner.executed == ["ls"]


def test_whitelist_entries_are_trimmed_and_blanks_ignored():
    backend = RemoteExecBackend(_Inner(), url="http://x", tenants=[" a ", "", "  "])

    assert backend.should_use_service(tenant="a") is True
    assert backend.should_use_service(tenant="") is False


# ── 3. 服务不可用 ⇒ 失败，不回退（核心）────────────────────────────────


@pytest.mark.asyncio
async def test_connection_error_fails_without_falling_back():
    """**最关键的一条**：服务挂了要让执行可见地失败，绝不能悄悄跑在引擎进程里。"""
    inner = _Inner()
    backend = RemoteExecBackend(inner, url="http://sandbox:9000", tenants=[TENANT])
    _FakeClient.raises = httpx.ConnectError("connection refused")

    result = await backend.execute("ls")

    assert result.error and "unavailable" in result.error
    assert "ConnectError" in result.error
    # 告诉读到它的人该怎么办，否则只会看到"命令跑不了"
    assert "SANDBOX_BACKEND=local" in result.error
    # 绝不回退
    assert inner.executed == []


@pytest.mark.asyncio
async def test_http_error_fails_without_falling_back():
    inner = _Inner()
    backend = RemoteExecBackend(inner, url="http://sandbox:9000", tenants=[TENANT])
    _FakeClient.response = _FakeResponse({"error": "nope"}, status_code=503)

    result = await backend.execute("ls")

    assert result.error and "503" in result.error
    assert inner.executed == []


@pytest.mark.asyncio
async def test_malformed_response_fails_without_falling_back():
    inner = _Inner()
    backend = RemoteExecBackend(inner, url="http://sandbox:9000", tenants=[TENANT])

    class _BadResponse(_FakeResponse):
        def json(self) -> Any:
            raise ValueError("not json")

    _FakeClient.response = _BadResponse({})

    result = await backend.execute("ls")

    assert result.error and "invalid response" in result.error
    assert inner.executed == []


# ── 4. 两侧同源（机械检查）─────────────────────────────────────────────


def test_this_backend_carries_no_second_copy_of_the_baseline():
    """设计稿 §4 的核心取舍：宁可共享一份实现，也不要第二份"看起来一样"的白名单。

    因此本模块**不得**自带白名单 / RLIMIT —— 它只负责转发；基线仍由服务侧 import
    `app/tools/sandbox.py` 的同一份实现。
    """
    import ast

    from app.backends import remote_exec as remote_mod

    tree = ast.parse(pathlib.Path(remote_mod.__file__).read_text(encoding="utf-8"))

    # 只在**代码**里找标识符，不在散文（docstring / 注释）里找 —— 否则本模块文档里那句
    # "白名单 / RLIMIT / AST 守卫"会把自己判失败。机械检查必须精确到它真正想拦的东西。
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            identifiers.update(a.name for a in node.names)

    for forbidden in (
        "_ALLOWED_EXECUTABLES",
        "_TEXT_ALLOWED",
        "apply_resource_limits",
        "setrlimit",
        "_parse_command",
    ):
        assert forbidden not in identifiers, f"远程后端不得自带 {forbidden}（会与本地基线漂移）"
