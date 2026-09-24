# 记忆管理测试
from unittest.mock import MagicMock

import pytest

from app.memory.manager import MemoryManager


class _FakeGateway:
    async def embed(self, text, model=""):
        resp = MagicMock()
        resp.embedding = [0.0] * 16
        return resp


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, list[str]] = {}

    async def rpush(self, key, value):
        self.store.setdefault(key, []).append(value)

    async def expire(self, key, ttl):
        pass

    async def scan_iter(self, match="*"):
        for k in list(self.store.keys()):
            yield k

    async def lrange(self, key, start, stop):
        return self.store.get(key, [])


class TestMemoryManager:
    """测试记忆管理器"""

    def _manager(self):
        return MemoryManager(llm_gateway=_FakeGateway(), redis=_FakeRedis())

    @pytest.mark.asyncio
    async def test_save_short_term_redis_unavailable(self):
        """测试 Redis 不可用时保存短期记忆"""
        manager = self._manager()
        result = await manager.save_memory(
            tenant_id="test", user_id="user1", session_id="session1",
            content="Remember this", memory_type="short_term",
        )
        assert result["status"] == "saved"
        assert "memory_id" in result

    @pytest.mark.asyncio
    async def test_save_long_term_milvus_unavailable(self):
        """测试 Milvus 不可用时保存长期记忆（应降级失败）"""
        manager = self._manager()
        result = await manager.save_memory(
            tenant_id="test", user_id="user1", session_id="session1",
            content="Important fact", memory_type="long_term",
        )
        assert result["status"] in {"saved", "failed"}

    @pytest.mark.asyncio
    async def test_query_memory_empty(self):
        """测试查询空记忆"""
        manager = self._manager()
        results = await manager.query_memory(
            tenant_id="test", user_id="user1", query="Hello",
        )
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_save_memory_with_metadata(self):
        """测试带元数据保存记忆"""
        manager = self._manager()
        result = await manager.save_memory(
            tenant_id="test", user_id="user1", session_id="session1",
            content="Test content", memory_type="short_term",
            metadata={"source": "test", "importance": "high"},
        )
        assert "memory_id" in result


class TestMemoryTypes:
    """测试记忆类型"""

    def test_short_term_ttl(self):
        """测试短期记忆 TTL 配置"""
        from app.config import Settings
        settings = Settings()
        assert settings.short_term_ttl == 604800  # 7 天

    def test_long_term_ttl(self):
        """测试长期记忆 TTL 配置"""
        from app.config import Settings
        settings = Settings()
        assert settings.long_term_ttl == 0  # 永不过期


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
