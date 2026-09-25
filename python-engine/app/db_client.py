"""Unified database and Redis client for Python engine.

This module provides a unified interface to access PostgreSQL and Redis
through the Go gateway's internal API endpoints, instead of direct connections.

Benefits:
- Centralized connection management in Go layer
- Unified monitoring and health checks
- Easy scaling (read replicas, sharding)
- Reduced database exposure surface
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class GatewayClientError(Exception):
    """Base error for gateway client calls."""


class DBClientError(GatewayClientError):
    """Database client error."""


class RedisClientError(GatewayClientError):
    """Redis client error."""


class _BaseGatewayClient:
    """Base class for gateway-proxied clients (DB / Redis).

    Shared HTTP transport, authentication, and error handling.
    Subclasses define ``_error_cls`` and service-specific methods.
    """

    _error_cls: type[GatewayClientError] = GatewayClientError
    _health_path: str = ""

    def __init__(self, base_url: str | None = None, internal_token: str | None = None):
        self.base_url = (base_url or settings.gateway_internal_url).rstrip("/")
        self.internal_token = internal_token or settings.internal_token
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=settings.http_timeout_default)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request(
        self, method: str, path: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        client = await self._get_client()
        headers = {"X-Internal-Token": self.internal_token}
        url = f"{self.base_url}{path}"

        try:
            if method == "GET":
                resp = await client.get(url, headers=headers)
            else:
                resp = await client.post(url, headers=headers, json=data)

            resp.raise_for_status()
            result = resp.json()
            if not result.get("success"):
                raise self._error_cls(result.get("error", "unknown error"))
            payload: dict[str, Any] = result.get("data", {})
            return payload
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error {e.response.status_code}: {e.response.text}")
            raise self._error_cls(f"HTTP {e.response.status_code}") from e
        except self._error_cls:
            raise
        except Exception as e:
            logger.error(f"Request failed: {e}")
            raise self._error_cls(str(e)) from e

    async def health_check(self) -> dict[str, Any]:
        """Check service health."""
        return await self._request("GET", self._health_path)

    async def ping(self) -> bool:
        """Simple connectivity check."""
        try:
            result = await self.health_check()
            available: bool = result.get("available", False)
            ping_ok: bool = result.get("ping_ok", False)
            return available and ping_ok
        except Exception:
            return False


class UnifiedDBClient(_BaseGatewayClient):
    """Unified database client that calls Go gateway API."""

    _error_cls = DBClientError
    _health_path = "/v1/internal/db/health"

    async def fetch_one(self, sql: str, args: list[Any] | None = None) -> dict[str, Any] | None:
        """Execute query and return first row."""
        data = {"sql": sql, "args": args or []}
        result = await self._request("POST", "/v1/internal/db/query", data)
        rows: list[dict[str, Any]] = result.get("rows", [])
        return rows[0] if rows else None

    async def fetch_all(self, sql: str, args: list[Any] | None = None) -> list[dict[str, Any]]:
        """Execute query and return all rows."""
        data = {"sql": sql, "args": args or []}
        result = await self._request("POST", "/v1/internal/db/query", data)
        rows: list[dict[str, Any]] = result.get("rows", [])
        return rows

    async def execute(self, sql: str, args: list[Any] | None = None) -> int:
        """Execute write SQL and return affected rows."""
        data = {"sql": sql, "args": args or []}
        result = await self._request("POST", "/v1/internal/db/execute", data)
        affected: int = result.get("rows_affected", 0)
        return affected

    async def batch_execute(self, queries: list[str]) -> bool:
        """Batch execute multiple SQL statements."""
        data = {"queries": queries}
        result = await self._request("POST", "/v1/internal/db/batch-execute", data)
        ok: bool = result.get("success", False)
        return ok


class UnifiedRedisClient(_BaseGatewayClient):
    """Unified Redis client that calls Go gateway API.

    **不要再把它接回 ``app/redis_client.get_redis()``。** 它只实现了 get / set / delete
    三个方法，而调用方需要的是 xadd（工作流入队）、eval / zadd（限流 Lua）、exists / incr /
    expire 等。曾经那层 ``_UnifiedRedisWrapper`` 就是这么接的，结果是
    ``USE_UNIFIED_REDIS_CLIENT=true`` 时工作流入队恒失败，还会被上层误当成"队列暂不可达"
    （见 docs/production-readiness-fixes.md 的 X9）。需要更多命令就在 aioredis 上直接用 ——
    不要再造一个"实现了一半"的适配层：类型注解看不出来，只有运行时才炸。
    """

    _error_cls = RedisClientError
    _health_path = "/v1/internal/redis/health"

    async def get(self, key: str) -> str | None:
        """Get value by key."""
        data = {"key": key}
        result = await self._request("POST", "/v1/internal/redis/get", data)
        value: str | None = result.get("value")
        return value

    async def set(self, key: str, value: Any, ttl: int | None = None) -> bool:
        """Set value with optional TTL (seconds)."""
        data = {"key": key, "value": value}
        if ttl is not None:
            data["ttl"] = ttl
        result = await self._request("POST", "/v1/internal/redis/set", data)
        ok: bool = result.get("success", False)
        return ok

    async def delete(self, *keys: str) -> bool:
        """Delete one or more keys."""
        data = {"keys": list(keys)}
        result = await self._request("POST", "/v1/internal/redis/del", data)
        ok: bool = result.get("success", False)
        return ok


# Global instances
_db_client: UnifiedDBClient | None = None
_redis_client: UnifiedRedisClient | None = None


def get_db_client() -> UnifiedDBClient:
    """Get global database client instance."""
    global _db_client
    if _db_client is None:
        _db_client = UnifiedDBClient()
    return _db_client


def get_redis_client() -> UnifiedRedisClient:
    """Get global Redis client instance."""
    global _redis_client
    if _redis_client is None:
        _redis_client = UnifiedRedisClient()
    return _redis_client


async def close_clients() -> None:
    """Close all client connections."""
    global _db_client, _redis_client
    if _db_client:
        await _db_client.close()
        _db_client = None
    if _redis_client:
        await _redis_client.close()
        _redis_client = None
