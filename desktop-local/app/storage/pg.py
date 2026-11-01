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
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Collection, Iterator, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.config import Settings

# PyPI 包名（import 名是 embedded_postgres）
_EMBEDDED_MODULE = "embedded_postgres"

# embedded 包内部固定用它做超级用户（``initdb -U postgres --auth=trust``），
# 与 ``Settings.pg_user`` 无关 —— 见 :meth:`PostgresServer._embedded_notes`。
_EMBEDDED_SUPERUSER = "postgres"

# 随包 PG 二进制的默认落位（相对本文件：desktop-local/app/storage/ → desktop-local/bin/pg/bin/）
#
# **必须指向 ``bin/`` 而不是 ``bin/pg/``**：``initdb`` / ``pg_ctl`` 是官方发行版构建，
# 它们按相对路径（``../share``、``../lib``）找模板与动态库。把 ``bin``、``lib``、``share``
# 保持同级拷过来就能独立运行；只拷 ``bin`` 会在 initdb 阶段报找不到 ``postgres.bki``。
BUNDLED_PG_BIN_DIR = Path(__file__).resolve().parents[2] / "bin" / "pg" / "bin"

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

#: 端口冲突时最多尝试几次（含首次）。
#: ``find_free_port`` 只是"借号"，close 到别人 bind 之间有竞态（Windows 上
#: 127.0.0.1 还会撞 hyper-v 保留段 / TIME_WAIT），所以启动失败像端口冲突时必须换端口重试。
PORT_ATTEMPTS = 3

#: 端口/地址被占用的特征串（大小写不敏感）；与
#: ``frontend-vue/electron/defaults.cjs`` 的 ``PORT_CONFLICT_PATTERNS`` 保持一致。
PORT_CONFLICT_PATTERNS = (
    "address already in use",
    "address in use",
    "eaddrinuse",
    "only one usage of each socket address",
    "port is already allocated",
    "could not bind",
    "failed to bind",
    "cannot bind",
    "bind failed",
    "another server might be running",
    "port already in use",
)


def is_port_conflict(text: object) -> bool:
    """文本里是否像"端口/地址被占用"（空文本一律 False，不猜）。"""
    value = str(text or "").lower()
    if not value:
        return False
    return any(pattern in value for pattern in PORT_CONFLICT_PATTERNS)

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


def find_free_port(host: str = "127.0.0.1", *, exclude: Collection[int] = ()) -> int:
    """借一个空闲端口（只取号，不占坑）。

    这里存在**无法消除的竞态**：本函数 close 之后到目标进程 bind 之前，别的进程
    可能占掉这个端口（Windows 的 127.0.0.1 还有 hyper-v 保留段）。所以调用方必须
    把"启动失败且像端口冲突"当作可重试事件（见 ``PORT_ATTEMPTS``），而不是
    相信这个端口一定用得上。``exclude`` 用来避开刚失败过的端口。
    """
    blocked = {int(port) for port in exclude}
    port = 0
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            port = int(sock.getsockname()[1])
        if port not in blocked:
            return port
    # 极端情况：连续挑到的都是刚失败过的端口，把最后一个交回去（调用方还会重试）
    return port


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


def _port_open(host: str, port: int) -> bool:
    """单次探测端口是否仍在监听 —— 用来验证"说停了的 PG 是不是真停了"。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


EMBEDDED_STOP_WAIT_S = 15.0


def wait_for_stopped(host: str, port: int, timeout: float = EMBEDDED_STOP_WAIT_S) -> bool:
    """等端口释放（fast shutdown 实测 1~2s 内完成，留足余量）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _port_open(host, port):
            return True
        time.sleep(0.25)
    return not _port_open(host, port)


# 句柄登记表里的 pid 该由谁来持有：引导器自己（打包态是随包 cpython 的 ``python.exe``）。
# 只认前缀，不认全名 —— ``python.exe`` / ``python3.exe`` / ``pythonw.exe`` 都算。
_HANDLE_HOLDER_PREFIX = "python"

