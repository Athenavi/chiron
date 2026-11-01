"""``embedded_postgres`` 后端适配的单元测试（全部用假模块，不启动任何真进程）。

覆盖三件事：
1. **API 探测**：``dir()`` / 真实签名 / 打包二进制包装函数要能被区分出来（并真的能在
   装好的包上跑一遍 —— ``importorskip``，不装包就跳过）；
2. **参数适配**：入口只吃 ``pgdata``（位置/关键字）、只吃 ``data_dir=``、只吃 ``**kwargs``
   这三种形态都要能调起来 —— 老代码全部猜 ``data_dir=/port=/user=``，结果 embedded-pack
   永远 TypeError、静默回退到系统二进制（就是 P0 里 backend=binaries 的原因）；
3. **失败回退**：真实异常与探测结果必须原样进 ``PostgresUnavailable.attempts`` / ``probes``，
   不许把回退包装成成功。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from app.config import Settings
from app.storage import pg
from app.storage.pg import (
    EMBEDDED_HANDLE_PIDS_FILE,
    PostgresServer,
    PostgresUnavailable,
    _dsn_from_postmaster,
    _is_pg_ctl_wait_timeout,
    _read_handle_pids,
    _read_postmaster_file,
    _tcp_endpoint,
    database_of,
    probe_embedded_api,
    replace_database,
    resolve_embedded_entry,
)

# ---------------------------------------------------------------- 测试替身


def _fake_module(**attrs: Any) -> types.ModuleType:
    """造一个像 ``embedded_postgres`` 的模块对象。"""
    module = types.ModuleType("embedded_postgres")
    module.__file__ = "<fake embedded_postgres>"  # type: ignore[attr-defined]
    for name, value in attrs.items():
        setattr(module, name, value)
    return module


def _bundled_binary(name: str = "pg_ctl") -> Any:
    """包内二进制包装函数（``pg_ctl`` / ``initdb`` / ``psql``…）：可调用但不是启动入口。"""

    def command(args: list[str], pgdata: Path | None = None, **kwargs: Any) -> str:
        return ""

    command.__name__ = name
    command.__module__ = "embedded_postgres._commands"
    return command


class FakeHandle:
    """像 ``embedded_postgres.PostgresServer`` 那样被使用的句柄。"""

    def __init__(self, uri: str = "postgresql://postgres:@127.0.0.1:5433/postgres") -> None:
        self.uri = uri
        self.asked_for: list[str | None] = []
        self.cleaned = 0

    def get_uri(self, database: str | None = None) -> str:
        self.asked_for.append(database)
        if database is None:
            return self.uri
        # 真实的包也是“把库名换掉”，socket 形态的 DSN 同样要能处理
        return replace_database(self.uri, database)

    def cleanup(self) -> None:
        self.cleaned += 1


class InfoOnlyHandle:
    """没有 ``get_uri()`` 的形态：只能靠 postmaster 信息拼 DSN。"""

    def __init__(self, port: int | None = 6543, socket_dir: Path | None = None) -> None:
        self._info = types.SimpleNamespace(socket_dir=socket_dir, port=port, hostname="127.0.0.1")
        self.cleaned = 0

    def get_postmaster_info(self) -> Any:
        return self._info

    def cleanup(self) -> None:
        self.cleaned += 1


# ---------------------------------------------------------------- 夹具


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", pg_enabled=True, redis_enabled=False)


@pytest.fixture
def no_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """禁用 binaries 后端，让测试只观察 embedded-pack 这一条路。"""

    def boom(self: PostgresServer) -> str:
        raise RuntimeError("测试里禁用 binaries 后端")

    monkeypatch.setattr(PostgresServer, "_start_binaries", boom)


@pytest.fixture
def prepared(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """把 ``_prepare``（真连库）换成记录器：单元测试不碰真 PG。"""
    calls: list[dict[str, Any]] = []

    def fake_prepare(
        self: PostgresServer, dsn: str, strict: bool, endpoint: tuple[str, int] | None = None
    ) -> None:
        calls.append({"dsn": dsn, "strict": strict, "endpoint": endpoint})

    monkeypatch.setattr(PostgresServer, "_prepare", fake_prepare)
    return calls


# ---------------------------------------------------------------- 1. API 探测


def test_probe_records_dir_signatures_and_bundled_binaries() -> None:
    module = _fake_module(
        get_server=lambda pgdata, *, cleanup_mode="stop": None,
        pg_ctl=_bundled_binary(),
        initdb=_bundled_binary("initdb"),
        utils=types.ModuleType("embedded_postgres.utils"),
    )
    report = probe_embedded_api(module)

    assert "get_server" in report["dir"]
    assert report["signatures"]["get_server"].startswith("get_server(pgdata")
    # 打包的二进制包装函数不算入口，但要留名字（排查时要看得见）
    assert report["bundled_binaries"] == ["initdb", "pg_ctl"]
    assert "pg_ctl" not in report["signatures"]
    assert report["entry"] == "" and report["call"] == "" and report["error"] == ""
    assert report["module"] == "<fake embedded_postgres>"


def test_probe_prefers_get_server_and_never_returns_binary_wrappers() -> None:
    module = _fake_module(
        pg_ctl=_bundled_binary(),
        initdb=_bundled_binary("initdb"),
        PostgresServer=FakeHandle,
        get_server=lambda pgdata, cleanup_mode="stop": FakeHandle(),
    )
    name, entry = resolve_embedded_entry(module)

    assert name == "get_server"
    assert entry is module.get_server


def test_bundled_binary_wrappers_are_never_picked_as_entry() -> None:
    names = ("PostgresServer", "Postgres", "Server", "get_server")
    module = _fake_module(**{name: _bundled_binary(name) for name in names})

    with pytest.raises(RuntimeError) as info:
        resolve_embedded_entry(module)
    assert "未暴露可识别的启动入口" in str(info.value)


def test_resolve_falls_back_to_unknown_name_with_path_first_parameter() -> None:
    def launch(pgdata: Path) -> FakeHandle:  # 名字不在候选名单里
        return FakeHandle()

    name, entry = resolve_embedded_entry(_fake_module(launch=launch))

    assert name == "launch"
    assert entry is launch


def test_real_package_probe_when_installed() -> None:
    """装了 embedded-postgres 就顺手核对一次真实签名（不建库、不起进程）。"""
    module = pytest.importorskip("embedded_postgres")
    report = probe_embedded_api(module)

    assert report["signatures"]["get_server"].startswith("get_server(pgdata")
    assert "pg_ctl" in report["dir"]
    assert "pg_ctl" in report["bundled_binaries"]
    name, entry = resolve_embedded_entry(module)
    assert name == "get_server"
    assert callable(entry)


# ---------------------------------------------------------------- 2. 参数适配


def test_entry_receives_pgdata_keyword_and_cleanup_mode(settings: Settings) -> None:
    server = PostgresServer(settings)
    seen: dict[str, Any] = {}

    def get_server(pgdata: Path, *, cleanup_mode: str = "stop") -> FakeHandle:
        seen["pgdata"] = pgdata
        seen["cleanup_mode"] = cleanup_mode
        return FakeHandle()

    server._call_entry(get_server, "get_server")

    assert seen["pgdata"] == server.data_dir
    assert isinstance(seen["pgdata"], Path)
    # 'stop' = 停服务器但保留 pgdata（契约 §5.2 要同目录重启复用）
    assert seen["cleanup_mode"] == "stop"


def test_entry_with_positional_only_parameter_is_called_positionally(settings: Settings) -> None:
    server = PostgresServer(settings)
    seen: list[Any] = []

    def get_server(pgdata: Path, /) -> FakeHandle:
        seen.append(pgdata)
        return FakeHandle()

    server._call_entry(get_server, "get_server")

    assert seen == [server.data_dir]


def test_entry_accepting_data_dir_keyword_is_adapted(settings: Settings) -> None:
    server = PostgresServer(settings)
    seen: dict[str, Any] = {}

    def create_server(data_dir: Path, port: int = 0) -> FakeHandle:
        seen["data_dir"] = data_dir
        seen["port"] = port
        return FakeHandle()

    server._call_entry(create_server, "create_server")

    assert seen["data_dir"] == server.data_dir


def test_entry_accepting_only_kwargs_gets_the_legacy_arguments(settings: Settings) -> None:
    server = PostgresServer(settings)
    seen: dict[str, Any] = {}

    def start_postgres(**kwargs: Any) -> FakeHandle:
        seen.update(kwargs)
        return FakeHandle()

    server._call_entry(start_postgres, "start_postgres")

    assert seen["pgdata"] == str(server.data_dir)
    assert isinstance(seen["port"], int)
    assert seen["user"] == settings.pg_user


def test_failed_calls_are_collected_with_real_type_errors(settings: Settings) -> None:
    server = PostgresServer(settings)

    def never_works(a: int, b: int, c: int) -> FakeHandle:  # 任何变体都对不上
        raise TypeError("不匹配")

    with pytest.raises(RuntimeError) as info:
        server._call_entry(never_works, "never_works")
    message = str(info.value)
    assert "入口调用全部失败" in message
    assert "TypeError" in message


# ---------------------------------------------------------------- 3. 成功路径


def test_start_uses_embedded_pack_when_package_works(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    prepared: list[dict[str, Any]],
    no_binaries: None,
) -> None:
    handle = FakeHandle()
    module = _fake_module(get_server=lambda pgdata, cleanup_mode="stop": handle)
    monkeypatch.setitem(sys.modules, "embedded_postgres", module)

    server = PostgresServer(settings)
    dsn = server.start()

    assert server.backend == "embedded-pack"
    assert dsn == "postgresql://postgres:@127.0.0.1:5433/chiron"
    assert server.dsn == dsn
    assert prepared == [{"dsn": dsn, "strict": True, "endpoint": ("127.0.0.1", 5433)}]
    assert server.embedded_probe["entry"] == "get_server"
    assert server.embedded_probe["dsn_source"] == "get_uri"
    assert server.attempts == []
    # 差异必须显式说出来，而不是静默忽略 Settings.pg_user / pg_port
    assert any("postgres" in note and settings.pg_user in note for note in server.notes)

    server.stop()
    assert handle.cleaned == 1
    assert server._stop_hook is None


def test_start_falls_back_to_postmaster_info_without_get_uri(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    prepared: list[dict[str, Any]],
    no_binaries: None,
) -> None:
    handle = InfoOnlyHandle(port=6543)
    monkeypatch.setitem(sys.modules, "embedded_postgres", _fake_module(get_server=lambda pgdata: handle))

    server = PostgresServer(settings)
    dsn = server.start()

    assert dsn == "postgresql://postgres:@127.0.0.1:6543/chiron"
    assert server.embedded_probe["dsn_source"] == "postmaster.pid"
    assert prepared[0]["endpoint"] == ("127.0.0.1", 6543)


def test_socket_dsn_means_no_tcp_wait(settings: Settings, monkeypatch: pytest.MonkeyPatch, prepared) -> None:
    """POSIX 上包只开 unix socket：不能拿端口去等 TCP，否则必然超时。"""
    handle = FakeHandle(uri="postgresql://postgres:@/postgres?host=/tmp/chiron-ep")
    monkeypatch.setitem(sys.modules, "embedded_postgres", _fake_module(get_server=lambda pgdata: handle))
    monkeypatch.setattr(
        PostgresServer, "_start_binaries", lambda self: (_ for _ in ()).throw(RuntimeError("禁用一个"))
    )

    server = PostgresServer(settings)
    dsn = server.start()

    assert dsn == "postgresql://postgres:@/chiron?host=/tmp/chiron-ep"
    assert prepared[0]["endpoint"] is None
    assert any("unix socket" in note for note in server.notes)


def test_handle_without_any_stop_entry_is_rejected(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, prepared, no_binaries: None
) -> None:
    class NoStop:
        def get_uri(self, database: str | None = None) -> str:
            return "postgresql://postgres:@127.0.0.1:5433/postgres"

    monkeypatch.setitem(sys.modules, "embedded_postgres", _fake_module(get_server=lambda pgdata: NoStop()))

    with pytest.raises(PostgresUnavailable) as info:
        PostgresServer(settings).start()
    assert "没有任何停止入口" in info.value.attempts[0]


# ---------------------------------------------------------------- 4. 失败回退


def test_failure_keeps_real_exception_and_probe(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, no_binaries: None
) -> None:
    def get_server(pgdata: Path, *, cleanup_mode: str = "stop") -> FakeHandle:
        raise PermissionError("invalid binary ...\\initdb.exe: Permission denied")

    module = _fake_module(get_server=get_server, pg_ctl=_bundled_binary())
    monkeypatch.setitem(sys.modules, "embedded_postgres", module)

    server = PostgresServer(settings)
    with pytest.raises(PostgresUnavailable) as info:
        server.start()

    attempts = info.value.attempts
    assert len(attempts) == 2  # embedded-pack + binaries 都失败
    assert attempts[0].startswith("embedded-pack: RuntimeError:")
    # 真实异常原文
    assert "invalid binary" in attempts[0]
    assert "Permission denied" in attempts[0]
    # 探测结果（dir / 签名 / 真实异常类型）
    assert "embedded_postgres 探测" in attempts[0]
    assert "get_server(pgdata" in attempts[0]
    assert "PermissionError: invalid binary" in attempts[0]
    assert attempts[1].startswith("binaries: RuntimeError: 测试里禁用 binaries 后端")
    # 结构化的探测结果也要能拿到
    assert info.value.probes["entry"] == "get_server"
    assert "PermissionError: invalid binary" in server.embedded_probe["error"]
    assert "embedded-pack" in str(info.value)


def test_missing_package_reports_install_hint(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, no_binaries: None
) -> None:
    monkeypatch.setitem(sys.modules, "embedded_postgres", None)  # import 时抛 ImportError

    with pytest.raises(PostgresUnavailable) as info:
        PostgresServer(settings).start()

    assert "pip install 'embedded-postgres>=18.6.3,<19'" in info.value.attempts[0]


def test_nothing_recognisable_means_no_entry_is_reported(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, no_binaries: None
) -> None:
    monkeypatch.setitem(sys.modules, "embedded_postgres", _fake_module(pg_ctl=_bundled_binary()))

    with pytest.raises(PostgresUnavailable) as info:
        PostgresServer(settings).start()

    assert "未暴露可识别的启动入口" in info.value.attempts[0]
    assert "dir()" in info.value.attempts[0]


# ---------------------------------------------------------------- 5. DSN 工具


def test_replace_database_keeps_socket_host_query() -> None:
    dsn = "postgresql://postgres:@/chiron?host=/var/run/postgresql"
    assert replace_database(dsn, "postgres") == "postgresql://postgres:@/postgres?host=/var/run/postgresql"
    assert database_of(dsn) == "chiron"


def test_tcp_endpoint_is_only_reported_for_tcp_dsns() -> None:
    assert _tcp_endpoint("postgresql://postgres:@127.0.0.1:5433/chiron") == ("127.0.0.1", 5433)
    assert _tcp_endpoint("postgresql://postgres:@/chiron?host=/tmp/x") is None


def test_dsn_from_postmaster_prefers_socket_dir() -> None:
    socket_info = types.SimpleNamespace(socket_dir=Path("ep-socket"), port=None, hostname=None)
    tcp_info = types.SimpleNamespace(socket_dir=None, port=6543, hostname="127.0.0.1")

    assert _dsn_from_postmaster(socket_info, "chiron") == (
        f"postgresql://postgres:@/chiron?host={socket_info.socket_dir}"
    )
    assert _dsn_from_postmaster(tcp_info, "chiron") == "postgresql://postgres:@127.0.0.1:6543/chiron"
    assert _dsn_from_postmaster(types.SimpleNamespace(socket_dir=None, port=None), "chiron") == ""


# ---------------------------------------------------------------- 6. 收尾：说停了就得真停了
#
# 真机事故（2026-11-01 08:46）：Electron 正常退出，引导器也确实走到 ``finally`` 调了
# ``handle.cleanup()``，Redis 被收走，**但 postmaster 还在监听 3786**。根因在上游：
# ``_cleanup()`` 的判据是"``pgdata/.handle_pids.json`` 里只剩我自己"，而那份 DiskList
# **从不清理死 pid** —— 历次被杀掉的引导器在里面留了 14 条，判据于是永远不成立，
# ``cleanup()`` 一进门就 return。下面的用例把这条链一节一节钉住。


class SilentHandle(FakeHandle):
    """模拟上游 ``cleanup()`` 的早退：登记在案，但什么都没停。"""

    def cleanup(self) -> None:
        self.cleaned += 1


def _started(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    handle: Any,
    prepared: list[dict[str, Any]],
    no_binaries: None,
) -> PostgresServer:
    """走完整 embedded-pack 启动，拿到一个带 ``_stop_hook`` 的服务器。"""
    monkeypatch.setitem(sys.modules, "embedded_postgres", _fake_module(get_server=lambda pgdata: handle))
    server = PostgresServer(settings)
    server.start()
    return server


@pytest.fixture
def listener(request: pytest.FixtureRequest) -> socket.socket:
    """一个**真在监听**的 TCP 端口，冒充"cleanup() 说停了但没停"的 postmaster。

    测试里 ``close()`` 它，就等于"pg_ctl 把服务器停了"。
    backlog 要给足：``listen(1)`` 时第一个探测连接（没人 accept）就会把队列塞满，
    之后的探测全被拒 —— 那会让"还在监听"被测成"已经停了"。
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    request.addfinalizer(sock.close)
    return sock


