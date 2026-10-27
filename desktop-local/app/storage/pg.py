"""embedded PostgreSQL 生命周期 + 迁移执行。

后端按固定顺序尝试，**每个后端都要真的能接受 SQL 才被接受**（只起来不算成功）：

1. ``external``       —— 显式给了 ``POSTGRES_DSN``（用户自己的 PG / Docker / 共享实例），
   本进程只管连，不管进程。
2. ``embedded-pack``  —— pyproject 声明的 ``embedded_postgres`` 包。该包的公开入口在不同
   版本里形态不一，因此这里做**特征探测**（列出真实属性、逐个入口试调用），失败原因逐条记录，
   不假装支持。
3. ``binaries``       —— ``PATH``（或 ``CHIRON_LOCAL_PG_BIN_DIR``）里的 ``initdb`` / ``pg_ctl``，
   行为完全由 PostgreSQL 官方工具保证，是 Windows 上最可靠的一条路。

三条都不通 → :class:`PostgresUnavailable`，异常里带**每条路的真实错误**与可执行建议。

迁移复用仓库根的 ``migrations/``（只读），通过 ``alembic upgrade head`` 执行。
"""

from __future__ import annotations

import contextlib
import importlib
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

# embedded_postgres 包在各版本里用过的公开入口名（只做特征探测，命中哪个用哪个）
_EMBEDDED_ENTRYPOINTS = (
    "EmbeddedPostgres",
    "PostgresServer",
    "Postgres",
    "PgServer",
    "Server",
    "start_postgres",
    "create_server",
    "get_server",
)

# 库名会用字面量插进 CREATE DATABASE，必须是可校验的标识符
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

PG_SUGGESTIONS = (
    "给一个现成实例：set POSTGRES_DSN=postgresql://user:pwd@127.0.0.1:5432/chiron",
    "装 PostgreSQL 16+ 并指向其 bin 目录：set CHIRON_LOCAL_PG_BIN_DIR=C:\\Program Files\\PostgreSQL\\16\\bin",
    "用容器（CI/本机均可）：docker run --rm -e POSTGRES_PASSWORD=chiron -p 55432:5432 postgres:16",
    "只想先跑通 Redis/HTTP 骨架：CHIRON_LOCAL_PG_ENABLED=false（/readyz 会报 pg=false）",
)


class PostgresUnavailable(RuntimeError):  # noqa: N818 —— 名字表达“不可用”而非“错误”
    """所有 PG 后端都不可用。``attempts`` 里是逐条真实错误。"""

    def __init__(self, attempts: Sequence[str]) -> None:
        self.attempts = list(attempts)
        lines = ["embedded PostgreSQL 不可用，以下每条路都失败了："]
        lines.extend(f"  - {item}" for item in self.attempts)
        lines.append("可执行建议：")
        lines.extend(f"  - {item}" for item in PG_SUGGESTIONS)
        super().__init__("\n".join(lines))


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
    """把 DSN 的库名换掉（用于连 ``postgres`` 维护库建库；保留 query 参数）。"""
    head, _, tail = dsn.rpartition("/")
    _name, _, query = tail.partition("?")
    suffix = f"?{query}" if query else ""
    return f"{head}/{quote(database, safe='')}{suffix}"


def database_of(dsn: str) -> str:
    """从 DSN 取库名（去掉 ``?sslmode=...`` 这类 query）。"""
    return dsn.rpartition("/")[2].partition("?")[0]


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
        for name, starter in (
            ("embedded-pack", self._start_embedded_pack),
            ("binaries", self._start_binaries),
        ):
            try:
                dsn = starter()
            except Exception as exc:  # noqa: BLE001 —— 逐条记录真实错误，再试下一条
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                self._teardown()
                continue
            self._backend = name
            self._dsn = dsn
            self._started = True
            self.attempts = errors
            return dsn

        self.attempts = errors
        raise PostgresUnavailable(errors)

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
        module = importlib.import_module("embedded_postgres")
        entry_name = next(
            (name for name in _EMBEDDED_ENTRYPOINTS if callable(getattr(module, name, None))),
            None,
        )
        if entry_name is None:
            exposed = ", ".join(sorted(n for n in dir(module) if not n.startswith("_"))[:40])
            raise RuntimeError(f"embedded_postgres 未暴露可识别的启动入口；实测 dir() = [{exposed}]")

        entry = getattr(module, entry_name)
        port = self._pick_port()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        handle = self._call_entry(entry, entry_name, port)

        stop_hook = getattr(handle, "stop", None) or getattr(handle, "close", None)
        if callable(stop_hook):
            self._stop_hook = stop_hook
        elif hasattr(handle, "__exit__"):
            handle.__enter__()
            self._stop_hook = lambda: handle.__exit__(None, None, None)

        start_hook = getattr(handle, "start", None)
        if callable(start_hook):
            start_hook()

        dsn = build_dsn(
            self.settings.pg_user, self.settings.pg_password, "127.0.0.1", port, self.settings.pg_database
        )
        self._prepare(dsn, strict=True, port=port)
        return dsn

    def _call_entry(self, entry: Any, entry_name: str, port: int) -> Any:
        """按「参数从多到少」试调用入口函数，只把结果当句柄，不假设返回类型。"""
        full = {
            "data_dir": str(self.data_dir),
            "port": port,
            "user": self.settings.pg_user,
            "username": self.settings.pg_user,
            "password": self.settings.pg_password,
            "database": self.settings.pg_database,
            "dbname": self.settings.pg_database,
        }
        variants = [
            full,
            {k: v for k, v in full.items() if k not in {"username", "database", "dbname"}},
            {"data_dir": str(self.data_dir), "port": port},
            {"port": port},
            {},
        ]
        errors: list[str] = []
        for kwargs in variants:
            try:
                return entry(**kwargs)
            except TypeError as exc:
                errors.append(f"{entry_name}({sorted(kwargs)}) → TypeError: {exc}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{entry_name}({sorted(kwargs)}) → {type(exc).__name__}: {exc}")
        raise RuntimeError("embedded_postgres 入口调用全部失败：\n    " + "\n    ".join(errors))

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
        self._prepare(dsn, strict=True, port=port)
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

    def _prepare(self, dsn: str, strict: bool, port: int | None = None) -> None:
        """等端口 → 等 SQL → 建库。只把**真能执行 SQL** 的实例算启动成功。"""
        if port is not None and not wait_for_port("127.0.0.1", port, timeout=self.settings.pg_start_timeout):
            raise RuntimeError(f"等待 127.0.0.1:{port} 可连接超时（见 {self.settings.pg_log_file}）")

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