#: ``_handle_pid_state`` 的三态
HANDLE_PID_LIVE = "live"
HANDLE_PID_RECYCLED = "recycled"
HANDLE_PID_DEAD = "dead"


def _handle_pid_state(pid: int) -> tuple[str, str]:
    """登记表里这个 pid 现在到底是谁 —— ``("live"|"recycled"|"dead", 进程名)``。

    **为什么"进程还在"不够**：Windows 会复用 PID。真机上取到的 ``.handle_pids.json``
    有 14 条，13 条已退出，剩下唯一"存活"的那条属于一个 ``msedgewebview2.exe`` ——
    它只是捡起了上一个引导器的号。按"还活着"判，就是"还有别人在用这个库"，
    于是 ``cleanup()`` 的判据永远不成立、postmaster 永远不停：M46 要修的泄漏原样复发。

    所以第二道判据是"像不像引导器自己人"（见 ``_HANDLE_HOLDER_PREFIX``）。
    任何判不出来的情况（缺 psutil、权限不足、僵尸进程）一律算 live：
    宁可留着服务器不越权，也不要停掉别人正在连的库。
    """
    if pid <= 0:
        return HANDLE_PID_DEAD, ""
    try:
        import psutil
    except ImportError:
        return HANDLE_PID_LIVE, ""
    try:
        raw = psutil.Process(pid).name() or ""
    except psutil.NoSuchProcess:
        return HANDLE_PID_DEAD, ""
    except (psutil.AccessDenied, psutil.ZombieProcess, OSError, ValueError):
        return HANDLE_PID_LIVE, ""
    if not raw:
        return HANDLE_PID_LIVE, ""
    if raw.lower().startswith(_HANDLE_HOLDER_PREFIX):
        return HANDLE_PID_LIVE, raw
    return HANDLE_PID_RECYCLED, raw


# ``embedded_postgres`` 的跨进程句柄登记表（写在 pgdata 里，见 ``utils.DiskList``）。
EMBEDDED_HANDLE_PIDS_FILE = ".handle_pids.json"