def _listening_uri(sock: socket.socket) -> str:
    return f"postgresql://postgres:@127.0.0.1:{sock.getsockname()[1]}/chiron"


def _registry(server: PostgresServer, *pids: int) -> Path:
    target = server.data_dir / EMBEDDED_HANDLE_PIDS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(list(pids)), encoding="utf-8")
    return target


def test_dead_handle_pids_are_pruned_before_cleanup(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    prepared: list[dict[str, Any]],
    no_binaries: None,
) -> None:
    mine = os.getpid()
    dead = [999_901, 999_902]
    handle = SilentHandle()
    server = _started(settings, monkeypatch, handle, prepared, no_binaries)
    registry = _registry(server, *dead, mine)
    monkeypatch.setattr(pg, "_handle_pid_state", lambda pid: ("live", "python.exe") if pid == mine else ("dead", ""))
    monkeypatch.setattr(PostgresServer, "_confirm_embedded_stopped", lambda self, module: None)

    server.stop()

    assert handle.cleaned == 1
    # 只剩活着的自己：上游"最后一个句柄"的判据这才可能成立
    assert json.loads(registry.read_text(encoding="utf-8")) == [mine]
    assert any("已裁掉" in note and str(dead[0]) in note for note in server.notes)


def test_registry_is_left_alone_when_nobody_is_dead(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    prepared: list[dict[str, Any]],
    no_binaries: None,
    listener: socket.socket,
) -> None:
    """全是存活句柄时**不改写**登记表，也不去停：那是别人正在连的库。"""
    mine = os.getpid()
    other = 999_903
    handle = SilentHandle(uri=_listening_uri(listener))
    server = _started(settings, monkeypatch, handle, prepared, no_binaries)
    registry = _registry(server, mine, other)
    monkeypatch.setattr(pg, "_handle_pid_state", lambda pid: ("live", "python.exe"))
    fallbacks: list[str] = []
    monkeypatch.setattr(
        PostgresServer, "_stop_by_pg_ctl", lambda self, module, host: fallbacks.append(host)
    )

    server.stop()

    assert json.loads(registry.read_text(encoding="utf-8")) == [mine, other]
    assert fallbacks == []
    assert any("存活句柄" in note for note in server.notes)


