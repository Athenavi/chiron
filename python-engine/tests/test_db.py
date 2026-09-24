# app/db.py::ensure_tables 测试 —— 只读校验。
#
# DDL 的唯一权威是 Alembic（migrations/versions/0001_authoritative_baseline.py）：
# 应用侧只做存在性校验，不建表、不建扩展。本文件因此断言两件事：
#   1. 缺表能被检出（返回 False）；
#   2. 无论检出结果如何，都**没有执行任何 DDL**。
from unittest.mock import AsyncMock, patch

from app.db import REQUIRED_TABLES


async def test_ensure_tables_reports_missing_and_never_runs_ddl():
    """缺表时返回 False，且不得执行 DDL（建表是迁移的职责）。"""
    from app.db import ensure_tables

    pool = AsyncMock()
    pool.fetch.return_value = [{"table_name": "users"}]
    with patch("app.db.get_pool", return_value=pool):
        ok = await ensure_tables()

    assert ok is False
    pool.fetch.assert_awaited_once()
    pool.execute.assert_not_awaited()


async def test_ensure_tables_true_when_all_required_present():
    """REQUIRED_TABLES 齐全时返回 True，同样保持只读。"""
    from app.db import ensure_tables

    pool = AsyncMock()
    pool.fetch.return_value = [{"table_name": t} for t in REQUIRED_TABLES]
    with patch("app.db.get_pool", return_value=pool):
        ok = await ensure_tables()

    assert ok is True
    pool.execute.assert_not_awaited()


async def test_required_tables_excludes_retired_names():
    """历史表名不得留在校验清单里（否则该校验恒为 False、每次启动误报缺表）。"""
    assert "conversations" not in REQUIRED_TABLES
    assert "workflows" not in REQUIRED_TABLES
