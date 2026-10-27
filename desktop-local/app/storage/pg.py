"""embedded PostgreSQL 生命周期 + 迁移执行。

后端按固定顺序尝试，**每个后端都要真的能接受 SQL 才被接受**（只起来不算成功）：

1. ``external``       —— 显式给了 ``POSTGRES_DSN``（用户自己的 PG / Docker / 共享实例），
   本进程只管连，不管进程。
2. ``embedded-pack``  —— PyPI 的 ``embedded_postgres`` 包（wheel 里自带整套 PG 二进制，
   不需要系统装 PostgreSQL）。**实测 18.6.x 的真实 API**：

   - ``get_server(pgdata, *, cleanup_mode='stop')`` / ``PostgresServer(pgdata, ...)``：
     **构造就 initdb + pg_ctl start**，返回服务器句柄；
   - ``handle.get_uri(database)`` 给连接串，``handle.cleanup()`` 停服务器（保留 pgdata）；
   - 入口**没有** ``port`` / ``user`` / ``password`` 参数：端口由包内部挑一个空闲口
     （POSIX 上直接用 unix socket，连 127.0.0.1 都不监听），超级用户固定 ``postgres``
     且 ``--auth=trust``。

   因此这里先做**特征探测**（``dir()`` + 每个入口的真实签名）再按签名适配调用；
   失败时把真实异常与探测结果逐条写进 :class:`PostgresUnavailable`。
3. ``binaries``       —— ``PATH``（或 ``CHIRON_LOCAL_PG_BIN_DIR``）里的 ``initdb`` / ``pg_ctl``，
   行为完全由 PostgreSQL 官方工具保证，是 embedded-pack 装不上时的兜底。

三条都不通 → :class:`PostgresUnavailable`，异常里带**每条路的真实错误**与可执行建议。

迁移复用仓库根的 ``migrations/``（只读），通过 ``alembic upgrade head`` 执行。
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.config import Settings

# PyPI 包名（import 名是 embedded_postgres）
_EMBEDDED_MODULE = "embedded_postgres"

# embedded 包内部固定用它做超级用户（``initdb -U postgres --auth=trust``），
# 与 ``Settings.pg_user`` 无关 —— 见 :meth:`PostgresServer._embedded_notes`。
_EMBEDDED_SUPERUSER = "postgres"

# embedded_postgres 包在各版本里用过的公开入口名（只做特征探测，命中哪个用哪个；顺序 = 优先级）
_EMBEDDED_ENTRYPOINTS = (
    "get_server",
    "PostgresServer",
    "create_server",
    "start_postgres",
    "EmbeddedPostgres",
    "PgServer",
    "Postgres",
    "Server",
)

# 入口函数的“数据目录”参数在不同版本里可能叫这些名字
_PATH_PARAMS = ("pgdata", "data_dir", "datadir", "pg_data", "db_dir", "path", "directory")

# postgresql://user[:pwd]@host:port/db —— 用来判断 DSN 走 TCP 还是 unix socket
_TCP_DSN = re.compile(r"^postgres(?:ql)?://[^/?#]*@(?P<host>[^:/?#@]+):(?P<port>\d+)/")

# 库名会用字面量插进 CREATE DATABASE，必须是可校验的标识符
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

PG_SUGGESTIONS = (
    "给一个现成实例：set POSTGRES_DSN=postgresql://user:pwd@127.0.0.1:5432/chiron",
    "装 PostgreSQL 16+ 并指向其 bin 目录：set CHIRON_LOCAL_PG_BIN_DIR=C:\\Program Files\\PostgreSQL\\16\\bin",
    "用容器（CI/本机均可）：docker run --rm -e POSTGRES_PASSWORD=chiron -p 55432:5432 postgres:16",
    "只想先跑通 Redis/HTTP 骨架：CHIRON_LOCAL_PG_ENABLED=false（/readyz 会报 pg=false）",
)


class PostgresUnavailable(RuntimeError):  # noqa: N818 —— 名字表达“不可用”而非“错误”
    """所有 PG 后端都不可用。``attempts`` 里是逐条真实错误。

    ``probes`` 是 embedded_postgres 的探测结果（``dir()`` / 真实签名 / 实际调用形态），
    失败时和 ``attempts`` 一起上报 —— 不猜、不美化。
    """

    def __init__(self, attempts: Sequence[str], probes: dict[str, Any] | None = None) -> None:
        self.attempts = list(attempts)
        self.probes: dict[str, Any] = dict(probes or {})
        lines = ["embedded PostgreSQL 不可用，以下每条路都失败了："]
        lines.extend(f"  - {item}" for item in self.attempts)
        lines.append("可执行建议：")
        lines.extend(f"  - {item}" for item in PG_SUGGESTIONS)
        super().__init__("\n".join(lines))


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _brief(value: Any) -> str:
    """把调用参数压成一行（Path 直接给路径，省掉 ``PosixPath(...)`` 噪音）。"""
    return str(value) if isinstance(value, Path) else repr(value)


def _is_bundled_binary(obj: Any) -> bool:
    """``embedded_postgres`` 把 ``pginstall/bin`` 下每个可执行文件都导出成函数
    （``pg_ctl`` / ``initdb`` / ``psql`` / ``postgres``…）。

    这些也是“可调用的公有属性”，但调用它们不是启动服务器，探测入口时必须排除。
    """
    return getattr(obj, "__module__", "").endswith("._commands")


def _public_names(module: Any) -> list[str]:
    return sorted(name for name in dir(module) if not name.startswith("_"))


def probe_embedded_api(module: Any) -> dict[str, Any]:
    """把 ``embedded_postgres`` 的真实形状记下来：``dir()``、候选入口签名、版本。

    ``signatures`` 只收“像服务器入口”的可调用对象（候选名单 + 首参像数据目录的），
    打包进包的几十个二进制包装函数（``pg_ctl`` / ``initdb`` / ``psql``…）只留名字，
    否则这份探测结果会长到没人看。
    """
    names = _public_names(module)
    signatures: dict[str, str] = {}
    bundled: list[str] = []
    for name in names:
        obj = getattr(module, name, None)
        if inspect.ismodule(obj) or not callable(obj):
            continue
        if _is_bundled_binary(obj):
            bundled.append(name)
            continue
        try:
            signature = inspect.signature(obj)
        except (TypeError, ValueError) as exc:  # 内建/桥接对象拿不到签名
            signatures[name] = f"{name}(<签名不可用：{type(exc).__name__}>）"
            continue
        first = next(iter(signature.parameters), "")
        if name in _EMBEDDED_ENTRYPOINTS or first.lower() in _PATH_PARAMS:
            signatures[name] = f"{name}{signature}"
    version = ""
    with contextlib.suppress(Exception):
        from importlib.metadata import version as _dist_version

        version = _dist_version("embedded-postgres")
    return {
        "module": str(getattr(module, "__file__", "") or ""),
        "version": version,
        "dir": names,
        "bundled_binaries": bundled,
        "signatures": signatures,
        "entry": "",
        "call": "",
        "error": "",
    }


def format_probe(report: dict[str, Any]) -> str:
    """把探测结果压成几行，塞进异常/上报（不隐藏任何事实）。"""
    lines = [
        "embedded_postgres 探测：module={} version={}".format(
            report.get("module") or "<未知>", report.get("version") or "<未知>"
        )
    ]
    names = report.get("dir") or []
    lines.append(f"实测 dir()（{len(names)} 项）= " + ", ".join(names))
    signatures = report.get("signatures") or {}
    if signatures:
        lines.extend(f"候选入口签名 {signature}" for signature in signatures.values())
    else:
        lines.append("候选入口签名 <一个都没有>")
    bundled = report.get("bundled_binaries") or []
    if bundled:
        lines.append(f"（其中 {len(bundled)} 个是包内二进制包装函数，已排除：{', '.join(bundled)}）")
    if report.get("entry"):
        lines.append(f"选中的入口：{report['entry']}")
    if report.get("call"):
        lines.append(f"实际调用：{report['call']}")
    if report.get("error"):
        lines.append(f"真实异常：{report['error']}")
    return "\n".join(lines)


def resolve_embedded_entry(module: Any) -> tuple[str, Any]:
    """找出真正能启动服务器的入口；找不到就抛（调用方会把探测结果一起上报）。"""
    for name in _EMBEDDED_ENTRYPOINTS:
        obj = getattr(module, name, None)
        if obj is None or inspect.ismodule(obj) or not callable(obj) or _is_bundled_binary(obj):
            continue
        return name, obj

    # 名字不认识也别直接放弃：挑一个“第一个参数像数据目录”的公有可调用对象
    for name in _public_names(module):
        obj = getattr(module, name, None)
        if obj is None or inspect.ismodule(obj) or not callable(obj) or _is_bundled_binary(obj):
            continue
        try:
            params = list(inspect.signature(obj).parameters)
        except (TypeError, ValueError):
            continue
        if params and params[0].lower() in _PATH_PARAMS:
            return name, obj
    raise RuntimeError(
        "embedded_postgres 未暴露可识别的启动入口"
        "（既没有 get_server/PostgresServer，也没有首参为数据目录的公有可调用对象）"
    )


def _tcp_endpoint(dsn: str) -> tuple[str, int] | None:
    """DSN 走 TCP 就返回 ``(host, port)``；unix socket（``?host=/dir``）返回 ``None``。"""
    match = _TCP_DSN.match(dsn)
    if match is None:
        return None
    return match.group("host"), int(match.group("port"))


def _dsn_from_postmaster(info: Any, database: str) -> str:
    """句柄没有 ``get_uri()`` 时，用 postmaster 信息拼 DSN（socket 优先，其次 TCP）。"""
    socket_dir = getattr(info, "socket_dir", None)
    port = getattr(info, "port", None)
    host = getattr(info, "hostname", None) or "127.0.0.1"
    if socket_dir:
        return f"postgresql://{_EMBEDDED_SUPERUSER}:@/{quote(database, safe='')}?host={socket_dir}"
    if port:
        return build_dsn(_EMBEDDED_SUPERUSER, "", str(host), int(port), database)
    return ""


def mask_dsn(dsn: str) -> str:
    """把 DSN 里的密码换成 ``***``（任何上报/日志都必须走这里）。"""
    if not dsn or "@" not in dsn:
        return dsn or ""
    scheme, _, rest = dsn.partition("://")
    userinfo, _, host = rest.rpartition("@")
    user = userinfo.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


def build_dsn(user: str, password: str, host: str, port: int, database: str) -> str:
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database, safe='')}"
    )


def replace_database(dsn: str, database: str) -> str:
    """把 DSN 的库名换掉（用于连 ``postgres`` 维护库建库；保留 query 参数）。

    先把 query 摘掉再找最后一个 ``/``：unix socket 形态的 DSN
    （``postgresql://user:@/db?host=/tmp/xxx``，embedded-pack 在 POSIX 上给的就是它）
    的 query 里也有斜杠，直接 ``rpartition("/")`` 会把 ``?host=`` 切坏。
    """
    base, sep, query = dsn.partition("?")
    head, _, _tail = base.rpartition("/")
    return f"{head}/{quote(database, safe='')}{sep}{query}"


def database_of(dsn: str) -> str:
    """从 DSN 取库名（去掉 ``?sslmode=...`` 这类 query）。"""
    return dsn.partition("?")[0].rpartition("/")[2]


def find_free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def wait_for_port(host: str, port: int, timeout: float = 60.0) -> bool:
    """等 TCP 可连（PG 起来后再等 SQL 可用，见 :meth:`PostgresServer._prepare`）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            try:
                sock.connect((host, port))
                return True
            except OSError:
                time.sleep(0.1)
    return False