def test_recycled_pid_is_not_treated_as_a_handle_holder(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    prepared: list[dict[str, Any]],
    no_binaries: None,
) -> None:
    """真机形状：登记表 14 条里 13 条已退出，唯一"存活"的那号已被 webview2 捡走。

    只看"进程在不在"会把它当成"还有别人在用这个库"，于是 ``cleanup()`` 判据永不成立、
    postmaster 永不 —— 必须再看它像不像引导器自己人。
    """
    mine = os.getpid()
    hijacked = 999_904
    handle = SilentHandle()
    server = _started(settings, monkeypatch, handle, prepared, no_binaries)
    registry = _registry(server, mine, hijacked, 999_905)
    states = {hijacked: ("recycled", "msedgewebview2.exe")}
    monkeypatch.setattr(
        pg,
        "_handle_pid_state",
        lambda pid: states.get(pid, ("live", "python.exe") if pid == mine else ("dead", "")),
    )
    verified: list[int] = []
    monkeypatch.setattr(
        PostgresServer, "_confirm_embedded_stopped", lambda self, module: verified.append(1)
    )

    server.stop()

    assert handle.cleaned == 1, "裁掉复用号后 cleanup() 才不会再早退"
    assert json.loads(registry.read_text(encoding="utf-8")) == [mine]
    assert verified == [1], "没有真句柄就该往下验端口"
    assert any("已裁掉 2 条" in note and "msedgewebview2.exe" in note for note in server.notes)


