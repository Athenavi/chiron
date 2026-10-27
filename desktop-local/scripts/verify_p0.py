#!/usr/bin/env python
"""P0 存储验证（契约 §5）：跑五项，逐项给**实测** PASS/FAIL，失败给可执行建议。

五项 =
1. ``embedded-postgres`` 能启动 + 仓库根 ``alembic upgrade head`` 跑通；
2. 数据目录可指定，停止后重启数据仍在；
3. ``fakeredis`` 对 XADD / XREADGROUP / XACK / XCLAIM / XAUTOCLAIM / PUBLISH / SUBSCRIBE
   的逐项支持情况；
4. 冷启动耗时（毫秒）与数据目录体积；
5. 失败时打印可执行的替代方案。

用法::

    cd desktop-local
    python scripts/verify_p0.py                          # 临时数据目录，跑完即删
    python scripts/verify_p0.py --data-dir D:\\chiron-p0  # 指定目录（会复用已有实例）
    python scripts/verify_p0.py --keep                   # 保留临时目录以便排查
    python scripts/verify_p0.py --skip-pg --skip-redis   # 只跑骨架相关项

退出码：0 = 全部 PASS 或跳过；1 = 有 FAIL。
``password`` 之类敏感值一律不打印（DSN 走 :func:`app.storage.pg.mask_dsn`）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # desktop-local/

from app.config import Settings  # noqa: E402
from app.storage.pg import (  # noqa: E402
    PG_SUGGESTIONS,
    PostgresServer,
    run_sql,
)
from app.storage.redis import VALKEY_HINT, RedisServer  # noqa: E402

PROBE_TABLE = "local_p0_probe"
PROBE_MARKER = "p0-restart-probe"
PROBE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {PROBE_TABLE} ("
    "  id bigserial PRIMARY KEY,"
    "  marker text NOT NULL,"
    "  created_at timestamptz NOT NULL DEFAULT now()"
    ")"
)


@dataclass
class Check:
    """一项结论。``ok is None`` 表示跳过（不算失败）。"""

    name: str
    ok: bool | None
    detail: str = ""
    suggestions: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.ok is None:
            return "SKIP"
        return "PASS" if self.ok else "FAIL"


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def dir_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:  # 被占用的文件（如正在写的 WAL）跳过，别把统计搞崩
            continue
    return total


def _print_header(data_dir: Path) -> None:
    print("=" * 78)
    print("Chiron 本地 core — P0 存储验证（docs/desktop-local/contracts.md §5）")
    print(f"数据目录: {data_dir}")
    print("=" * 78)


def _print_check(index: int, total: int, check: Check) -> None:
    print(f"[{index}/{total}] {check.name} ... {check.status}")
    if check.detail:
        for line in check.detail.splitlines():
            print(f"      {line}")


def _pg_stage(settings: Settings, skip: bool) -> tuple[Check, Check, PostgresServer | None, dict]:
    """第 1、2 项：启动 + 迁移，然后停机重启验证持久化。"""
    if skip:
        return (
            Check("embedded PostgreSQL + alembic upgrade head", None, "跳过（--skip-pg）"),
            Check("停止后重启，数据仍在", None, "跳过（--skip-pg）"),
            None,
            {},
        )

    pg = PostgresServer(settings)
    migration: dict = {}

    # --- 第 1 项 -------------------------------------------------------
    started = time.perf_counter()
    try:
        pg.start()
        start_ms = (time.perf_counter() - started) * 1000
        migration = pg.migrate()
    except Exception as exc:  # noqa: BLE001 —— 失败要原样上报，不能吞
        suggestions = list(PG_SUGGESTIONS)
        attempts = getattr(exc, "attempts", None)
        detail = f"{type(exc).__name__}: {exc}"
        if attempts:
            detail += "\n逐条后端失败原因:\n" + "\n".join(f"  - {item}" for item in attempts)
        return (
            Check("embedded PostgreSQL + alembic upgrade head", False, detail, suggestions),
            Check("停止后重启，数据仍在", None, "上一步失败，跳过"),
            None,
            migration,
        )

    first = Check(
        "embedded PostgreSQL + alembic upgrade head",
        True,
        (
            f"backend={pg.backend} dsn={pg.safe_dsn}\n"
            f"进程启动 {start_ms:.0f} ms；迁移 {migration['elapsed_ms']} ms；"
            f"alembic_version={migration['revision']}\n"
            f"迁移脚本目录 {migration['migrations_dir']}"
        ),
    )

    # --- 第 2 项 -------------------------------------------------------
    try:
        run_sql(pg.dsn, PROBE_DDL)
        run_sql(pg.dsn, f"INSERT INTO {PROBE_TABLE} (marker) VALUES (%s)", (PROBE_MARKER,))
        before = int(run_sql(pg.dsn, f"SELECT count(*) FROM {PROBE_TABLE} WHERE marker = %s", (PROBE_MARKER,))[0][0])
        revision_before = pg.current_revision()

        pg.stop()
        pg_wait = time.perf_counter()
        pg.start()  # 同一个 data-dir 再起
        restart_ms = (time.perf_counter() - pg_wait) * 1000

        after = int(run_sql(pg.dsn, f"SELECT count(*) FROM {PROBE_TABLE} WHERE marker = %s", (PROBE_MARKER,))[0][0])
        revision_after = pg.current_revision()
        ok = before > 0 and after >= before and revision_after == revision_before
        second = Check(
            "停止后重启，数据仍在",
            ok,
            (
                f"停机前 {PROBE_MARKER} {before} 行 → 重启后 {after} 行；"
                f"alembic_version {revision_before} → {revision_after}；重启 {restart_ms:.0f} ms"
            ),
            [] if ok else ["重启后行数或迁移版本不一致，检查 CHIRON_LOCAL_DATA_DIR 是否指向同一目录"],
        )
    except Exception as exc:  # noqa: BLE001
        second = Check(
            "停止后重启，数据仍在",
            False,
            f"{type(exc).__name__}: {exc}",
            list(PG_SUGGESTIONS),
        )
    return first, second, pg, migration


async def _redis_stage(redis: RedisServer) -> list:
    await redis.start()
    try:
        return await redis.probe()
    finally:
        await redis.stop()


def _redis_check(settings: Settings, skip: bool) -> tuple[Check, list[str]]:
    """第 3 项：Redis 逐项实测。返回 (结论, 明细行)。"""
    if skip:
        return Check("Redis 命令逐项探测", None, "跳过（--skip-redis）"), []

    redis = RedisServer(settings)
    try:
        results = asyncio.run(_redis_stage(redis))
    except Exception as exc:  # noqa: BLE001
        return (
            Check(
                "Redis 命令逐项探测",
                False,
                f"{type(exc).__name__}: {exc}",
                [VALKEY_HINT],
            ),
            [],
        )

    lines = [f"backend={redis.backend}" + (f"（{redis.warning}）" if redis.warning else "")]
    lines.extend(result.line() for result in results)
    failed = [result.op for result in results if not result.ok]
    ok = not failed
    detail = "\n".join(lines)
    if redis.error:
        detail += f"\n后端报告: {redis.error}"
    suggestions = [] if ok else [f"fakeredis 不支持: {', '.join(failed)}（见下方明细的异常原文）", VALKEY_HINT]
    return Check("Redis 命令逐项探测", ok, detail, suggestions), lines


def _json_summary(checks: list[Check], extras: dict) -> dict:
    return {
        "checks": [
            {"name": check.name, "status": check.status, "detail": check.detail} for check in checks
        ],
        "suggestions": [item for check in checks for item in check.suggestions],
        **extras,
    }


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("CHIRON_P0_STACKS", "") == "1":
        # 诊断用：卡住时每 30s 打印所有线程栈（默认关闭，不影响正常输出）
        import faulthandler

        faulthandler.dump_traceback_later(30, repeat=True)
    parser = argparse.ArgumentParser(description="Chiron 本地 core P0 存储验证")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认建临时目录，跑完删除）")
    parser.add_argument("--keep", action="store_true", help="保留临时数据目录")
    parser.add_argument("--skip-pg", action="store_true", help="跳过 PostgreSQL 两项")
    parser.add_argument("--skip-redis", action="store_true", help="跳过 Redis 逐项探测")
    parser.add_argument("--json", action="store_true", help="额外输出一行 JSON 摘要（给 CI 解析）")
    args = parser.parse_args(argv)

    temporary = args.data_dir is None
    data_dir = Path(args.data_dir) if args.data_dir else Path(tempfile.mkdtemp(prefix="chiron-p0-"))
    settings = Settings(data_dir=data_dir, pg_enabled=not args.skip_pg, redis_enabled=not args.skip_redis)
    settings.ensure_dirs()
    _print_header(data_dir)

    cold_start = time.perf_counter()
    checks: list[Check] = []

    pg_first, pg_second, pg, migration = _pg_stage(settings, args.skip_pg)
    checks.append(pg_first)
    checks.append(pg_second)

    redis_check, redis_lines = _redis_check(settings, args.skip_redis)
    checks.append(redis_check)

    # --- 第 4 项：冷启动耗时 + 数据目录体积 -----------------------------
    total_ms = (time.perf_counter() - cold_start) * 1000
    if pg is not None:
        try:
            pg.stop()
        except Exception as exc:  # noqa: BLE001
            print(f"警告: 停止 PostgreSQL 失败：{type(exc).__name__}: {exc}")
    size = dir_size_bytes(data_dir)
    checks.append(
        Check(
            "冷启动耗时 / 数据目录体积",
            True,
            (
                f"端到端冷启动（起 PG + 迁移 + Redis 探测）{total_ms:.0f} ms；"
                f"数据目录 {data_dir} = {human_bytes(size)}（{size} bytes）"
            ),
        )
    )

    # --- 输出 -----------------------------------------------------------
    print()
    for index, check in enumerate(checks, start=1):
        _print_check(index, len(checks), check)

    failures = [check for check in checks if check.ok is False]
    skipped = [check for check in checks if check.ok is None]
    print("-" * 78)
    print(f"结论: {len(checks) - len(failures) - len(skipped)} 项通过 / {len(failures)} 项失败 / {len(skipped)} 项跳过")
    suggestions: list[str] = []
    for check in failures:
        for item in check.suggestions:
            if item not in suggestions:
                suggestions.append(item)
    if suggestions:
        print()
        print("可执行建议（契约 §5.5）：")
        for item in suggestions:
            print(f"  - {item}")
    print("-" * 78)

    if temporary and not args.keep:
        shutil.rmtree(data_dir, ignore_errors=True)
        print(f"临时数据目录已删除（--keep 可保留）：{data_dir}")
    elif temporary:
        print(f"临时数据目录已保留：{data_dir}")

    if args.json:
        extras = {
            "data_dir": str(data_dir),
            "cold_start_total_ms": round(total_ms, 1),
            "data_dir_bytes": size,
            "migration": migration,
            "redis": redis_lines,
        }
        print("JSON " + json.dumps(_json_summary(checks, extras), ensure_ascii=False))

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
