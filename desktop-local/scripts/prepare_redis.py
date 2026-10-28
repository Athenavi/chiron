#!/usr/bin/env python
"""把 ``redis-server`` 二进制定位/拷贝到 ``desktop-local/bin/redis/``，并实测它能真的解释 Lua。

**为什么需要这一步**：``app/storage/redis.py`` 的后端顺序是
``外部 URL → 内置二进制 → fakeredis``。第二档要求 ``bin/redis/redis-server`` 存在，
而本仓库 ``.gitignore`` 里 ``*.exe`` 是忽略的 —— 二进制**不入库**（合理：几十 MB，
且各平台不同），所以必须有一个可复现的"就位 + 自检"动作，否则打包机上这一档会静默
落空、退回 fakeredis，然后引擎的 Lua 脚本在运行时才炸。

**为什么要连带拷 DLL**：Windows 上发行版 ``redis-server.exe`` 多是 MSYS2/MINGW 构建，
自身不静态链接运行库。实测把 exe 单独拷走、清空 PATH 后无法启动；同目录带上
``*.dll``（其中 ``msys-*.dll`` 是运行时）才能脱离 scoop/安装目录独立运行。

用法::

    # 从 PATH 找到的 redis-server 拷进 bin/redis/ 并自检
    python desktop-local/scripts/prepare_redis.py

    # 指定来源目录（某个已下载/已安装的 redis 目录）
    python desktop-local/scripts/prepare_redis.py --from D:/scoop/apps/redis/8.6.2

    # 只自检，不拷贝（CI 里验证"当前环境这一档是否可用"）
    python desktop-local/scripts/prepare_redis.py --check

    # 删掉已就位的二进制
    python desktop-local/scripts/prepare_redis.py --clean

自检内容（与 ``RedisServer.probe()`` 同一条路径）：起一个**独立端口**的实例、
跑 8 条契约命令（含 EVAL），然后停掉并确认端口已释放。任何一条 FAIL 都以非零退出。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

DESKTOP_LOCAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DESKTOP_LOCAL))

from app.config import Settings  # noqa: E402
from app.storage.redis import (  # noqa: E402
    BUNDLED_REDIS_DIR,
    RedisServer,
    find_redis_binary,
)

EXE_NAME = "redis-server.exe" if sys.platform == "win32" else "redis-server"
# 需要一起带走的同目录文件：DLL（含 MSYS2 运行时）与随发行版附带的默认配置。
EXTRA_GLOBS = ("*.dll", "*.conf")


def _resolve_real_binary(exe: Path) -> Path:
    """把 scoop/ShimGen 的 shim 解析成真身；普通二进制原样返回。

    **为什么必须做**：scoop 在 ``shims/`` 下放的是一个 136KB 的转发器
    ``redis-server.exe``，外加同名 ``redis-server.shim`` 文本记录真身路径。只把这个
    exe 拷进 ``bin/redis/`` 后执行会报 ``Cannot open shim file for read.`` —— 它依赖
    同目录的 .shim 元数据。实测自检因此掉到 ``backend=unavailable``，安装包里就带上了
    一个**永远起不来的 redis**，用户侧表现为核心功能静默降级。
    """
    if sys.platform != "win32":
        return exe
    meta = exe.with_suffix(".shim")
    if not meta.is_file():
        return exe
    for line in meta.read_text(encoding="utf-8", errors="ignore").splitlines():
        key, sep, value = line.partition("=")
        if not sep or key.strip().lower() != "path":
            continue
        target = Path(value.strip().strip('"'))
        if target.is_file():
            return target
    raise SystemExit(
        f"{exe} 是 shim 但无法从 {meta} 解析出真身：请用 --from 直接指定 redis 安装目录"
    )


def _source_dir(explicit: str) -> Path:
    """确定源目录：显式 ``--from`` 优先，否则用 PATH/内置目录里已找到的那份。"""
    if explicit:
        path = Path(explicit)
        if not path.is_dir():
            raise SystemExit(f"--from 不是目录：{path}")
        if not (path / EXE_NAME).is_file():
            raise SystemExit(f"{path} 里没有 {EXE_NAME}")
        return path

    # 先看 PATH：内置目录里若已经躺着上一轮的坏 shim，不能拿它当来源（否则永远修不好）
    found: Path | None = None
    which = shutil.which(EXE_NAME)
    if which:
        found = Path(which)
    if found is None:
        candidate = find_redis_binary(Settings(redis_enabled=True))
        if candidate is not None and candidate.parent.resolve() != BUNDLED_REDIS_DIR.resolve():
            found = candidate
    if found is None:
        raise SystemExit(
            f"找不到 {EXE_NAME}：请用 --from 指定 redis 发行版目录，或先安装（brew/scoop/apt）"
        )
    return _resolve_real_binary(found).parent


def install(src: Path, dest: Path) -> list[Path]:
    """把 exe 与同目录的 DLL/conf 拷到 ``dest``，返回拷贝清单。"""
    if src.resolve() == dest.resolve():
        return []
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = [(shutil.copy2(src / EXE_NAME, dest / EXE_NAME))]
    for pattern in EXTRA_GLOBS:
        for item in sorted(src.glob(pattern)):
            if item.is_file():
                copied.append(shutil.copy2(item, dest / item.name))
    return copied


async def self_check(verbose: bool = True) -> int:
    """真起一次内置实例并跑全量探测；返回退出码。"""
    data_dir = Path(tempfile.mkdtemp(prefix="chiron-redis-check-"))
    settings = Settings(pg_enabled=False, redis_enabled=True, data_dir=data_dir)
    binary = find_redis_binary(settings)
    if binary is None:
        print(f"FAIL 找不到 {EXE_NAME}（--check 不会拷贝）", file=sys.stderr)
        return 2

    print(f"binary = {binary}")
    server = RedisServer(settings)
    await server.start()
    try:
        print(f"backend = {server.backend}")
        if server.backend != "bundled":
            print(f"FAIL 期望 backend=bundled，实际 {server.backend}：{server.error}", file=sys.stderr)
            return 1
        results = await server.probe()
        for result in results:
            if verbose:
                print(result.line())
    finally:
        await server.stop()

    failed = [item for item in results if not item.ok]
    if failed:
        print(f"FAIL {len(failed)}/{len(results)} 条命令不支持", file=sys.stderr)
        return 1

    # 停止后端口必须释放：否则下次启动会撞端口，且日志里看不出谁占的
    import socket

    if server._port:
        with socket.socket() as sock:
            sock.settimeout(1.0)
            if sock.connect_ex(("127.0.0.1", server._port)) == 0:
                print(f"FAIL stop 后 127.0.0.1:{server._port} 仍在监听", file=sys.stderr)
                return 1

    print(f"\nOK {len(results)}/{len(results)} 条命令 PASS，Lua 可用，停止后端口已释放")
    return 0


def _force_utf8_stdio() -> None:
    """把 stdout/stderr 固定成 UTF-8，不依赖宿主 locale。

    **为什么必须做**：windows-latest 跑的是 en-US 镜像，stdout 默认 cp1252，
    而本脚本的输出含中文。cp1252 编不出来就抛 UnicodeEncodeError，
    构建会在打印进度时崩掉 —— 与构建逻辑无关，纯粹是显示层。

    用 ``getattr`` 而不是直接调：pytest 的捕获对象没有 ``reconfigure``。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="source", default="", help="redis 发行版目录（含 redis-server）")
    parser.add_argument("--check", action="store_true", help="只自检，不拷贝")
    parser.add_argument("--clean", action="store_true", help="删掉 bin/redis/ 下已就位的二进制")
    args = parser.parse_args(argv)

    if args.clean:
        if not BUNDLED_REDIS_DIR.is_dir():
            print(f"无需清理：{BUNDLED_REDIS_DIR} 不存在")
            return 0
        removed = 0
        for item in BUNDLED_REDIS_DIR.iterdir():
            if item.is_file() and (item.suffix.lower() in {".exe", ".dll", ".conf"} or item.name == EXE_NAME):
                item.unlink()
                removed += 1
        print(f"已删除 {removed} 个文件（{BUNDLED_REDIS_DIR}）")
        return 0

    if not args.check:
        src = _source_dir(args.source)
        copied = install(src, BUNDLED_REDIS_DIR)
        if copied:
            print(f"已从 {src} 拷贝 {len(copied)} 个文件到 {BUNDLED_REDIS_DIR}：")
            for item in copied:
                print(f"  {item.name}")
        else:
            print(f"{BUNDLED_REDIS_DIR} 已是来源目录，跳过拷贝")

    return asyncio.run(self_check())


if __name__ == "__main__":
    raise SystemExit(main())
