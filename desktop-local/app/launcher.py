"""本地数据栈引导器：只负责把 PG / Redis 拉起来并把连接信息交给 Go 网关。

**为什么要有这个进程**（架构决策，见 docs/desktop-local/launcher.md）：

本地模式的目标是"与云端几乎无差"，而云端全部业务路由都在 Go 网关里（``cmd/chiron``）。
过去 desktop-local 自己实现了一份 14 组端点的"骨架后端"，与 Go 网关是两套并存的
实现 —— 覆盖面天然对不齐，前端每加一个页面，本地就多一处 404。

现在 desktop-local 收窄成**数据栈引导器**：

    Electron ──spawn──> chiron-launcher（本文件）──> embedded PG + bundled Redis
                            │
                            └─ stdout 一行 JSON：{"event":"launcher-ready", pg_dsn, redis_addr, ...}
                                    │
    Electron ──拿到 DSN/端口──> spawn ──> chiron.exe（Go 网关，唯一业务入口）

Go 网关本身**没有**拉起 PG/Redis 的能力（``FATAL: PostgreSQL is required but
unavailable``），而 embedded PG 的打包、initdb、端口挑选这些脏活在 Python 侧已经
做实了（``app/storage/pg.py``）。所以让引导器做"脏活"，让网关做"业务"，两边各守一件事。

三个刻意的设计：

1. **不继承 FastAPI**。引导器只跑 asyncio + 两个存储类，不起 HTTP 服务。它没有端口，
   没有路由，因此也没有"又implement了一半"的空间。
2. **连接信息走 stdout，不落盘**。DSN 里有密码（embedded 固定 ``postgres`` + trust 认证，
   只绑 loopback），既然父进程就在管道另一头，就没必要把它写到磁盘上多一份副本。
   ``--state-file`` 只是给"手动排查"留的显式出口，默认关闭。
3. **就绪即宣告，不等业务**。PG 起来 + 迁移跑完 + Redis 起来，就打印 ``launcher-ready``；
   网关能否起来是下一步的事，混在一起会让故障归因变难。

失败一律**非零退出**：网关硬依赖 PG 与 Redis，装作起来了只会把错误推迟到更难看懂的地方。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app import __version__
from app.config import Settings
from app.storage.pg import PostgresServer
from app.storage.redis import RedisServer

# 就绪事件名。Electron 主进程按行解析 stdout，与既有的 ``listening`` 事件同一套约定。
READY_EVENT = "launcher-ready"
# 出错事件名：与就绪事件成对，让父进程不必只看退出码就能拿到原因。
ERROR_EVENT = "launcher-error"


def _emit(payload: dict[str, Any]) -> None:
    """往 stdout 打一行 JSON。**必须 flush** —— 父进程按行阻塞读取。"""
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="chiron-launcher",
        description="Chiron 本地数据栈引导器（embedded PostgreSQL + Redis），就绪后输出连接信息",
    )
    parser.add_argument("--data-dir", default=None, help="数据目录（默认按平台选择）")
    parser.add_argument("--no-pg", action="store_true", help="不启动 embedded PostgreSQL")
    parser.add_argument("--no-redis", action="store_true", help="不启动 Redis")
    parser.add_argument("--no-migrate", action="store_true", help="不跑 alembic upgrade head")
    parser.add_argument(
        "--state-file",
        default=None,
        help="把就绪信息另写一份到该文件（默认不写：连接信息里有密码，只走 stdout）",
    )
    parser.add_argument(
        "--print-and-exit",
        action="store_true",
        help="打印就绪信息后立刻退出并停掉 PG/Redis（只用于自检，不能给网关用）",
    )
    return parser.parse_args(argv)


def settings_from_args(args: argparse.Namespace) -> Settings:
    overrides: dict[str, Any] = {}
    if args.data_dir:
        overrides["data_dir"] = args.data_dir
    if args.no_pg:
        overrides["pg_enabled"] = False
    if args.no_redis:
        overrides["redis_enabled"] = False
    if args.no_migrate:
        overrides["run_migrations"] = False
    return Settings(**overrides)


def build_ready_payload(
    settings: Settings,
    pg: PostgresServer,
    redis: RedisServer,
    startup_ms: float,
    *,
    migration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组装交给网关的连接信息。

    ``pg_dsn`` 是**真实** DSN（含密码）—— 网关没有它就起不来，不能打码。
    这里的安全边界是：值只出现在 stdout 管道与网关进程的内存里，不落盘、不写日志。
    """
    redis_addr = f"127.0.0.1:{redis.port}" if redis.port else ""
    return {
        "event": READY_EVENT,
        "pid": os.getpid(),
        "version": __version__,
        "data_dir": str(settings.data_dir),
        "startup_ms": round(startup_ms, 1),
        # --- 交给 Go 网关的两个值（对应 POSTGRES_DSN / REDIS_ADDR）---
        "pg_dsn": pg.dsn,
        "redis_addr": redis_addr,
        # --- 诊断用，不含密钥 ---
        "pg_backend": pg.backend,
        "pg_port": pg.port,
        "pg_safe_dsn": pg.safe_dsn,
        "redis_backend": redis.backend,
        "redis_port": redis.port,
        "redis_lua": redis.lua_supported,
        "migration": migration,
    }


