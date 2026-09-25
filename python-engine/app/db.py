"""PostgreSQL connection pool for graph persistence.

This module provides a compatibility layer that can use either:
1. Direct asyncpg connection (legacy mode, for development)
2. Unified DB client through Go gateway (recommended for production)

The mode is controlled by USE_UNIFIED_DB_CLIENT environment variable.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, cast

import asyncpg

from app.config import settings

if TYPE_CHECKING:
    # 仅为类型注解：运行时仍走函数内的延迟 import，避免模块加载期就拉起 httpx。
    from app.db_client import UnifiedDBClient

logger = logging.getLogger(__name__)

# Configuration: use unified client or direct connection
USE_UNIFIED = os.getenv("USE_UNIFIED_DB_CLIENT", "false").lower() == "true"

_pool: asyncpg.Pool | None = None
_unified_client: UnifiedDBClient | None = None
# 缓存 _UnifiedPoolWrapper 实例，避免每次 get_pool() 新建
_unified_pool_wrapper: _UnifiedPoolWrapper | None = None


def _get_unified_client() -> UnifiedDBClient | None:
    """Lazy load unified client."""
    global _unified_client
    if _unified_client is None and USE_UNIFIED:
        from app.db_client import get_db_client

        _unified_client = get_db_client()
    return _unified_client


async def init_pool(dsn: str) -> asyncpg.Pool | None:
    """Initialize the global connection pool (legacy mode only).

    unified 模式下不建直连池，返回 None（此前注解写成 ``asyncpg.Pool`` 与这里的
    ``return None`` 矛盾）。
    """
    if USE_UNIFIED:
        logger.info("Using unified DB client through Go gateway")
        return None

    global _pool
    _pool = await asyncpg.create_pool(
        dsn, min_size=settings.db_pool_min_size, max_size=settings.db_pool_max_size
    )
    logger.info(
        "PostgreSQL connected directly (pool=%d-%d)",
        settings.db_pool_min_size,
        settings.db_pool_max_size,
    )
    await _log_pool_capacity(_pool)
    return _pool


async def _log_pool_capacity(pool: asyncpg.Pool) -> None:
    """打出本实例池上限 / PG 上限 / 按此池大小能容纳多少实例。

    企业化扩容的关键约束：网关与引擎各自持有连接池、都连同一个 PG，因此

        N_网关 × MaxConns + M_引擎 × DB_POOL_MAX_SIZE ≤ PG max_connections

    应用无法知道集群里有多少实例，但可以把自己这一份与 PG 上限一起打出来，让运维
    扩容时能直接算，而不是等到"连接被拒"才发现。失败不影响启动。
    """
    own = settings.db_pool_max_size
    try:
        pg_max = await pool.fetchval("SELECT current_setting('max_connections')::int")
    except Exception as e:  # noqa: BLE001
        logger.debug("read pg max_connections failed: %s", e)
        return
    if not pg_max or not own:
        return
    instances = int(pg_max) // int(own)
    logger.info(
        "PostgreSQL pool capacity: pool_max_size=%d pg_max_connections=%d "
        "instances_supported=%d (the gateway holds its own pool against the same PG)",
        own,
        pg_max,
        instances,
    )
    if instances < 4:
        logger.warning(
            "PostgreSQL pool headroom is thin for horizontal scaling: "
            "pool_max_size=%d pg_max_connections=%d instances_supported=%d",
            own,
            pg_max,
            instances,
        )


async def close_pool() -> None:
    """Close the global connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None

    # Close unified client
    if _unified_client:
        from app.db_client import close_clients

        await close_clients()


def get_pool() -> asyncpg.Pool:
    """Get the global connection pool or unified client wrapper."""
    if USE_UNIFIED:
        client = _get_unified_client()
        if client is None:
            raise RuntimeError("Unified DB client not initialized")
        # 返回缓存的包装器实例，委托给 unified client
        global _unified_pool_wrapper
        if _unified_pool_wrapper is None:
            _unified_pool_wrapper = _UnifiedPoolWrapper(client)
        # 刻意的类型断言：unified 模式返回的是鸭子类型的适配器（只实现 fetchrow /
        # fetch / execute / executemany / transaction），不是真的 asyncpg.Pool。
        # 改成联合类型会让两侧签名冲突、波及全部调用点，故在此收敛。
        return cast("asyncpg.Pool", _unified_pool_wrapper)

    if _pool is None:
        raise RuntimeError("PostgreSQL pool not initialized")
    # Check if pool is closed (using internal attribute)
    if getattr(_pool, "_closed", False):
        raise RuntimeError("PostgreSQL pool was closed")
    return _pool


class _UnifiedPoolWrapper:
    """Wrapper to make UnifiedDBClient compatible with asyncpg.Pool interface."""

    def __init__(self, client: UnifiedDBClient):
        self._client = client

    async def fetchrow(self, query: str, *args: Any) -> _RowDict | None:
        """Execute query and return single row."""
        result = await self._client.fetch_one(query, list(args))
        return _RowDict(result) if result else None

    async def fetch(self, query: str, *args: Any) -> list[_RowDict]:
        """Execute query and return all rows."""
        results = await self._client.fetch_all(query, list(args))
        return [_RowDict(r) for r in results]

    async def execute(self, query: str, *args: Any) -> str:
        """Execute write SQL."""
        affected = await self._client.execute(query, list(args))
        return str(affected)

    async def executemany(self, query: str, args_list: list[Any]) -> bool:
        """Batch execute."""
        queries = [
            query % tuple(a) if isinstance(a, tuple) else query for a in args_list
        ]
        return await self._client.batch_execute(queries)

    async def transaction(self, **kwargs: Any) -> Any:
        """Return a transaction context manager (not fully implemented)."""
        raise NotImplementedError("Transactions not supported in unified mode yet")


class _RowDict(dict[str, Any]):
    """Dictionary-like row object compatible with asyncpg.Record."""

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None


# 引擎启动时必须存在的表（只读校验用）。DDL 的唯一权威是 Alembic：
# migrations/versions/0001_authoritative_baseline.py。
#
# 注意：本清单**不得**再列入已不存在的历史表名。曾经的 `conversations` / `workflows`
# 早已被 `conversation_shares` / `workflow_graphs` / `workflow_instances` 取代，但清单
# 没跟着改，导致下面这个校验恒为 False、每次启动都误报"缺少 N 张表"。
REQUIRED_TABLES = [
    "users",
    "sessions",
    "agents",
    "messages",
    "knowledge_bases",
    "knowledge_documents",
    "knowledge_chunks",
    "media_assets",
    "uploads",
    "workflow_instances",
    "cron_jobs",
    "audit_logs",
    "billing_records",
    "credit_transactions",
    "payments",
    "ent_oidc_providers",
    "ent_user_identities",
    "ent_captcha_config",
    "ent_quota_pools",
    "ent_quota_allocations",
]


async def ensure_tables() -> bool:
    """只读校验必需表是否存在，缺失时提示运行迁移。

    本函数**不执行任何 DDL**：建表全部由 Alembic 权威迁移负责。
    """
    pool = get_pool()

    try:
        # 查询所有已存在的表
        existing = await pool.fetch("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
        """)
        existing_names = {row["table_name"] for row in existing}

        missing = [t for t in REQUIRED_TABLES if t not in existing_names]

        if missing:
            logger.warning(
                f"Missing database tables: {', '.join(missing)}. "
                f"Please run: alembic upgrade head"
            )
            return False

        logger.info("All required database tables exist")
        return True

    except Exception as e:
        logger.error(f"Failed to check database tables: {e}")
        return False
