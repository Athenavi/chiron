"""端口韧性测试：端口冲突/启动失败时的重试与最终错误。

覆盖三处修改：

1. ``app.storage.pg`` —— embedded-pack / binaries 两个后端起步"像端口冲突"时换端口
   重试；``get_uri()`` 回来的真实端口要能被读出来（``PostgresServer.port``）；
   非端口冲突的错误不重试；全部失败时 ``PostgresUnavailable.attempts`` 带每条真实原因。
2. ``app.components.manager`` —— 组件起步就退出（日志写端口冲突）/健康检查超时都换
   端口重试一次；两次都不行时给一条带每次原因的可读错误。
3. 两处共用的 ``is_port_conflict`` / ``free_port(exclude=...)`` 的纯逻辑。

不碰真实数据库、不 spawn 真进程：PG 的 ``_prepare`` / 子进程的 ``Popen`` 全部替换掉。
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
from app.components import manager as manager_module
from app.components.manager import (
    ComponentManager,
    ComponentSpec,
    StartFailedError,
    component_base_url,
    free_port,
    is_port_conflict,
)
from app.config import Settings
from app.storage import pg as pg_module
from app.storage.pg import (
    PORT_ATTEMPTS,
    PostgresServer,
    PostgresUnavailable,
    find_free_port,
)


# --------------------------------------------------------------------- 纯逻辑
def test_is_port_conflict_matches_common_kernel_messages() -> None:
    assert is_port_conflict("OSError: [Errno 98] Address already in use")
    assert is_port_conflict('LOG:  could not bind IPv4 address "127.0.0.1": Address already in use')
    assert is_port_conflict("EADDRINUSE: address already in use 127.0.0.1:8000")
    assert is_port_conflict("another server might be running; trying to start server anyway")
    assert pg_module.is_port_conflict("listen tcp 127.0.0.1:8000: bind: address already in use")


def test_is_port_conflict_rejects_unrelated_errors() -> None:
    assert not is_port_conflict("ModuleNotFoundError: No module named 'app'")
    assert not is_port_conflict("Permission denied")
    assert not is_port_conflict("")
    assert not is_port_conflict(None)


def test_find_free_port_skips_excluded_ports() -> None:
    first = find_free_port()
    second = find_free_port(exclude=[first])
    assert second != first
    assert 1024 < second <= 65535


def test_component_free_port_skips_excluded_ports() -> None:
    first = free_port()
    second = free_port(exclude={first})
    assert second != first


def test_component_base_url_is_loopback_only() -> None:
    assert component_base_url(4321) == "http://127.0.0.1:4321"


# --------------------------------------------------------------- PG 后端重试
class _FakeHandle:
    """冒充 embedded_postgres 返回的服务器句柄（get_uri / cleanup 是对外契约）。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.cleaned = 0

    def get_uri(self, database: str | None = None) -> str:
        return f"postgresql://postgres:@127.0.0.1:{self.port}/{database or 'chiron'}"

    def cleanup(self) -> None:
        self.cleaned += 1


def _embedded_module(behaviour) -> types.SimpleNamespace:
    """造一个只有 ``get_server(pgdata, *, cleanup_mode)`` 的假 embedded_postgres。"""

    def get_server(pgdata, *, cleanup_mode="stop"):
        return behaviour(pgdata, cleanup_mode)

    return types.SimpleNamespace(get_server=get_server)


def _server(tmp_path: Path) -> PostgresServer:
    return PostgresServer(Settings(data_dir=tmp_path / "data", pg_enabled=False))


def _disable_prepare(monkeypatch: pytest.MonkeyPatch) -> None:
    """不连真库：只验证"起没起来、用了哪个端口"。"""

    monkeypatch.setattr(
        PostgresServer,
        "_prepare",
        lambda self, dsn, strict, endpoint=None: None,
    )


def _forbid_binaries(monkeypatch: pytest.MonkeyPatch, message: str = "测试里禁用 binaries 后端") -> None:
    def boom(self) -> str:
        raise RuntimeError(message)

    monkeypatch.setattr(PostgresServer, "_start_binaries", boom)


def _forbid_embedded(monkeypatch: pytest.MonkeyPatch, message: str = "测试里不启用 embedded-pack 后端") -> None:
    """报错文本要**不带**端口冲突特征，否则会触发换端口重试。"""

    def boom(self) -> str:
        raise RuntimeError(message)

    monkeypatch.setattr(PostgresServer, "_start_embedded_pack", boom)