def test_mixed_registry_keeps_the_real_holder_and_prunes_the_rest(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    prepared: list[dict[str, Any]],
    no_binaries: None,
) -> None:
    """同库里既有真句柄（另一个还活着的引导器）又有死号：只裁死号，且**不许**停服务器。"""
    mine = os.getpid()
    other_live = 999_906
    handle = SilentHandle()
    server = _started(settings, monkeypatch, handle, prepared, no_binaries)
    registry = _registry(server, 999_905, mine, other_live)
    monkeypatch.setattr(
        pg,
        "_handle_pid_state",
        lambda pid: {
            mine: ("live", "python.exe"),
            other_live: ("live", "python.exe"),
        }.get(pid, ("dead", "")),
    )
    verified: list[int] = []
    monkeypatch.setattr(
        PostgresServer, "_confirm_embedded_stopped", lambda self, module: verified.append(1)
    )

    server.stop()

    # 写回去的是"我 + 还活着的那个"，不是"只有我"
    assert json.loads(registry.read_text(encoding="utf-8")) == [mine, other_live]
    assert verified == [], "还有别人在用就不去验端口/兜底"
    assert any("已裁掉 1 条" in note for note in server.notes)
    assert any("存活句柄" in note for note in server.notes)


def _fake_psutil(
    monkeypatch: pytest.MonkeyPatch,
    names: dict[int, str],
    *,
    missing: int = ...,
    denied: int = ...,
) -> None:
    # 名字必须与 psutil 真身逐字一致（被测代码按名字取这些异常类），故不改成 *Error 后缀
    class NoSuchProcess(Exception):  # noqa: N818
        pass

    class AccessDenied(Exception):  # noqa: N818
        pass

    class ZombieProcess(Exception):  # noqa: N818
        pass

    class _Proc:
        def __init__(self, pid: int) -> None:
            if pid is missing:
                raise NoSuchProcess(pid)
            if pid is denied:
                raise AccessDenied(pid)
            if pid not in names:
                raise NoSuchProcess(pid)
            self._name = names[pid]

        def name(self) -> str:
            return self._name

    module = types.ModuleType("psutil")
    module.Process = _Proc  # type: ignore[attr-defined]
    module.NoSuchProcess = NoSuchProcess  # type: ignore[attr-defined]
    module.AccessDenied = AccessDenied  # type: ignore[attr-defined]
    module.ZombieProcess = ZombieProcess  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psutil", module)