def _read_handle_pids(path: Path) -> list[int]:
    """读登记表；文件不存在/格式不认识都当成"没人登记"（与上游 ``DiskList.get()`` 一致）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, int)]


def _write_handle_pids(path: Path, pids: Sequence[int]) -> None:
    with contextlib.suppress(OSError):
        path.write_text(json.dumps(list(pids)), encoding="utf-8")


def _stop_report(notes: list[str], message: str) -> None:
    """把收尾结论同时写进 ``notes`` 和 stderr。

    收尾发生在父进程已经在退的当口：notes 随就绪事件上报，但那一刻未必还有人读；
    stderr 会被壳侧转进 ``logs/main.log``。"PG 到底停没停"必须留下可见记录 ——
    之前正是这里静默，残留的 postmaster 才被当成"已经停过了"。
    """
    notes.append(f"pg 收尾：{message}")
    with contextlib.suppress(OSError):
        print(f"[pg] {message}", file=sys.stderr, flush=True)


def _embedded_binary_dir(module: Any) -> Path | None:
    """``embedded_postgres`` wheel 自带的那套 PG 二进制目录（``pginstall/bin``）。"""
    try:
        root = Path(str(module.__file__)).parent
    except (AttributeError, TypeError):
        return None
    candidate = root / "pginstall" / "bin"
    return candidate if candidate.is_dir() else None


def _is_pg_ctl_wait_timeout(exc: BaseException) -> bool:
    """这条失败是不是"上游 pg_ctl 等服务器就绪超时"—— 崩溃恢复慢的典型表现。

    ``_call_entry`` 会把原始异常包成 ``RuntimeError("TimeoutExpired: Command '[... pg_ctl ... -w ...]'
    timed out after 10 seconds（调用形态 …）")``，所以只能按文本特征认。两个词都必须在：只认
    ``TimeoutExpired`` 会把我们自己的 SQL 轮询超时也当成"慢"来重试；只认 ``pg_ctl`` 会把权限不足、
    二进制损坏也拖进重试，等于盖住真因。
    """
    text = str(exc)
    return "TimeoutExpired" in text and "pg_ctl" in text


# ``postmaster.pid`` 最后一行的状态取值（PG 16+ 才写这一行）。
_POSTMASTER_STATUS_VALUES = ("starting", "ready")


def _read_postmaster_file(data_dir: Path) -> tuple[int, str]:
    """读 ``postmaster.pid`` 里的 ``(端口, 状态)``；文件不在或读不懂都返回 ``(0, "")``。

    PG 的固定格式：第 1 行 pid、第 2 行 pgdata、第 3 行启动时间戳、**第 4 行端口**、
    之后是 socket 目录/监听地址，**最后一行是状态**（``starting`` → ``ready``）。
    状态只认那两个值：拿不到就按"未就绪"处理，等不到就把真实读数一起上报，不猜。
    """
    try:
        raw = (data_dir / "postmaster.pid").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, ""
    lines = [line.strip() for line in raw.splitlines()]
    port = int(lines[3]) if len(lines) >= 4 and lines[3].isdigit() else 0
    status = next((line for line in reversed(lines) if line in _POSTMASTER_STATUS_VALUES), "")
    return port, status


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
        # 真正监听的 endpoint（从 DSN / get_uri() 读到，不是配置值）
        self._actual_host = ""
        self._actual_port = 0
        # 已经被撞过、或者起步失败过的端口：换端口重试时避开它们
        self._failed_ports: set[int] = set()

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

    @property
    def port(self) -> int:
        """实际监听的 TCP 端口（unix socket 后端为 0）。

        取值以句柄的 ``get_uri()`` / 真实 DSN 为准 —— ``pg_port`` 只是"建议值":
        embedded-pack 自己挑端口，binaries 后端也会在端口冲突后换一个。上报、
        重试与日志都应该看这个值。
        """
        return self._actual_port

    # ------------------------------------------------------------------ 启停
    def start(self) -> str:
        """启动（或连接）PG，返回可用的 DSN。"""
        if self._started and self._dsn:
            return self._dsn

        if self.settings.postgres_dsn:
            self._backend = "external"
            self._dsn = self.settings.postgres_dsn
            self._prepare(self._dsn, strict=False)
            # 外部实例的地址就在 DSN 里：同样把真实 endpoint 记下来
            self._record_endpoint(self._dsn)
            self._started = True
            return self._dsn

        errors: list[str] = []
        self.notes = []
        for name, starter in (
            ("embedded-pack", self._start_embedded_pack),
            ("binaries", self._start_binaries),
        ):
            dsn, backend_errors = self._start_with_port_retry(name, starter)
            errors.extend(backend_errors)
            if dsn is None:
                continue
            self._backend = name
            self._dsn = dsn
            self._started = True
            # 成功时 **不保留** 中途的重试错误：``attempts`` 的语义是"为什么起不来"，
            # 成功场景下把重试记录留在这里会让 /readyz 与日志看起来像"坏了但有 backend"。
            # 重试过程仍然留在 ``notes`` 里，排查时能看到换过端口。
            self.attempts = []
            return dsn

        self.attempts = errors
        raise PostgresUnavailable(errors, self.embedded_probe)

    def _start_with_port_retry(self, name: str, starter: Any) -> tuple[str | None, list[str]]:
        """起一个后端；**只在“像端口冲突”时换端口重试**，其它错误立即交给下一条路。

        换端口靠 ``_pick_port()`` 避开 ``_failed_ports``（binaries 后端）；embedded-pack
        的端口由包内部挑，重试就是重新构造一个服务器句柄。

        返回 ``(dsn, 错误列表)``：成功时 dsn 非空；失败时 dsn 为 None 且错误逐条列出
        （非端口冲突只 1 条，仍保持 ``<后端>: <类型>: <正文>`` 的格式）。
        """
        errors: list[str] = []
        for attempt in range(1, PORT_ATTEMPTS + 1):
            try:
                return starter(), errors
            except Exception as exc:  # noqa: BLE001 —— 逐条记录真实错误，再决定重不重试
                # 先把上一次没收拾干净的进程/句柄收掉，避免它继续占着端口
                self._teardown()
                conflict = is_port_conflict(exc)
                message = f"{name}: {type(exc).__name__}: {_indent(str(exc), '      ').lstrip()}"
                if conflict:
                    message = f"{message}（第 {attempt}/{PORT_ATTEMPTS} 次尝试）"
                errors.append(message)
                if not conflict:
                    break
                self.notes.append(
                    f"{name} 第 {attempt} 次起步失败像端口冲突，已换端口重试（共 {PORT_ATTEMPTS} 次）"
                )
        return None, errors

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
        try:
            hook()
        except Exception as exc:  # noqa: BLE001 —— 收尾异常吞掉就等于谎报"停过了"
            _stop_report(self.notes, f"停止入口抛了：{type(exc).__name__}: {exc}")

    # ------------------------------------------- embedded-pack 收尾：修上游的停止判据
    def _embedded_stop_hook(self, module: Any, stop_hook: Any) -> Any:
        """把 ``handle.cleanup()`` 包一层：先裁死句柄，再验 postmaster 真停了。

        **为什么不能直接信上游**：``PostgresServer._cleanup()`` 的判据是"登记表里只剩
        我这一个 pid"，而那份 ``pgdata/.handle_pids.json``（``utils.DiskList``）**从不清理**
        —— 父进程被杀掉的历次运行会把死 pid 一直留在里面（真机取到 14 条）。判据于是永远
        不成立，``cleanup()`` 一进门就 return，PG 留在后台监听；而 ``pg_ctl`` 起的
        postmaster **不是**引导器的子进程，壳侧的进程树回收碰不到它。真机现象正是
        "引导器退出了、Redis 被收走了、端口 3786 上还挂着 postmaster"。

        这里只裁"确认不再持有句柄"的 pid（已退出，或 PID 已被别的程序复用）：只要还有
        像引导器自己人的活进程，就保留服务器（不越权），也不去 ``pg_ctl stop``；
        判据恢复成立后仍没关掉，才退回官方工具兜底。
        """

        def stop() -> None:
            registry = self.data_dir / EMBEDDED_HANDLE_PIDS_FILE
            mine = os.getpid()
            others = [pid for pid in _read_handle_pids(registry) if pid != mine]
            states = {pid: _handle_pid_state(pid) for pid in others}
            live = [pid for pid, (state, _) in states.items() if state == HANDLE_PID_LIVE]
            dead = [pid for pid, (state, _) in states.items() if state == HANDLE_PID_DEAD]
            hijacked = [
                f"{pid}->{name}"
                for pid, (state, name) in states.items()
                if state == HANDLE_PID_RECYCLED
            ]
            stale = [pid for pid, (state, _) in states.items() if state != HANDLE_PID_LIVE]
            if stale:
                _write_handle_pids(registry, [mine, *live])
                _stop_report(
                    self.notes,
                    f"句柄登记表已裁掉 {len(stale)} 条（已退出 {dead}，PID 被别的进程复用 {hijacked}）："
                    f"留着会让 cleanup() 误判'还有别人在用'而不停服务器",
                )
            stop_hook()
            if live:
                _stop_report(self.notes, f"仍有存活句柄 {live}，保留服务器不越权")
                return
            self._confirm_embedded_stopped(module)

        return stop

    def _confirm_embedded_stopped(self, module: Any) -> None:
        """cleanup() 之后必须**看到端口关掉**才算停；没看到就兜底，兜不住就上报。"""
        if not self._actual_port:
            _stop_report(self.notes, "该后端没有 TCP 端口（unix socket），无法验证服务器已停")
            return
        host = self._actual_host or "127.0.0.1"
        if wait_for_stopped(host, self._actual_port, timeout=EMBEDDED_STOP_WAIT_S):
            _stop_report(self.notes, f"postmaster 已停止（端口 {self._actual_port} 不再监听）")
            return
        _stop_report(self.notes, f"cleanup() 后端口 {self._actual_port} 仍在监听：走 pg_ctl 兜底")
        self._stop_by_pg_ctl(module, host)

    def _stop_by_pg_ctl(self, module: Any, host: str) -> bool:
        pg_ctl: str | None = None
        binary_dir = _embedded_binary_dir(module)
        if binary_dir is not None:
            candidate = binary_dir / ("pg_ctl.exe" if os.name == "nt" else "pg_ctl")
            if candidate.is_file():
                pg_ctl = str(candidate)
        if pg_ctl is None:
            pg_ctl = self._find_binary("pg_ctl")
        if pg_ctl is None:
            _stop_report(self.notes, f"找不到 pg_ctl：端口 {self._actual_port} 上的进程可能未被回收")
            return False
        cmd = [pg_ctl, "-D", str(self.data_dir), "-m", "fast", "-w", "-t", "30", "stop"]
        try:
            # capture=False：postmaster 会继承管道，收输出会一直等 EOF
            self._run(cmd, timeout=60, what="pg_ctl stop（收尾兜底）", check=False, capture=False)
        except Exception as exc:  # noqa: BLE001 —— 兜底失败也要留下结论，不是留个孤儿
            _stop_report(self.notes, f"pg_ctl stop 失败：{type(exc).__name__}: {exc}")
            return False
        if _port_open(host, self._actual_port):
            _stop_report(self.notes, f"pg_ctl 兜底后端口 {self._actual_port} 仍在监听：进程未被回收")
            return False
        _stop_report(self.notes, f"pg_ctl 兜底成功：端口 {self._actual_port} 已释放")
        return True

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

            handle, call_desc = self._call_entry_resilient(entry, entry_name, report)
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
        self._stop_hook = self._embedded_stop_hook(module, stop_hook)

        self.notes.extend(self._embedded_notes(dsn))
        # 端口由包内部挑（POSIX 上干脆只有 unix socket），拿不到 endpoint 就只靠 SQL 轮询
        endpoint = _tcp_endpoint(dsn)
        # get_uri() 回给我们的才是真实端口：记下来（配置的 pg_port 在该后端不生效）
        self._record_endpoint(dsn)
        self._prepare(dsn, strict=True, endpoint=endpoint)
        return dsn

    def _call_entry_resilient(
        self, entry: Any, entry_name: str, report: dict[str, Any]
    ) -> tuple[Any, str]:
        """:meth:`_call_entry` 外面罩一层"崩溃恢复慢"的补救。

        上游 ``embedded_postgres`` 启动用的是 ``pg_ctl(['-w', ...], timeout=10)``，
        那个 10s 是**写死的**，只够一次干净启动。上一次 PG 没正常关（掉电、被强杀、
        或被我们的兜底砍掉）时，postmaster 要先做崩溃恢复 —— 真机实测 33s 才 ready
        （08:39:37 启动 → 08:40:10 可连），10s 到点抛 ``TimeoutExpired``，embedded-pack
        这条路整体判死，引导器报"每条路径都失败"、本地模式起不来。而 PG 其实**还在后台
        继续起**：等它就绪再试一次，上游看到 postmaster 已运行就直接附着，不会再跑 pg_ctl。

        只认这一种特征，别的故障原样上抛 —— 把"权限不够""二进制损坏"也当成"慢"来重试，
        等于把真因盖住。
        """
        try:
            return self._call_entry(entry, entry_name)
        except RuntimeError as exc:
            if not _is_pg_ctl_wait_timeout(exc):
                raise
            waited, detail = self._await_postmaster_ready(timeout=self.settings.pg_start_timeout)
            report["recovery"] = f"pg_ctl 等待超时后仍在恢复：{detail}"
            if not waited:
                raise RuntimeError(
                    f"{exc}\n（等它恢复完也没就绪：{detail}，上限 {self.settings.pg_start_timeout:.0f}s）"
                ) from exc
            self.notes.append(f"上游 pg_ctl 只等 10s，本次恢复更久（{detail}）：就绪后重试一次")
            return self._call_entry(entry, entry_name)

    def _await_postmaster_ready(self, timeout: float) -> tuple[bool, str]:
        """等 postmaster 自己爬完崩溃恢复：pid 文件里状态是 ``ready`` 且端口能连。"""
        deadline = time.monotonic() + timeout
        port, status = 0, ""
        while True:
            port, status = _read_postmaster_file(self.data_dir)
            if port and status == "ready" and _port_open("127.0.0.1", port):
                return True, f"port={port} status={status}"
            if time.monotonic() >= deadline:
                return False, f"port={port or '未知'} status={status or '未知'}"
            time.sleep(0.5)

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
        try:
            self._run(cmd, timeout=30, what="pg_ctl start", capture=False)
            # 端口用本进程下发的值拼 DSN；如果真不行（被占），下面会换端口重试
            dsn = build_dsn(
                self.settings.pg_user,
                self.settings.pg_password,
                "127.0.0.1",
                port,
                self.settings.pg_database,
            )
            self._stop_hook = lambda: self._stop_binaries(pg_ctl)
            self._prepare(dsn, strict=True, endpoint=("127.0.0.1", port))
        except Exception:
            # pg_ctl 可能已经把 postmaster 拉起来了（只是不接受 SQL）：先收掉再抛，
            # 否则它会继续占着这个端口，下一次换端口也白搭
            self._stop_hook = None
            self._stop_binaries(pg_ctl)
            self._failed_ports.add(port)
            raise
        self._record_endpoint(dsn)
        return dsn

    def _stop_binaries(self, pg_ctl: str) -> None:
        cmd = [pg_ctl, "-D", str(self.data_dir), "-m", "fast", "-w", "-t", "30", "stop"]
        self._run(cmd, timeout=60, what="pg_ctl stop", check=False, capture=False)

    def _find_binary(self, name: str) -> str | None:
        """按 ``配置目录 → 随包目录 → PATH`` 找 PG 可执行文件。

        顺序与 :func:`app.storage.redis.find_redis_binary` 一致：显式配置最高，
        其次是安装包自带那份（可脱离用户机环境独立运行），PATH 最后兜底开发者自己装的 PG。
        """
        suffix = ".exe" if os.name == "nt" else ""
        if self.settings.pg_bin_dir:
            candidate = Path(self.settings.pg_bin_dir) / f"{name}{suffix}"
            if candidate.exists():
                return str(candidate)
        bundled = BUNDLED_PG_BIN_DIR / f"{name}{suffix}"
        if bundled.is_file():
            return str(bundled)
        return shutil.which(name)

    # ------------------------------------------------------------------ 工具
    def _pick_port(self) -> int:
        """选一个端口：优先用配置值，但它刚失败过就换成内核分配的空闲端口。

        ``pg_port`` 只是建议值 —— 被占时继续硬用只会一直失败，所以必须换。
        """
        requested = int(self.settings.pg_port or 0)
        if requested and requested not in self._failed_ports:
            return requested
        if requested:
            self.notes.append(f"pg_port={requested} 已被占用或起步失败，改用内核分配的空闲端口（不写死端口）")
        return find_free_port(exclude=self._failed_ports)

    def _record_endpoint(self, dsn: str) -> None:
        """把 DSN 里的真实 endpoint 记下来，作为 ``port`` 属性的唯一来源。

        embedded-pack 自己挑端口、binaries 后端会在冲突后换端口，所以“配置值”与
        “真实值”可能不一致；上报与排查必须看真实值。
        """
        endpoint = _tcp_endpoint(dsn)
        if endpoint is None:
            self._actual_host, self._actual_port = "", 0
            return
        self._actual_host, self._actual_port = endpoint

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
