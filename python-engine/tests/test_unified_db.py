"""Test unified database and Redis clients."""
import asyncio

import pytest


@pytest.fixture(autouse=True)
def _enable_unified_clients(monkeypatch):
    """以 autouse fixture 打开统一客户端开关。

    这两个开关必须在 ``import app.db_client`` 之前生效，但**不能**写成模块级的
    ``os.environ[...] = "true"`` —— 那会在收集阶段污染整个 pytest 会话，让其它
    测试也意外跑在 unified 模式下。
    """
    monkeypatch.setenv("USE_UNIFIED_DB_CLIENT", "true")
    monkeypatch.setenv("USE_UNIFIED_REDIS_CLIENT", "true")


async def _require_gateway() -> None:
    """这两条走的是**网关的内部 API**（`/v1/internal/db/*`、`/v1/internal/redis/*`），
    不是直连 PG/Redis —— 见 `app/db_client.py` 的模块头。

    没有运行中的网关时它们必然 `RedisClientError: All connection attempts failed`，
    而那是**环境不具备**、不是代码缺陷 ⇒ 按本仓纪律 skip 而不是红（与 `conftest.py` 对
    `POSTGRES_DSN` 的处理同一条口径）。
    """
    import httpx

    from app.config import settings

    url = settings.gateway_internal_url.rstrip("/") + "/v1/internal/db/health"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.get(url, headers={"X-Internal-Token": settings.internal_token})
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"网关不可达（{settings.gateway_internal_url}）：{str(exc)[:80]}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unified_db_client():
    """Test unified database client operations."""
    await _require_gateway()
    from app.db_client import get_db_client

    db = get_db_client()

    # Test health check
    health = await db.health_check()
    assert "available" in health
    print(f"DB Health: {health}")

    # Test ping
    is_alive = await db.ping()
    print(f"DB Ping: {is_alive}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unified_redis_client():
    """Test unified Redis client operations."""
    await _require_gateway()
    from app.db_client import get_redis_client

    redis = get_redis_client()

    # Test health check
    health = await redis.health_check()
    assert "available" in health
    print(f"Redis Health: {health}")

    # Test basic operations
    await redis.set("test_key", "test_value", ttl=60)
    value = await redis.get("test_key")
    assert value == "test_value"

    await redis.delete("test_key")
    value = await redis.get("test_key")
    assert value is None

    print("Redis operations successful")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_compatibility_layer():
    """Test that existing code still works with compatibility layer."""
    from app.db import get_pool
    from app.redis_client import get_redis

    # Test DB pool wrapper
    pool = get_pool()
    print(f"Pool type: {type(pool)}")

    # Test Redis wrapper
    redis = await get_redis()
    print(f"Redis type: {type(redis)}")

    # Basic operations should work
    await redis.set("compat_test", "value")
    val = await redis.get("compat_test")
    assert val == "value"

    await redis.delete("compat_test")
    print("Compatibility layer works!")


if __name__ == "__main__":
    asyncio.run(test_unified_db_client())
    asyncio.run(test_unified_redis_client())
    asyncio.run(test_compatibility_layer())
    print("\n✅ All tests passed!")