def test_handle_pid_state_is_judged_by_process_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_psutil(
        monkeypatch,
        {
            111: "msedgewebview2.exe",
            222: "python.exe",
            333: "Python3.EXE",
            444: "",
            # 0 与负数在 Windows 上"查得到名字"（System Idle Process），
            # 少了入口的合法性判断就会被当成活句柄
            0: "System Idle Process",
            -1: "System",
        },
        missing=555,
        denied=666,
    )

    assert pg._handle_pid_state(222) == ("live", "python.exe")
    assert pg._handle_pid_state(333) == ("live", "Python3.EXE"), "名字大小写不能影响判定"
    assert pg._handle_pid_state(111) == ("recycled", "msedgewebview2.exe")
    assert pg._handle_pid_state(555) == ("dead", "")
    assert pg._handle_pid_state(0) == ("dead", ""), "0 / 负数是非法号，不必问系统"
    assert pg._handle_pid_state(-1) == ("dead", "")
    # 认不出是谁（权限不足、名字取不到）一律按"还在用"处理：宁可不停服务器
    assert pg._handle_pid_state(666) == ("live", "")
    assert pg._handle_pid_state(444) == ("live", "")


def test_handle_pid_state_without_psutil_stays_conservative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "psutil", None)  # import 时抛 ImportError

    assert pg._handle_pid_state(999_907) == ("live", "")