def test_embedded_pack_retries_port_conflict_then_uses_real_uri_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """前两次"地址被占用"，第三次拿到 55999：DSN 与 port 都必须来自 get_uri()。"""
    attempts = {"count": 0}
    real_port = 55999

    def behaviour(pgdata, cleanup_mode):
        attempts["count"] += 1
        if attempts["count"] < PORT_ATTEMPTS:
            raise OSError("could not bind IPv4 socket: Address already in use")
        return _FakeHandle(real_port)

    monkeypatch.setitem(sys.modules, "embedded_postgres", _embedded_module(behaviour))
    _disable_prepare(monkeypatch)
    _forbid_binaries(monkeypatch)

    server = _server(tmp_path)
    dsn = server.start()

    assert attempts["count"] == PORT_ATTEMPTS
    assert server.backend == "embedded-pack"
    assert dsn == f"postgresql://postgres:@127.0.0.1:{real_port}/chiron"
    # 端口以 get_uri() 为准，而不是 settings.pg_port（默认 0）
    assert server.port == real_port
    assert any("端口冲突" in note for note in server.notes)
    assert server.attempts == [], "成功了就不该留下失败记录"


def test_embedded_pack_non_conflict_error_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}

    def behaviour(pgdata, cleanup_mode):
        attempts["count"] += 1
        raise PermissionError("invalid binary")

    monkeypatch.setitem(sys.modules, "embedded_postgres", _embedded_module(behaviour))
    _disable_prepare(monkeypatch)
    _forbid_binaries(monkeypatch)

    with pytest.raises(PostgresUnavailable) as info:
        _server(tmp_path).start()

    assert attempts["count"] == 1, "非端口冲突不该重试"
    assert len(info.value.attempts) == 2, "embedded-pack 与 binaries 各一条"
    assert "invalid binary" in info.value.attempts[0]


def test_binaries_backend_switches_port_after_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """第一次 pg_ctl start 报地址被占：收掉进程、换端口、第二次成功。"""
    server = _server(tmp_path)
    monkeypatch.setattr(PostgresServer, "_find_binary", lambda self, name: f"/fake/{name}")
    _disable_prepare(monkeypatch)
    _forbid_embedded(monkeypatch)

    # initdb 已经跑过：只测 pg_ctl start 这条路径
    server.data_dir.mkdir(parents=True, exist_ok=True)
    (server.data_dir / "PG_VERSION").write_text("18\n", encoding="utf-8")

    ports = [56101, 56102]

    def fake_free_port(host: str = "127.0.0.1", *, exclude=()) -> int:
        blocked = set(exclude)
        return next(port for port in ports if port not in blocked)

    monkeypatch.setattr(pg_module, "find_free_port", fake_free_port)

    calls: list[tuple[str, list[str]]] = []

    def fake_run(self, cmd, timeout, what, check=True, capture=True):
        calls.append((what, list(cmd)))
        if what == "pg_ctl start" and len([c for c in calls if c[0] == "pg_ctl start"]) == 1:
            raise RuntimeError(
                'pg_ctl start 退出码 1: LOG: could not bind IPv4 address "127.0.0.1": '
                "Address already in use"
            )
        return ""

    monkeypatch.setattr(PostgresServer, "_run", fake_run)

    dsn = server.start()

    assert server.backend == "binaries"
    assert server.port == 56102
    assert ":56102/" in dsn
    starts = [c for c in calls if c[0] == "pg_ctl start"]
    assert len(starts) == 2, "端口冲突后应换端口再起一次"
    assert any("56102" in part for part in starts[1][1])
    # 失败的那次必须先被 pg_ctl stop 收掉，否则它会一直占着 56101
    assert any(c[0] == "pg_ctl stop" for c in calls)
    assert 56101 in server._failed_ports


def test_binaries_backend_all_attempts_conflict_raises_with_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_prepare(monkeypatch)

    def embedded_boom(self) -> str:
        raise RuntimeError("could not bind: Address already in use")

    def binaries_boom(self) -> str:
        raise RuntimeError("pg_ctl start 退出码 1: Address already in use")

    monkeypatch.setattr(PostgresServer, "_start_embedded_pack", embedded_boom)
    monkeypatch.setattr(PostgresServer, "_start_binaries", binaries_boom)

    with pytest.raises(PostgresUnavailable) as info:
        _server(tmp_path).start()

    attempts = info.value.attempts
    assert len(attempts) == PORT_ATTEMPTS * 2, "两个后端各自跑满 PORT_ATTEMPTS 次"
    assert attempts[0].startswith("embedded-pack: RuntimeError:")
    assert f"第 1/{PORT_ATTEMPTS} 次尝试" in attempts[0]
    assert f"第 {PORT_ATTEMPTS}/{PORT_ATTEMPTS} 次尝试" in attempts[PORT_ATTEMPTS - 1]
    assert attempts[-1].startswith("binaries: RuntimeError:")
    assert "Address already in use" in attempts[-1]


