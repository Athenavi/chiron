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

import sys
import types
from pathlib import Path
from typing import Any

import pytest
from app.config import Settings
from app.storage.pg import (
    PostgresServer,
    PostgresUnavailable,
    _dsn_from_postmaster,
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