def test_port_still_open_falls_back_to_pg_ctl(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    prepared: list[dict[str, Any]],
    no_binaries: None,
    listener: socket.socket,
) -> None:
    """没人用却还开着 ⇒ 退回官方 ``pg_ctl -m fast -w stop``，并把参数钉死。"""
    handle = SilentHandle(uri=_listening_uri(listener))
    server = _started(settings, monkeypatch, handle, prepared, no_binaries)
    _registry(server, os.getpid())
    monkeypatch.setattr(pg, "_handle_pid_state", lambda pid: ("live", "python.exe"))
    monkeypatch.setattr(pg, "_embedded_binary_dir", lambda module: None)
    monkeypatch.setattr(pg, "EMBEDDED_STOP_WAIT_S", 0.2)
    monkeypatch.setattr(PostgresServer, "_find_binary", lambda self, name: "pg_ctl")

    run_calls: list[list[str]] = []

    def stop_run(self: PostgresServer, cmd, timeout, what, check=True, capture=True) -> str:  # noqa: ANN001
        run_calls.append(list(cmd))
        listener.close()  # 兜底生效：端口释放
        return ""

    monkeypatch.setattr(PostgresServer, "_run", stop_run)

    server.stop()

    assert run_calls, "cleanup() 后端口还开着就该走 pg_ctl 兜底"
    cmd = run_calls[0]
    assert cmd[0] == "pg_ctl"
    assert cmd[1:3] == ["-D", str(server.data_dir)]
    assert cmd[3:5] == ["-m", "fast"]
    assert "-w" in cmd and "stop" in cmd
    assert any("兜底成功" in note for note in server.notes)