@contextlib.contextmanager
def _dotenv_noop() -> Iterator[None]:
    """临时把 ``dotenv.load_dotenv`` 打成空操作。

    为什么必须这么做：仓库根 ``migrations/env.py`` 在模块顶层调用
    ``load_dotenv(..., override=True)`` 读仓库的 ``.env``。若那份 ``.env`` 里有
    ``DATABASE_DSN``，它会**覆盖**我们刚设好的本地实例 DSN —— 迁移就会打到开发库上。
    这里让 load_dotenv 变成空操作，配合 ``DATABASE_DSN`` 环境变量，迁移只可能落在
    我们自己的实例里（事后还会用 ``alembic_version`` 复核一次）。
    """
    try:
        import dotenv
    except ImportError:
        yield
        return
    original = dotenv.load_dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False  # type: ignore[assignment]
    try:
        yield
    finally:
        dotenv.load_dotenv = original  # type: ignore[assignment]


def run_sql(dsn: str, sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
    """同步执行一条 SQL 并返回全部行（走 psycopg2，迁移侧同源）。"""
    import psycopg2

    conn = psycopg2.connect(dsn, connect_timeout=10)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return []
            return list(cur.fetchall())
    finally:
        conn.close()


class PostgresServer:
    """embedded PostgreSQL 的启停 + 迁移 + 健康检查。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.data_dir: Path = settings.pg_data_dir
        self._backend = "none"
        self._dsn = settings.postgres_dsn or ""
        self._started = False
        self._stop_hook: Any = None
        self.attempts: list[str] = []
        # 后端能力差异（embedded-pack 固定 postgres 用户 / 自己挑端口），上报用
        self.notes: list[str] = []
        # embedded_postgres 的真实探测结果（失败时随异常一起上报）
        self.embedded_probe: dict[str, Any] = {}

    # ------------------------------------------------------------------ 属性
    @property
    def backend(self) -> str:
        return self._backend

    @property
    def dsn(self) -> str:
        return self._dsn

    @property
    def safe_dsn(self) -> str:
        return mask_dsn(self._dsn)

    # ------------------------------------------------------------------ 启停
    def start(self) -> str:
        """启动（或连接）PG，返回可用的 DSN。"""
        if self._started and self._dsn:
            return self._dsn

        if self.settings.postgres_dsn:
            self._backend = "external"
            self._dsn = self.settings.postgres_dsn
            self._prepare(self._dsn, strict=False)
            self._started = True
            return self._dsn

        errors: list[str] = []
        self.notes = []
        for name, starter in (
            ("embedded-pack", self._start_embedded_pack),
            ("binaries", self._start_binaries),
        ):
            try:
                dsn = starter()
            except Exception as exc:  # noqa: BLE001 —— 逐条记录真实错误，再试下一条
                errors.append(f"{name}: {type(exc).__name__}: {_indent(str(exc), '      ').lstrip()}")
                self._teardown()
                continue
            self._backend = name
            self._dsn = dsn
            self._started = True
            self.attempts = errors
            return dsn

        self.attempts = errors
        raise PostgresUnavailable(errors, self.embedded_probe)

    def stop(self) -> None:
        """停止 PG。``external`` 后端不动别人的进程。"""
        if self._backend == "external":
            self._started = False
            return
        self._teardown()
        self._started = False

    def _teardown(self) -> None:
        hook = self._stop_hook
        self._stop_hook = None
        if hook is None:
            return
        with contextlib.suppress(Exception):
            hook()

    # ------------------------------------------------------- 后端 1：embedded 包
    def _start_embedded_pack(self) -> str:
        """用 PyPI 的 ``embedded_postgres``（wheel 自带 PG 二进制）起一个本地实例。

        实测的真实形态（18.6.x）::

            get_server(pgdata, *, cleanup_mode='stop')   # 构造即 initdb + pg_ctl start
            handle.get_uri(database)                     # postgresql://postgres:@host:port/db
            handle.cleanup()                             # 停服务器，保留 pgdata

        入口**没有** port/user/password 参数，所以这里先探测再按签名适配；
        任何一步失败都把真实异常 + 探测结果塞进异常文本（→ ``PostgresUnavailable.attempts``）。
        """
        report: dict[str, Any] = {}
        self.embedded_probe = report
        try:
            try:
                module = importlib.import_module(_EMBEDDED_MODULE)
            except ImportError as exc:
                report["error"] = f"{type(exc).__name__}: {exc}"
                raise RuntimeError(
                    f"{exc}（装包：pip install 'embedded-postgres>=18.6.3,<19'）"
                ) from exc

            report.update(probe_embedded_api(module))
            entry_name, entry = resolve_embedded_entry(module)
            report["entry"] = entry_name

            # get_server() 要求 pgdata 的父目录已存在；Settings.ensure_dirs() 一般建好了，
            # 但脚本/测试里不一定调过，这里兜一次
            self.data_dir.parent.mkdir(parents=True, exist_ok=True)
            self.data_dir.mkdir(parents=True, exist_ok=True)

            handle, call_desc = self._call_entry(entry, entry_name)
            report["call"] = call_desc
            self._ensure_handle_running(handle)
            dsn = self._handle_dsn(handle, report)
            report["dsn"] = mask_dsn(dsn)
        except Exception as exc:  # noqa: BLE001 —— 探测结果必须和真实异常一起上去
            if not report.get("error"):  # 导入失败那条已经写过了，别用包装后的消息覆盖
                report["error"] = f"{type(exc).__name__}: {exc}"
            detail = format_probe(report)
            message = str(exc)
            # 消息本身不要再带一层类型名（调用方会写 ``backend: 类型: 消息``）；
            # 真实异常类型保留在探测结果的 “真实异常” 一行里
            raise RuntimeError(message if detail in message else f"{exc}\n{detail}") from exc

        stop_hook = self._handle_stop_hook(handle)
        if stop_hook is None:
            raise RuntimeError(
                "embedded_postgres 返回的句柄没有任何停止入口（cleanup/stop/close/__exit__），"
                f"无法管理其生命周期；dir(handle) = {_public_names(handle)}\n{format_probe(report)}"
            )
        self._stop_hook = stop_hook

        self.notes.extend(self._embedded_notes(dsn))
        # 端口由包内部挑（POSIX 上干脆只有 unix socket），拿不到 endpoint 就只靠 SQL 轮询
        self._prepare(dsn, strict=True, endpoint=_tcp_endpoint(dsn))
        return dsn

    def _call_entry(self, entry: Any, entry_name: str) -> tuple[Any, str]:
        """按**真实签名**适配调用入口，返回 ``(句柄, 实际调用形态)``。

        历史教训：18.6.x 的入口只吃 ``pgdata``（位置参数），老代码按
        ``data_dir=/port=/user=`` 猜，五个变体全是 ``TypeError`` —— 于是 embedded-pack
        永远“不可用”，实际一直悄悄回退到系统二进制。这里改成先看 ``inspect.signature``。
        """
        pgdata = self.data_dir
        extra: dict[str, Any] = {}
        named: dict[str, Any] | None = None
        try:
            params = inspect.signature(entry).parameters
        except (TypeError, ValueError):  # 拿不到签名（C 扩展/桥接对象）
            params = {}  # type: ignore[assignment]
        for name in _PATH_PARAMS:
            if name in params:
                named = {name: pgdata}
                break
        if "cleanup_mode" in params:
            # 'stop' = 只停服务器、保留 pgdata（契约 §5.2 要同目录重启复用）
            extra["cleanup_mode"] = "stop"

        variants: list[tuple[dict[str, Any], tuple[Any, ...]]] = []
        if named is not None:
            variants.append(({**named, **extra}, ()))
        variants.append((dict(extra), (pgdata,)))
        variants.append((dict(extra), (str(pgdata),)))
        variants.extend(self._legacy_embedded_variants())  # 最后才是老路（参数从多到少猜）

        errors: list[str] = []
        for kwargs, args in variants:
            shape = (
                f"{entry_name}("
                + ", ".join([f"{key}={_brief(value)}" for key, value in kwargs.items()] + [_brief(a) for a in args])
                + ")"
            )
            try:
                return entry(*args, **kwargs), shape
            except TypeError as exc:
                # 只有“参数对不上”才继续换下一个形态
                errors.append(f"{shape} → TypeError: {exc}")
            except Exception as exc:  # noqa: BLE001 —— 形态对了、但服务器真的起不来
                raise RuntimeError(f"{type(exc).__name__}: {exc}（调用形态 {shape}）") from exc
        raise RuntimeError("embedded_postgres 入口调用全部失败：\n    " + "\n    ".join(errors))

    def _legacy_embedded_variants(self) -> list[tuple[dict[str, Any], tuple[Any, ...]]]:
        """给“名字对不上、签名也没见过”的版本留的最后一条路（按参数从多到少试）。"""
        port = self._pick_port()
        full: dict[str, Any] = {
            "pgdata": str(self.data_dir),
            "data_dir": str(self.data_dir),
            "port": port,
            "user": self.settings.pg_user,
            "username": self.settings.pg_user,
            "password": self.settings.pg_password,
            "database": self.settings.pg_database,
            "dbname": self.settings.pg_database,
        }
        drops = (
            (),
            ("database", "dbname"),
            ("database", "dbname", "username", "user", "password"),
            ("database", "dbname", "username", "user", "password", "port"),
            ("database", "dbname", "username", "user", "password", "port", "data_dir"),
        )
        variants: list[tuple[dict[str, Any], tuple[Any, ...]]] = [
            ({key: value for key, value in full.items() if key not in drop}, ()) for drop in drops
        ]
        variants.append(({}, ()))
        return variants

    def _ensure_handle_running(self, handle: Any) -> None:
        """18.6.x 构造即启动；这里只给“构造不启动”的形态补一步（幂等）。"""
        get_pid = getattr(handle, "get_pid", None)
        if callable(get_pid):
            with contextlib.suppress(Exception):
                if get_pid():
                    return
        for name in ("ensure_pgdata_inited", "ensure_postgres_running"):
            step = getattr(handle, name, None)
            if callable(step):
                step()

    def _handle_dsn(self, handle: Any, report: dict[str, Any]) -> str:
        """从句柄取真实连接串：优先 ``get_uri()``，其次 postmaster 信息，都不行就报错。"""
        database = self.settings.pg_database
        get_uri = getattr(handle, "get_uri", None)
        if callable(get_uri):
            for args, kwargs in (((), {"database": database}), ((database,), {}), ((), {})):
                try:
                    uri = get_uri(*args, **kwargs)
                except TypeError:
                    continue
                if not isinstance(uri, str) or not uri.startswith("postgres"):
                    raise RuntimeError(f"handle.get_uri() 返回的不是 PG 连接串：{uri!r}")
                report["dsn_source"] = "get_uri"
                return uri

        info = None
        get_info = getattr(handle, "get_postmaster_info", None)
        if callable(get_info):
            info = get_info()
        if info is not None:
            dsn = _dsn_from_postmaster(info, database)
            if dsn:
                report["dsn_source"] = "postmaster.pid"
                return dsn
        raise RuntimeError(
            "embedded_postgres 句柄既没有 get_uri()，也读不到 postmaster 信息；"
            f"dir(handle) = {_public_names(handle)}"
        )

    @staticmethod
    def _handle_stop_hook(handle: Any) -> Any:
        """句柄的停止入口（embedded_postgres 是 ``cleanup``：停服务器、保留 pgdata）。"""
        for name in ("cleanup", "stop", "close", "shutdown"):
            hook = getattr(handle, name, None)
            if callable(hook):
                return hook
        if callable(getattr(handle, "__exit__", None)):
            return lambda: handle.__exit__(None, None, None)
        return None

    def _embedded_notes(self, dsn: str) -> list[str]:
        """把 embedded-pack 与 binaries 的行为差异显式记下来（不静默忽略配置）。"""
        notes: list[str] = []
        user = dsn.split("://", 1)[-1].split(":", 1)[0] or "?"
        if user != self.settings.pg_user:
            notes.append(
                f"embedded-pack 固定用 `{user}`（initdb --auth=trust，仅 loopback/socket）连接，"
                f"pg_user={self.settings.pg_user} 在该后端下不生效"
            )
        endpoint = _tcp_endpoint(dsn)
        if endpoint is None:
            notes.append("embedded-pack 在该平台用 unix socket（DSN 里是 ?host=<目录>），没有 TCP 端口")
        elif self.settings.pg_port and endpoint[1] != self.settings.pg_port:
            notes.append(
                f"embedded-pack 自己挑空闲端口（实测 {endpoint[1]}），pg_port={self.settings.pg_port} 不生效"
            )
        return notes

    # -------------------------------------------------------- 后端 2：官方二进制
    def _start_binaries(self) -> str:
        initdb = self._find_binary("initdb")
        pg_ctl = self._find_binary("pg_ctl")
        if initdb is None or pg_ctl is None:
            raise RuntimeError(
                "PATH 上找不到 initdb/pg_ctl（可用 CHIRON_LOCAL_PG_BIN_DIR 指定 PostgreSQL bin 目录）"
            )

        port = self._pick_port()
        if not (self.data_dir / "PG_VERSION").exists():
            self.data_dir.parent.mkdir(parents=True, exist_ok=True)
            cmd = [
                initdb,
                "-D",
                str(self.data_dir),
                "-U",
                self.settings.pg_user,
                "--auth=trust",
                "-E",
                "UTF8",
            ]
            self._run(cmd, timeout=self.settings.pg_start_timeout, what="initdb")

        self.settings.pg_log_file.parent.mkdir(parents=True, exist_ok=True)
        options = f"-p {port} -c listen_addresses=127.0.0.1"
        # **不要用 `-w`**：Windows 上 `pg_ctl -w` 会在 postmaster 已监听后仍卡在
        # 等待 pid/socket 的循环里（实测 90s 超时）。这里让它启动后立刻返回，
        # 由 `_prepare` 的 wait_for_port + SQL 轮询判断“真的可用”。
        cmd = [
            pg_ctl,
            "-D",
            str(self.data_dir),
            "-l",
            str(self.settings.pg_log_file),
            "-o",
            options,
            "-W",
            "start",
        ]
        self._run(cmd, timeout=30, what="pg_ctl start", capture=False)

        dsn = build_dsn(
            self.settings.pg_user, self.settings.pg_password, "127.0.0.1", port, self.settings.pg_database
        )
        self._stop_hook = lambda: self._stop_binaries(pg_ctl)
        self._prepare(dsn, strict=True, endpoint=("127.0.0.1", port))
        return dsn

    def _stop_binaries(self, pg_ctl: str) -> None:
        cmd = [pg_ctl, "-D", str(self.data_dir), "-m", "fast", "-w", "-t", "30", "stop"]
        self._run(cmd, timeout=60, what="pg_ctl stop", check=False, capture=False)

    def _find_binary(self, name: str) -> str | None:
        suffix = ".exe" if os.name == "nt" else ""
        if self.settings.pg_bin_dir:
            candidate = Path(self.settings.pg_bin_dir) / f"{name}{suffix}"
            if candidate.exists():
                return str(candidate)
        return shutil.which(name)

    # ------------------------------------------------------------------ 工具
    def _pick_port(self) -> int:
        if self.settings.pg_port:
            return self.settings.pg_port
        return find_free_port()

    def _run(
        self,
        cmd: Sequence[str],
        timeout: float,
        what: str,
        check: bool = True,
        capture: bool = True,
    ) -> str:
        """跑一条外部命令。

        ``capture=False`` 用于 ``pg_ctl start/stop``：postmaster 会继承父进程的
        stdout/stderr 管道并长期持有，``capture_output=True`` 的 ``communicate()``
        会一直等 EOF（Windows 实测卡死）。启动/停止只关心退出码，输出丢 DEVNULL。
        """
        kwargs: dict[str, Any] = {"timeout": timeout, "check": False}
        if capture:
            kwargs.update(capture_output=True, text=True)
        else:
            kwargs.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            proc = subprocess.run(  # noqa: S603 —— 命令与参数都是本进程拼的，无用户输入拼接
                list(cmd),
                **kwargs,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"{what}: 可执行文件不存在（{cmd[0]}）") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{what}: 超过 {timeout:.0f}s 未返回") from exc
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"{what} 退出码 {proc.returncode}: {detail[-2000:]}")
        return proc.stdout or ""

    def _prepare(self, dsn: str, strict: bool, endpoint: tuple[str, int] | None = None) -> None:
        """等端口 → 等 SQL → 建库。只把**真能执行 SQL** 的实例算启动成功。"""
        if endpoint is not None:
            host, port = endpoint
            if not wait_for_port(host, port, timeout=self.settings.pg_start_timeout):
                raise RuntimeError(f"等待 {host}:{port} 可连接超时（见 {self.settings.pg_log_file}）")

        # 自己拉的实例允许重试（起库期间连接被拒是常态）；外部 DSN 只试一次，别让用户等 60s
        deadline = time.monotonic() + (self.settings.pg_start_timeout if strict else 0.0)
        last_error: Exception | None = None
        while True:
            try:
                run_sql(replace_database(dsn, "postgres"), "SELECT 1")
                break
            except Exception as exc:  # noqa: BLE001 —— 起库过程中连接失败是常态
                last_error = exc
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"实例不接受 SQL 连接：{type(last_error).__name__}: {last_error}"
                    ) from last_error
                time.sleep(0.2)

        database = database_of(dsn)
        if not _IDENTIFIER.fullmatch(database):
            raise RuntimeError(f"库名只允许 [A-Za-z_][A-Za-z0-9_]*：{database!r}")
        try:
            existing = run_sql(
                replace_database(dsn, "postgres"),
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (database,),
            )
            if not existing:
                run_sql(replace_database(dsn, "postgres"), f'CREATE DATABASE "{database}"')
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise RuntimeError(f"建库 {database} 失败：{type(exc).__name__}: {exc}") from exc

    # ------------------------------------------------------------------ 迁移
    def migrate(self, dsn: str | None = None) -> dict[str, Any]:
        """跑仓库根 ``migrations/`` 的 ``alembic upgrade head``，返回真实结果。"""
        target = dsn or self._dsn
        if not target:
            raise RuntimeError("迁移前必须先 start()（没有 DSN）")
        migrations_dir = self.settings.migrations_dir
        if not migrations_dir.is_dir():
            raise RuntimeError(f"找不到迁移目录：{migrations_dir}")

        # SQLAlchemy 2.1 起 `postgresql://` 默认走 psycopg(v3)；本地环境装的是
        # psycopg2-binary（与 run_sql 同源）。显式指定驱动，避免 alembic 报
        # `ModuleNotFoundError: No module named 'psycopg'`。
        alembic_target = target
        if alembic_target.startswith("postgresql://"):
            alembic_target = "postgresql+psycopg2://" + alembic_target[len("postgresql://") :]

        cfg = self._alembic_config(alembic_target, migrations_dir)
        previous = os.environ.get("DATABASE_DSN")
        os.environ["DATABASE_DSN"] = alembic_target
        started = time.perf_counter()
        try:
            from alembic import command

            with _dotenv_noop():
                command.upgrade(cfg, "head")
        finally:
            if previous is None:
                os.environ.pop("DATABASE_DSN", None)
            else:
                os.environ["DATABASE_DSN"] = previous
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

        revision = self.current_revision(target)
        if not revision:
            raise RuntimeError(
                "迁移后读不到 alembic_version —— 迁移很可能落到了别的库（检查 DATABASE_DSN 是否被 .env 覆盖）"
            )
        return {
            "ok": True,
            "revision": revision,
            "elapsed_ms": elapsed_ms,
            "migrations_dir": str(migrations_dir),
        }

    def _alembic_config(self, dsn: str, migrations_dir: Path) -> Any:
        """优先用程序化 Config（无 alembic.ini 时不会重配 logging），失败则读仓库 alembic.ini。"""
        from alembic.config import Config as AlembicConfig

        script_location = str(migrations_dir).replace("%", "%%")

        def _apply(cfg: Any) -> Any:
            cfg.set_main_option("script_location", script_location)
            cfg.set_main_option("sqlalchemy.url", dsn.replace("%", "%%"))
            return cfg

        with contextlib.suppress(Exception):
            return _apply(AlembicConfig())
        return _apply(AlembicConfig(str(self.settings.alembic_ini)))

    def current_revision(self, dsn: str | None = None) -> str | None:
        target = dsn or self._dsn
        if not target:
            return None
        try:
            rows = run_sql(target, "SELECT version_num FROM alembic_version")
        except Exception:  # noqa: BLE001 —— 没有这张表就是没迁移过
            return None
        return str(rows[0][0]) if rows else None

    # ------------------------------------------------------------------ 健康
    def health(self) -> dict[str, Any]:
        """同步健康检查（psycopg2）。任何异常都进 ``error``，不抛。"""
        result: dict[str, Any] = {
            "ok": False,
            "backend": self._backend,
            "dsn": self.safe_dsn,
            "server_version": "",
            "database": "",
            "error": "",
        }
        if not self._dsn:
            result["error"] = "未启动"
            return result
        try:
            rows = run_sql(
                self._dsn,
                "SELECT current_setting('server_version'), current_database()",
            )
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
        if rows:
            result["server_version"] = str(rows[0][0])
            result["database"] = str(rows[0][1])
        result["ok"] = True
        return result

    async def health_async(self, timeout: float = 5.0) -> dict[str, Any]:
        """异步健康检查（asyncpg），给 ``/readyz`` 用。"""
        result: dict[str, Any] = {
            "ok": False,
            "backend": self._backend,
            "dsn": self.safe_dsn,
            "server_version": "",
            "database": "",
            "error": "",
        }
        if not self._dsn:
            result["error"] = "未启动"
            return result
        try:
            import asyncpg

            conn = await asyncpg.connect(dsn=self._dsn, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
        try:
            row = await conn.fetchrow(
                "SELECT current_setting('server_version') AS version, current_database() AS database"
            )
            if row is not None:
                result["server_version"] = str(row["version"])
                result["database"] = str(row["database"])
            result["ok"] = True
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            await conn.close()
        return result