async def bootstrap(settings: Settings) -> tuple[PostgresServer, RedisServer, dict[str, Any]]:
    """起 PG（含迁移）与 Redis，返回就绪载荷。任一步失败直接抛出。"""
    settings.ensure_dirs()
    started = time.perf_counter()

    pg = PostgresServer(settings)
    redis = RedisServer(settings)
    migration: dict[str, Any] | None = None

    if settings.pg_enabled:
        # pg_ctl / embedded 包都是阻塞调用，扔到线程里免得卡住事件循环
        await asyncio.to_thread(pg.start)
        if settings.run_migrations:
            migration = await asyncio.to_thread(pg.migrate, pg.dsn)

    if settings.redis_enabled:
        await redis.start()

    payload = build_ready_payload(
        settings, pg, redis, (time.perf_counter() - started) * 1000, migration=migration
    )
    return pg, redis, payload


def _write_state_file(path: str, payload: dict[str, Any]) -> None:
    """显式要求时才落盘。权限收紧到仅本用户可读写（POSIX 上 ``0600``）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with contextlib.suppress(OSError):
        target.chmod(0o600)


async def run(settings: Settings, *, state_file: str | None, print_and_exit: bool) -> int:
    pg, redis, payload = await bootstrap(settings)

    try:
        _emit(payload)
        if state_file:
            _write_state_file(state_file, payload)

        if print_and_exit:
            return 0

        # 常驻：直到父进程关闭 stdin（Electron 侧控制）或收到信号。
        # 用"等 stdin EOF + 等信号"两条路径，是因为 Windows 上没有可靠的 SIGTERM，
        # 而 stdin 管道在父进程退出时一定会断 —— 这条路径跨平台都成立。
        stop = asyncio.Event()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, AttributeError, ValueError):
                loop.add_signal_handler(sig, stop.set)

        if not sys.stdin.isatty():
            task = asyncio.create_task(_watch_stdin_eof())
            task.add_done_callback(lambda _t: stop.set())

        await stop.wait()
        return 0
    finally:
        # 停的顺序与启动相反：先 Redis，再 PG。失败也不该掩盖上面的异常。
        with contextlib.suppress(Exception):
            await redis.stop()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(pg.stop)


async def _watch_stdin_eof() -> None:
    """读 stdin 直到 EOF。父进程退出 ⇒ 管道关闭 ⇒ 这里返回 ⇒ 引导器跟着退出。

    这样就不会留下"父进程没了但 PG/Redis 还在"的孤儿 —— 那会导致下次启动
    ``initdb`` 撞上残留 postmaster。
    """
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    with contextlib.suppress(Exception):
        await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
    with contextlib.suppress(Exception):
        while await reader.read(4096):
            pass


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = settings_from_args(args)
    try:
        return asyncio.run(run(settings, state_file=args.state_file, print_and_exit=args.print_and_exit))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 —— 顶层兜底：把原因打成事件，再以非零退出
        _emit({"event": ERROR_EVENT, "error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