def test_pg_ctl_fallback_that_still_leaves_the_port_is_reported(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    prepared: list[dict[str, Any]],
    no_binaries: None,
    listener: socket.socket,
) -> None:
    """兜底也没关掉：**不许**说"已停止"，要写"进程未被回收"。"""
    handle = SilentHandle(uri=_listening_uri(listener))
    server = _started(settings, monkeypatch, handle, prepared, no_binaries)
    _registry(server, os.getpid())
    monkeypatch.setattr(pg, "_handle_pid_state", lambda pid: ("live", "python.exe"))
    monkeypatch.setattr(pg, "_embedded_binary_dir", lambda module: None)
    monkeypatch.setattr(pg, "EMBEDDED_STOP_WAIT_S", 0.2)
    monkeypatch.setattr(PostgresServer, "_find_binary", lambda self, name: "pg_ctl")
    monkeypatch.setattr(PostgresServer, "_run", lambda self, cmd, timeout, what, check=True, capture=True: "")

    server.stop()

    assert any("兜底后端口" in note and "未被回收" in note for note in server.notes)
    assert not any("兜底成功" in note or "已停止" in note for note in server.notes)


def test_socket_backend_says_it_cannot_verify_the_stop(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    prepared: list[dict[str, Any]],
    no_binaries: None,
) -> None:
    """unix socket 后端没有端口可验：只能如实说"验不了"，不能默认"停了"。"""
    handle = SilentHandle(uri="postgresql://postgres:@/chiron?host=/tmp/chiron-ep")
    server = _started(settings, monkeypatch, handle, prepared, no_binaries)

    server.stop()

    assert handle.cleaned == 1
    assert any("无法验证服务器已停" in note for note in server.notes)


def test_teardown_reports_a_raising_stop_hook(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    prepared: list[dict[str, Any]],
    no_binaries: None,
) -> None:
    """停止入口抛异常不能吞：吞了就等于对外宣称"停过了"。"""

    class Raising(FakeHandle):
        def cleanup(self) -> None:
            raise RuntimeError("pg_ctl 拒绝停止")

    server = _started(settings, monkeypatch, Raising(), prepared, no_binaries)

    server.stop()

    assert any("停止入口抛了" in note and "pg_ctl 拒绝停止" in note for note in server.notes)


def test_handle_pids_reader_tolerates_garbage(tmp_path: Path) -> None:
    target = tmp_path / EMBEDDED_HANDLE_PIDS_FILE
    assert _read_handle_pids(target) == []

    target.write_text("not json", encoding="utf-8")
    assert _read_handle_pids(target) == []

    target.write_text('{"pid": 1}', encoding="utf-8")
    assert _read_handle_pids(target) == []

    target.write_text('[1, "two", null, 3]', encoding="utf-8")
    assert _read_handle_pids(target) == [1, 3]


# ---------------------------------------------------------------- 7. 首启：上游只等 10s，恢复完了再试一次
#
# 真机事故（2026-11-01 08:39，装完包第一次点本地模式）：embedded-pack 这条路整体判死 ——
# ``TimeoutExpired: Command '[... pg_ctl ... -w ...]' timed out after 10 seconds``，引导器
# 报"每条路径都失败"、本地模式起不来。根因在上游：``_commands.pg_ctl(..., timeout=10)``
# 那个 10s 是**写死的**，只够一次干净启动。上一次 PG 没正常关（被强杀 / 掉电）时
# postmaster 要先跑崩溃恢复，真机实测 33s 才 ready（08:39:37 启动 → 08:40:10 可连）。
# 关键是 pg_ctl 超时**并不影响** postmaster 继续在后台起：等它就绪再调一次入口，上游
# ``ensure_postgres_running()`` 看见活着的 postmaster 就直接附着（读源码核对过），不再跑 pg_ctl。


