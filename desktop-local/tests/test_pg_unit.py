"""PG 工具函数的纯逻辑测试（不需要任何数据库）。"""

from __future__ import annotations

import pytest
from app.config import Settings
from app.storage import pg as pg_module
from app.storage.pg import PostgresServer, build_dsn, find_free_port, mask_dsn, replace_database


def test_mask_dsn_hides_password() -> None:
    masked = mask_dsn("postgresql://chiron:s3cret@127.0.0.1:55432/chiron")
    assert masked == "postgresql://chiron:***@127.0.0.1:55432/chiron"
    assert "s3cret" not in masked


def test_mask_dsn_without_credentials_is_untouched() -> None:
    assert mask_dsn("postgresql://127.0.0.1:5432/chiron") == "postgresql://127.0.0.1:5432/chiron"
    assert mask_dsn("") == ""


def test_mask_dsn_handles_at_sign_in_password() -> None:
    masked = mask_dsn("postgresql://u:p@ss@127.0.0.1:5432/db")
    assert masked == "postgresql://u:***@127.0.0.1:5432/db"
    assert "p@ss" not in masked


def test_build_dsn_quotes_special_characters() -> None:
    dsn = build_dsn("user", "p@ss:word", "127.0.0.1", 5432, "chiron")
    assert dsn.startswith("postgresql://user:p%40ss%3Aword@127.0.0.1:5432/chiron")


def test_replace_database_swaps_only_database() -> None:
    assert replace_database("postgresql://u:p@h:5432/chiron", "postgres") == "postgresql://u:p@h:5432/postgres"


def test_find_free_port_returns_usable_port() -> None:
    port = find_free_port()
    assert 1024 < port <= 65535


def test_pg_data_dir_is_under_data_dir(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "d", pg_enabled=False)
    assert settings.pg_data_dir == (tmp_path / "d").resolve() / "pg"


def test_out_of_range_port_is_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(port=70000)


# ---------------------------------------------------------------- 二进制查找顺序
#
# 这三条钉的是 `_find_binary` 的优先级：显式配置 → 随包目录 → PATH。
# 顺序错了不会报错，只会让安装包自带的那份被悄悄绕过 —— 用户机上没装 PG 时
# 表现就是"数据库起不来"，而真正的原因（用了错的那份/没找到）在日志里看不出来。

def _server(tmp_path, **overrides) -> PostgresServer:
    settings = Settings(data_dir=tmp_path / "d", pg_enabled=False, **overrides)
    return PostgresServer(settings)


def _suffix() -> str:
    import os

    return ".exe" if os.name == "nt" else ""


def test_find_binary_prefers_explicit_bin_dir(tmp_path, monkeypatch) -> None:
    explicit = tmp_path / "explicit"
    bundled = tmp_path / "bundled"
    for directory in (explicit, bundled):
        directory.mkdir()
        (directory / f"initdb{_suffix()}").write_text("", encoding="utf-8")
    monkeypatch.setattr(pg_module, "BUNDLED_PG_BIN_DIR", bundled)

    found = _server(tmp_path, pg_bin_dir=str(explicit))._find_binary("initdb")

    assert found == str(explicit / f"initdb{_suffix()}")


def test_find_binary_falls_back_to_bundled_dir(tmp_path, monkeypatch) -> None:
    """配置为空时用随包那份 —— 这是安装包用户唯一可用的来源。"""
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / f"initdb{_suffix()}").write_text("", encoding="utf-8")
    monkeypatch.setattr(pg_module, "BUNDLED_PG_BIN_DIR", bundled)
    # PATH 上也放一份，确认随机性不会盖过随包那份
    monkeypatch.setattr(pg_module.shutil, "which", lambda name: "/should/not/be/used")

    found = _server(tmp_path)._find_binary("initdb")

    assert found == str(bundled / f"initdb{_suffix()}")


def test_find_binary_returns_none_when_nothing_available(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pg_module, "BUNDLED_PG_BIN_DIR", tmp_path / "missing")
    monkeypatch.setattr(pg_module.shutil, "which", lambda name: None)

    assert _server(tmp_path)._find_binary("initdb") is None
