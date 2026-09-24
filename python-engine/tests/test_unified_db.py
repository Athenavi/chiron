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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unified_db_client():
    """Test unified database client operations."""
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
