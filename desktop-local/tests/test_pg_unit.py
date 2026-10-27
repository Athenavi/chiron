"""PG 工具函数的纯逻辑测试（不需要任何数据库）。"""

from __future__ import annotations

import pytest
from app.config import Settings
from app.storage.pg import build_dsn, find_free_port, mask_dsn, replace_database


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
