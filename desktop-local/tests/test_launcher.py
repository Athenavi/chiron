"""本地数据栈引导器（``app/launcher.py``）的契约测试。

引导器是 Electron → PG/Redis → Go 网关这条链上的**交接点**，它只有两种出错方式，
两种都会把故障推迟到更难归因的地方，所以两边都要钉住：

1. **撒谎**：PG/Redis 没起来却打印 ``launcher-ready``。Electron 会拿着空 DSN 去 spawn
   网关，网关报 ``FATAL: PostgreSQL is required`` —— 用户看到的是"网关坏了"，
   真正的原因（数据栈没起来）被埋掉。
2. **沉默**：起来了却不给连接信息，或把 DSN 打码。网关拿不到可用 DSN 同样起不来。

这里只测**纯逻辑**（参数解析 / 载荷组装 / 落盘），不碰真实存储 ——
真实 PG/Redis 的启停由 ``scripts/verify_p0.py`` 与 ``--print-and-exit`` 自检覆盖，
在单测里再起一遍会慢十几秒且容易在 CI 上撞端口。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import Settings
from app.launcher import (
    ERROR_EVENT,
    READY_EVENT,
    _parse_args,
    _write_state_file,
    build_ready_payload,
    settings_from_args,
)


class _FakePg:
    """最小替身：只暴露载荷组装用到的四个属性。"""

    def __init__(self, dsn: str, port: int, backend: str = "embedded-pack") -> None:
        self.dsn = dsn
        self.port = port
        self.backend = backend

    @property
    def safe_dsn(self) -> str:
        return f"postgresql://postgres:***@127.0.0.1:{self.port}/chiron"


class _FakeRedis:
    def __init__(self, port: int, backend: str = "bundled", lua: bool | None = True) -> None:
        self.port = port
        self.backend = backend
        self.lua_supported = lua


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    defaults: dict[str, Any] = {"data_dir": tmp_path / "data", "pg_enabled": False, "redis_enabled": False}
    defaults.update(overrides)
    return Settings(**defaults)


def test_event_names_are_stable() -> None:
    """事件名是 Electron 主进程按行解析的契约，改名等于改协议。"""
    assert READY_EVENT == "launcher-ready"
    assert ERROR_EVENT == "launcher-error"


def test_args_default_to_storage_enabled() -> None:
    """默认要起 PG 和 Redis —— 网关对两者都是硬依赖，默认关掉没有任何意义。"""
    args = _parse_args([])
    assert args.no_pg is False
    assert args.no_redis is False
    assert args.no_migrate is False
    # 默认不落盘：DSN 里有连接信息，只该走 stdout
    assert args.state_file is None
    assert args.print_and_exit is False


def test_no_flags_map_to_settings(tmp_path: Path) -> None:
    args = _parse_args(
        ["--data-dir", str(tmp_path / "custom"), "--no-pg", "--no-redis", "--no-migrate"]
    )
    settings = settings_from_args(args)

    assert settings.data_dir == (tmp_path / "custom").resolve()
    assert settings.pg_enabled is False
    assert settings.redis_enabled is False
    assert settings.run_migrations is False


def test_ready_payload_carries_real_dsn_and_redis_addr(tmp_path: Path) -> None:
    """载荷里必须有网关真正能用的 DSN 与 Redis 地址，且是**未打码**的。"""
    settings = _settings(tmp_path)
    pg = _FakePg("postgresql://postgres:@127.0.0.1:50165/chiron", 50165)
    redis = _FakeRedis(50404)

    payload = build_ready_payload(settings, pg, redis, 1234.56, migration={"ok": True, "revision": "abc"})

    assert payload["event"] == READY_EVENT
    assert payload["pg_dsn"] == "postgresql://postgres:@127.0.0.1:50165/chiron"
    assert payload["redis_addr"] == "127.0.0.1:50404"
    assert payload["startup_ms"] == 1234.6
    assert payload["migration"] == {"ok": True, "revision": "abc"}
    # 诊断字段同时在：排查时不该被迫去猜后端
    assert payload["pg_backend"] == "embedded-pack"
    assert payload["redis_backend"] == "bundled"


def test_ready_payload_masks_in_diagnostic_field_only(tmp_path: Path) -> None:
    """打码只出现在 ``pg_safe_dsn``；``pg_dsn`` 一旦被打码，网关就起不来。"""
    settings = _settings(tmp_path)
    pg = _FakePg("postgresql://postgres:@127.0.0.1:50165/chiron", 50165)

    payload = build_ready_payload(settings, pg, _FakeRedis(50404), 1.0)

    assert "***" in payload["pg_safe_dsn"]
    assert "***" not in payload["pg_dsn"]


def test_ready_payload_without_redis_reports_empty_addr(tmp_path: Path) -> None:
    """Redis 没起（端口 0）时给空串，而不是 ``127.0.0.1:0`` —— 后者会被当成有效地址。"""
    settings = _settings(tmp_path, redis_enabled=False)
    payload = build_ready_payload(settings, _FakePg("postgresql://x", 1), _FakeRedis(0, backend="none"), 1.0)

    assert payload["redis_addr"] == ""
    assert payload["redis_port"] == 0


def test_state_file_is_opt_in_and_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "launcher-state.json"
    payload = {"event": READY_EVENT, "pg_dsn": "postgresql://postgres:@127.0.0.1:1/chiron"}

    _write_state_file(str(target), payload)

    assert json.loads(target.read_text(encoding="utf-8")) == payload