def _pid_file(data_dir: Path, port: int, status: str) -> None:
    """按 PG 的真格式写一份 ``postmaster.pid``（第 4 行端口，最后一行状态）。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    lines = ["48616", str(data_dir), "1793493839", str(port), "", "127.0.0.1", "", status]
    (data_dir / "postmaster.pid").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_postmaster_pid_file_gives_port_and_status(tmp_path: Path) -> None:
    assert _read_postmaster_file(tmp_path) == (0, "")

    _pid_file(tmp_path, 3786, "ready")
    assert _read_postmaster_file(tmp_path) == (3786, "ready")

    _pid_file(tmp_path, 3786, "starting")
    assert _read_postmaster_file(tmp_path) == (3786, "starting")

    # 端口位不是数字（半写入/没见过的版式）就报"拿不到"，不猜一个端口去连
    (tmp_path / "postmaster.pid").write_text("48616\n\n\nnope\n\n127.0.0.1\n\nready\n", encoding="utf-8")
    assert _read_postmaster_file(tmp_path) == (0, "ready")


def test_only_pg_ctl_timeout_is_treated_as_slow_startup() -> None:
    upstream = subprocess.TimeoutExpired(cmd=["C:/pg/bin/pg_ctl.exe", "-D", "pgdata", "-w", "start"], timeout=10)
    wrapped = RuntimeError(f"{type(upstream).__name__}: {upstream}（调用形态 get_server(pgdata=...)）")
    assert _is_pg_ctl_wait_timeout(wrapped)

    # 缺任意一个特征都不许当成"慢"：否则权限不足/二进制损坏会被重试盖住真因
    assert not _is_pg_ctl_wait_timeout(RuntimeError("PermissionError: 拒绝访问 pg_ctl.exe"))
    assert not _is_pg_ctl_wait_timeout(RuntimeError("TimeoutExpired: 等 SQL 就绪超时"))


def test_await_ready_needs_status_ready_and_a_live_port(
    settings: Settings, listener: socket.socket, tmp_path: Path
) -> None:
    port = listener.getsockname()[1]
    server = PostgresServer(settings)
    # 状态还没翻到 ready：光有 pid 文件不算就绪（这会儿连进去会报 "starting up"）
    _pid_file(server.data_dir, port, "starting")
    assert server._await_postmaster_ready(timeout=0.05) == (False, f"port={port} status=starting")

    # 状态说 ready 但端口没人听：也不能算就绪
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    _pid_file(server.data_dir, dead_port, "ready")
    assert server._await_postmaster_ready(timeout=0.05) == (False, f"port={dead_port} status=ready")

    _pid_file(server.data_dir, port, "ready")
    assert server._await_postmaster_ready(timeout=5.0) == (True, f"port={port} status=ready")


def test_slow_crash_recovery_retries_the_entry_once(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    prepared: list[dict[str, Any]],
    no_binaries: None,
    listener: socket.socket,
) -> None:
    port = listener.getsockname()[1]
    handle = FakeHandle(uri=_listening_uri(listener))
    seen: list[tuple[int, str]] = []

    def get_server(pgdata: Path) -> FakeHandle:
        seen.append(_read_postmaster_file(pgdata))
        if len(seen) == 1:
            # pg_ctl 到点放弃了，但它拉起的 postmaster 还在后台把恢复跑完
            _pid_file(pgdata, port, "ready")
            raise subprocess.TimeoutExpired(cmd=["pg_ctl.exe", "-D", str(pgdata), "-w", "start"], timeout=10)
        return handle

    monkeypatch.setitem(sys.modules, "embedded_postgres", _fake_module(get_server=get_server))
    server = PostgresServer(settings)

    server.start()

    assert len(seen) == 2  # 只重试一次
    assert seen[0] == (0, "")  # 第一次进来时 pgdata 里什么都还没有
    assert seen[1] == (port, "ready")  # 第二次是等它就绪之后才调的
    assert len(prepared) == 1
    assert any("上游 pg_ctl 只等 10s" in note and "重试一次" in note for note in server.notes)
    assert f"port={port} status=ready" in server.embedded_probe["recovery"]


def test_other_failure_is_never_retried(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    prepared: list[dict[str, Any]],
    no_binaries: None,
) -> None:
    calls: list[int] = []

    def get_server(pgdata: Path) -> FakeHandle:
        calls.append(1)
        raise PermissionError("initdb.exe: Permission denied")

    monkeypatch.setitem(sys.modules, "embedded_postgres", _fake_module(get_server=get_server))
    waits: list[float] = []
    monkeypatch.setattr(
        PostgresServer, "_await_postmaster_ready", lambda self, timeout: waits.append(timeout) or (True, "spy")
    )

    server = PostgresServer(settings)
    with pytest.raises(PostgresUnavailable) as info:
        server.start()

    assert len(calls) == 1
    assert waits == []  # 不是"慢"就别等：真因是权限，不是时间
    assert "Permission denied" in info.value.attempts[0]


def test_recovery_that_never_finishes_keeps_the_real_error(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    prepared: list[dict[str, Any]],
    no_binaries: None,
) -> None:
    calls: list[int] = []

    def get_server(pgdata: Path) -> FakeHandle:
        calls.append(1)
        raise subprocess.TimeoutExpired(cmd=["pg_ctl.exe", "-D", str(pgdata), "-w", "start"], timeout=10)

    monkeypatch.setitem(sys.modules, "embedded_postgres", _fake_module(get_server=get_server))

    server = PostgresServer(settings.model_copy(update={"pg_start_timeout": 0.05}))
    with pytest.raises(PostgresUnavailable) as info:
        server.start()

    assert len(calls) == 1  # 等不到就绪就不重试，更不许在里面打转
    attempt = info.value.attempts[0]
    assert "timed out after 10 seconds" in attempt  # 原文不能被包装掉
    assert "等它恢复完也没就绪" in attempt
    assert "上限 0s" in attempt