# ------------------------------------------------------------ 组件启动重试
class _FakeChild:
    """假子进程：poll 用 returncode 表示死活，terminate/kill 记账。"""

    def __init__(self, pid: int, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.terminated = 0
        self.killed = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated += 1
        self.returncode = 0

    def kill(self) -> None:
        self.killed += 1
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode


def _manager_with_component(tmp_path: Path, *, health: str = "/healthz") -> ComponentManager:
    """装好一个组件（写 installed.json + entry），不依赖组件源与安装流程。"""
    manager = ComponentManager(
        data_dir=tmp_path / "data",
        specs=[ComponentSpec(name="pageindex", title="PageIndex", description="测试组件")],
    )
    version_dir = manager.component_dir("pageindex") / "1.0.0"
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / "app.py").write_text("print('fake component')\n", encoding="utf-8")
    (version_dir / "installed.json").write_text(
        json.dumps(
            {
                "name": "pageindex",
                "version": "1.0.0",
                "entry": "app.py",
                "args": [],
                "health": health,
                "files": [],
                "source": "",
                "sourceKind": "dir",
                "installedAt": "",
            }
        ),
        encoding="utf-8",
    )
    return manager


def test_component_retries_on_port_conflict_in_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """第一次起步就退出且日志写"地址被占用" → 换端口重试，第二次成功。"""
    manager = _manager_with_component(tmp_path)
    ports: list[int] = []

    def fake_popen(argv, **kwargs):
        port = int(kwargs["env"]["CHIRON_COMPONENT_PORT"])
        ports.append(port)
        handle = kwargs["stdout"]
        if len(ports) == 1:
            handle.write(b"Traceback (most recent call last):\nOSError: [Errno 98] Address already in use\n")
            handle.flush()
            return _FakeChild(pid=1001, returncode=1)  # 立刻退出
        handle.write(b"serving on %d\n" % port)
        handle.flush()
        return _FakeChild(pid=1002, returncode=None)

    monkeypatch.setattr(manager_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(manager_module, "_http_ok", lambda url, timeout: True)

    started = manager.start("pageindex")
    try:
        assert len(ports) == 2, "端口冲突应触发一次换端口重试"
        assert ports[0] != ports[1]
        assert started["pid"] == 1002
        assert started["port"] == ports[1]
        assert started["baseUrl"] == component_base_url(ports[1])
        assert started["attempt"] == 2
        assert started["health"]["ok"] is True
        # 日志里标出了重试边界，排查时能看出第二次是换端口重来的
        log_text = (manager.log_dir / "pageindex.log").read_text("utf-8")
        assert "第 2/2 次启动" in log_text
    finally:
        manager.stop_all()


def test_component_retries_once_when_health_check_never_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """健康检查没过也换端口重来一次（组件可能卡在 bind 上），第一次的进程必须被收掉。"""
    manager = _manager_with_component(tmp_path)
    children: list[_FakeChild] = []
    ports: list[int] = []

    def fake_popen(argv, **kwargs):
        ports.append(int(kwargs["env"]["CHIRON_COMPONENT_PORT"]))
        child = _FakeChild(pid=2000 + len(children), returncode=None)
        children.append(child)
        return child

    waits = {"count": 0}

    def fake_wait(record, path, timeout):
        waits["count"] += 1
        if waits["count"] == 1:
            raise StartFailedError("组件健康检查超时", retryable=True)

    monkeypatch.setattr(manager_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(manager_module, "_wait_health", fake_wait)

    started = manager.start("pageindex", health_timeout=0.05)
    try:
        assert waits["count"] == 2
        assert len(ports) == 2
        assert ports[0] != ports[1]
        assert children[0].terminated == 1, "失败的那次进程必须先收掉"
        assert started["attempt"] == 2
        assert started["port"] == ports[1]
    finally:
        manager.stop_all()


def test_component_gives_readable_error_after_all_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两次都不行：错误里带每次的端口与原因，而不是一句笼统的"启动失败"。"""
    manager = _manager_with_component(tmp_path)
    ports: list[int] = []

    def fake_popen(argv, **kwargs):
        ports.append(int(kwargs["env"]["CHIRON_COMPONENT_PORT"]))
        handle = kwargs["stdout"]
        handle.write(b"OSError: address already in use\n")
        handle.flush()
        return _FakeChild(pid=3000 + len(ports), returncode=1)

    monkeypatch.setattr(manager_module.subprocess, "Popen", fake_popen)

    with pytest.raises(StartFailedError) as info:
        manager.start("pageindex")

    message = str(info.value)
    assert manager_module.PORT_ATTEMPTS == 2
    assert len(ports) == 2
    assert ports[0] != ports[1]
    assert "已换端口重试 2 次" in message
    assert f"端口 {ports[0]}" in message
    assert f"端口 {ports[1]}" in message
    assert "地址被占用" in message or "address already in use" in message.lower()
    assert manager.is_running("pageindex") is False


def test_component_spawn_failure_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """命令不存在（ENOENT）换端口也救不回来：只 spawn 一次，错误照原样给出。"""
    manager = _manager_with_component(tmp_path)
    spawns: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        spawns.append(list(argv))
        raise FileNotFoundError("no such file or directory: python")

    monkeypatch.setattr(manager_module.subprocess, "Popen", fake_popen)

    with pytest.raises(StartFailedError) as info:
        manager.start("pageindex")

    assert len(spawns) == 1
    assert "启动失败" in str(info.value)
    assert manager.is_running("pageindex") is False
