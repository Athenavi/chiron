"""Task 8: 单元测试 - MemoryService 基础。

测试 on_session_start、on_turn_complete、on_session_end 及工具集成。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest

from app.memory.layers import (
    ProfileItem,
    SessionContext,
    SlotType,
    SourceType,
)
from app.memory.profile_card import ProfileCard
from app.memory.service import (
    MemoryService,
    get_memory_service,
    set_memory_service,
)
from app.memory.session_meta import SessionMetaStore

# ── Mock 基础设施 ────────────────────────────────────────────────────────


class MockDatabasePool:
    """模拟 asyncpg 连接池。"""

    def __init__(self):
        self._items: dict[tuple, dict[str, Any]] = {}

    def _key(self, tenant_id, user_id, slot, item_key):
        return (tenant_id, user_id, slot, item_key)

    def _make_row(self, item):
        confirmed_at = item.get("confirmed_at")
        last_ref = item.get("last_referenced_at")
        return {
            "slot": item["slot"],
            "item_key": item["item_key"],
            "item_value": item["item_value"],
            "confidence": item["confidence"],
            "source": item["source"],
            "version": item["version"],
            "confirmed_at": datetime.fromtimestamp(confirmed_at, tz=UTC) if confirmed_at else None,
            "last_referenced_at": datetime.fromtimestamp(last_ref, tz=UTC) if last_ref else None,
            "created_at": datetime.fromtimestamp(item["created_at"], tz=UTC),
            "updated_at": datetime.fromtimestamp(item["updated_at"], tz=UTC),
        }

    async def fetch(self, query, *args):
        if "SELECT slot, item_key" in query and "FROM user_memory_profile" in query:
            if "WHERE tenant_id = $1 AND user_id = $2" in query:
                tenant_id, user_id = args[0], args[1]
                results = []
                for (tid, uid, _slot, _key), item in self._items.items():
                    if tid == tenant_id and uid == user_id:
                        results.append(self._make_row(item))
                results.sort(key=lambda r: (r["slot"], r["item_key"]))
                return results
        return []

    async def fetchrow(self, query, *args):
        if "slot = $3 AND item_key = $4" in query:
            tenant_id, user_id, slot, item_key = args[0], args[1], args[2], args[3]
            item = self._items.get(self._key(tenant_id, user_id, slot, item_key))
            return self._make_row(item) if item else None
        return None

    async def execute(self, query, *args):
        if "INSERT INTO user_memory_profile" in query:
            tenant_id, user_id, slot, item_key = args[0], args[1], args[2], args[3]
            item_value, confidence, source = args[4], args[5], args[6]
            now_ts = args[7]  # datetime

            key = self._key(tenant_id, user_id, slot, item_key)
            self._items[key] = {
                "slot": slot, "item_key": item_key,
                "item_value": item_value, "confidence": confidence,
                "source": source, "version": 1,
                "confirmed_at": now_ts.timestamp() if source == "user_confirmed" else None,
                "created_at": now_ts.timestamp(),
                "updated_at": now_ts.timestamp(),
            }
            return "INSERT 0 1"
        elif "UPDATE user_memory_profile" in query:
            tenant_id, user_id, slot, item_key = args[0], args[1], args[2], args[3]
            item_value, confidence, source = args[4], args[5], args[6]
            version = args[7]
            now_ts = args[8]  # datetime

            key = self._key(tenant_id, user_id, slot, item_key)
            if key in self._items:
                item = self._items[key]
                item["item_value"] = item_value
                item["confidence"] = confidence
                item["source"] = source
                item["version"] = version
                item["updated_at"] = now_ts.timestamp()
                if source == "user_confirmed":
                    item["confirmed_at"] = now_ts.timestamp()
                return "UPDATE 1"
            return "UPDATE 0"
        elif "DELETE FROM user_memory_profile" in query:
            if "slot = $3 AND item_key = $4" in query:
                tenant_id, user_id, slot, item_key = args[0], args[1], args[2], args[3]
                key = self._key(tenant_id, user_id, slot, item_key)
                if key in self._items:
                    del self._items[key]
                    return "DELETE 1"
                return "DELETE 0"
        return "OK"


class MockRedis:
    """模拟 Redis。"""

    def __init__(self):
        self._store: dict[str, str] = {}
        self._expiry: dict[str, float] = {}
        self._sets: dict[str, set[str]] = {}

    async def get(self, key):
        if key in self._expiry and time.time() > self._expiry[key]:
            self._store.pop(key, None)
            self._expiry.pop(key, None)
            return None
        return self._store.get(key)

    async def set(self, key, value, ex=None):
        self._store[key] = value
        return True

    async def setex(self, key, ttl, value):
        self._store[key] = value
        self._expiry[key] = time.time() + ttl
        return True

    async def delete(self, *keys):
        for key in keys:
            if key in self._store:
                self._store.pop(key, None)
                self._expiry.pop(key, None)
                return True
        return False

    async def sadd(self, key, value):
        if key not in self._sets:
            self._sets[key] = set()
        self._sets[key].add(value)
        return True

    async def srem(self, key, value):
        if key in self._sets:
            self._sets[key].discard(value)
        return True

    async def smembers(self, key):
        return self._sets.get(key, set())

    async def expire(self, key, ttl):
        self._expiry[key] = time.time() + ttl
        return True

    async def incr(self, key):
        count = int(self._store.get(key, "0"))
        count += 1
        self._store[key] = str(count)
        return count


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def mock_redis():
    return MockRedis()


@pytest.fixture
def mock_pool():
    return MockDatabasePool()


@pytest.fixture
def profile_card(mock_redis, mock_pool):
    from app.memory.conflict_manager import ConflictManager
    pc = ProfileCard.__new__(ProfileCard)
    pc._redis = mock_redis
    pc._pool = mock_pool
    pc._conflict_manager = ConflictManager(mock_redis)
    return pc


@pytest.fixture
def service(profile_card):
    """创建 MemoryService 实例。"""
    session_meta_store = SessionMetaStore()
    return MemoryService(
        session_meta_store=session_meta_store,
        profile_card=profile_card,
        summary_store=None,
    )


@pytest.fixture
def seeded_service(profile_card):
    """创建预置数据的 MemoryService 实例。"""
    session_meta_store = SessionMetaStore()
    svc = MemoryService(
        session_meta_store=session_meta_store,
        profile_card=profile_card,
        summary_store=None,
    )

    async def _seed():
        await svc.update_profile(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="pref_lang",
            item_value="Python", confidence=90,
            source=SourceType.USER_CONFIRMED,
        )

    import asyncio
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_seed())
    finally:
        loop.close()

    return svc


# ── Task 8.2: on_session_start ──────────────────────────────────────────


class TestOnSessionStart:
    """测试 on_session_start：L1 建立 + L2 预取。"""

    @pytest.mark.asyncio
    async def test_creates_session_meta(self, service):
        """应在 L1 创建 SessionMeta。"""
        context = await service.on_session_start(
            session_id="sess-001",
            tenant_id="t1",
            user_id="u1",
            entry_channel="web",
            mode="agent",
        )
        assert isinstance(context, SessionContext)
        assert context.meta.session_id == "sess-001"
        assert context.meta.tenant_id == "t1"
        assert context.meta.user_id == "u1"
        assert context.meta.entry_channel == "web"
        assert context.meta.mode == "agent"
        assert context.meta.turn_count == 0
        assert context.meta.total_tokens_in == 0
        assert context.meta.total_tokens_out == 0

    @pytest.mark.asyncio
    async def test_prefetches_profile(self, seeded_service):
        """应预取 L2 档案卡。"""
        context = await seeded_service.on_session_start(
            session_id="sess-002",
            tenant_id="t1",
            user_id="u1",
        )
        assert context.profile_cached is True
        assert context.summaries_prefetched == 0

    @pytest.mark.asyncio
    async def test_empty_profile_cached_false(self, service):
        """无档案时 profile_cached 应为 False。"""
        context = await service.on_session_start(
            session_id="sess-003",
            tenant_id="t1",
            user_id="u1",
        )
        assert context.profile_cached is False

    @pytest.mark.asyncio
    async def test_different_sessions_isolated(self, service):
        """不同会话应独立存储。"""
        await service.on_session_start("sess-001", "t1", "u1")
        await service.on_session_start("sess-002", "t1", "u1")

        meta1 = service._session_meta.get("sess-001")
        meta2 = service._session_meta.get("sess-002")
        assert meta1 is not None
        assert meta2 is not None
        assert meta1.session_id == "sess-001"
        assert meta2.session_id == "sess-002"


# ── Task 8.3: on_turn_complete ──────────────────────────────────────────


class TestOnTurnComplete:
    """测试 on_turn_complete：turn_count 递增、token 累计。"""

    @pytest.mark.asyncio
    async def test_increments_turn_count(self, service):
        """应递增回合计数。"""
        await service.on_session_start("sess-001", "t1", "u1")
        meta_before = service._session_meta.get("sess-001")
        assert meta_before.turn_count == 0

        await service.on_turn_complete("sess-001", tokens_in=100, tokens_out=50)
        meta_after = service._session_meta.get("sess-001")
        assert meta_after.turn_count == 1

    @pytest.mark.asyncio
    async def test_accumulates_tokens(self, service):
        """应累计 token 用量。"""
        await service.on_session_start("sess-001", "t1", "u1")

        await service.on_turn_complete("sess-001", tokens_in=100, tokens_out=50)
        meta = service._session_meta.get("sess-001")
        assert meta.total_tokens_in == 100
        assert meta.total_tokens_out == 50

        await service.on_turn_complete("sess-001", tokens_in=200, tokens_out=80)
        meta = service._session_meta.get("sess-001")
        assert meta.total_tokens_in == 300
        assert meta.total_tokens_out == 130

    @pytest.mark.asyncio
    async def test_updates_last_active_at(self, service):
        """应更新 last_active_at 时间戳。"""
        await service.on_session_start("sess-001", "t1", "u1")
        meta_before = service._session_meta.get("sess-001")
        before_ts = meta_before.last_active_at

        await service.on_turn_complete("sess-001", tokens_in=10, tokens_out=5)
        meta_after = service._session_meta.get("sess-001")
        assert meta_after.last_active_at >= before_ts

    @pytest.mark.asyncio
    async def test_nonexistent_session_no_error(self, service):
        """不存在的会话不应报错。"""
        await service.on_turn_complete("nonexistent", tokens_in=10, tokens_out=5)

    @pytest.mark.asyncio
    async def test_multiple_turns(self, service):
        """多回合累计验证。"""
        await service.on_session_start("sess-001", "t1", "u1")
        for i in range(5):
            await service.on_turn_complete("sess-001", tokens_in=100 * (i + 1), tokens_out=50 * (i + 1))
        meta = service._session_meta.get("sess-001")
        assert meta.turn_count == 5
        assert meta.total_tokens_in == 100 + 200 + 300 + 400 + 500
        assert meta.total_tokens_out == 50 + 100 + 150 + 200 + 250


# ── Task 8.4: on_session_end ─────────────────────────────────────────────


class TestOnSessionEnd:
    """测试 on_session_end：L1 丢弃。"""

    @pytest.mark.asyncio
    async def test_deletes_session_meta(self, service):
        """应删除 L1 中的会话元数据。"""
        await service.on_session_start("sess-001", "t1", "u1")
        assert service._session_meta.get("sess-001") is not None

        await service.on_session_end("sess-001")
        assert service._session_meta.get("sess-001") is None

    @pytest.mark.asyncio
    async def test_multiple_sessions_independent_end(self, service):
        """结束一个会话不应影响其他会话。"""
        await service.on_session_start("sess-001", "t1", "u1")
        await service.on_session_start("sess-002", "t1", "u1")

        await service.on_session_end("sess-001")

        assert service._session_meta.get("sess-001") is None
        assert service._session_meta.get("sess-002") is not None

    @pytest.mark.asyncio
    async def test_end_nonexistent_session(self, service):
        """结束不存在的会话不应报错。"""
        await service.on_session_end("ghost-session")


# ── Task 8.5: 工具集成 ──────────────────────────────────────────────────


class TestToolIntegration:
    """测试工具集成：remember/recall/forget。"""

    @pytest.mark.asyncio
    async def test_update_profile_via_service(self, service):
        """update_profile 应正确调用 ProfileCard.upsert_item。"""
        result = await service.update_profile(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="hobby",
            item_value="coding", confidence=80,
            source=SourceType.DERIVED,
        )
        assert result.success is True
        assert result.item is not None
        assert result.item.item_value == "coding"

        profile = await service._profile_card.get_profile("t1", "u1")
        assert len(profile) == 1
        assert profile[0].item_key == "hobby"

    @pytest.mark.asyncio
    async def test_update_profile_conflict_detection(self, service):
        """update_profile 应检测冲突。"""
        await service.update_profile(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="email",
            item_value="user@test.com",
            source=SourceType.USER_CONFIRMED,
        )
        result = await service.update_profile(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="email",
            item_value="other@test.com",
            source=SourceType.DERIVED,
        )
        assert result.success is False
        assert result.conflict is not None

    @pytest.mark.asyncio
    async def test_forget_via_service(self, service):
        """forget 应正确调用 ProfileCard.delete_item。"""
        await service.update_profile(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="temp",
            item_value="remove_me",
        )
        assert len(await service._profile_card.get_profile("t1", "u1")) == 1

        result = await service.forget("t1", "u1", SlotType.FACT, "temp")
        assert result is True
        assert len(await service._profile_card.get_profile("t1", "u1")) == 0





    @pytest.mark.asyncio
    async def test_forget_nonexistent_item(self, service):
        """forget 不存在的条目应返回 False。"""
        result = await service.forget("t1", "u1", SlotType.FACT, "ghost")
        assert result is False


# ── 全局单例工厂测试 ──────────────────────────────────────────────────────


class TestGlobalSingleton:
    """测试全局单例工厂函数。"""

    def test_set_and_get_memory_service(self):
        """应能设置和获取全局实例。"""
        session_meta_store = SessionMetaStore()
        pc = ProfileCard.__new__(ProfileCard)
        svc = MemoryService(
            session_meta_store=session_meta_store,
            profile_card=pc,
            summary_store=None,
        )
        set_memory_service(svc)
        assert get_memory_service() is svc

    def test_get_memory_service_before_init(self):
        """未初始化时应返回 None。"""
        set_memory_service(None)
        assert get_memory_service() is None


# ── 端到端生命周期测试 ──────────────────────────────────────────────────


class TestEndToEndLifecycle:
    """测试完整会话生命周期。"""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, service):
        """测试 start → turn → end 完整流程。"""
        ctx = await service.on_session_start("e2e-001", "t1", "u1")
        assert ctx.meta.turn_count == 0

        await service.on_turn_complete("e2e-001", tokens_in=150, tokens_out=80)
        meta = service._session_meta.get("e2e-001")
        assert meta.turn_count == 1
        assert meta.total_tokens_in == 150

        await service.on_turn_complete("e2e-001", tokens_in=200, tokens_out=100)
        meta = service._session_meta.get("e2e-001")
        assert meta.turn_count == 2
        assert meta.total_tokens_in == 350
        assert meta.total_tokens_out == 180

        await service.update_profile(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="workflow",
            item_value="tested", confidence=90,
        )

        recall_result = await service.recall("t1", "u1")
        assert "tested" in recall_result.profile_block

        await service.on_session_end("e2e-001")
        assert service._session_meta.get("e2e-001") is None

        profile = await service._profile_card.get_profile("t1", "u1")
        assert len(profile) == 1

    @pytest.mark.asyncio
    async def test_lifecycle_preserves_isolation(self, service):
        """测试多会话隔离。"""
        await service.on_session_start("sess-001", "t1", "u1")
        await service.update_profile(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="sess1_data",
            item_value="from_sess1",
        )

        await service.on_session_start("sess-002", "t1", "u2")
        await service.update_profile(
            tenant_id="t1", user_id="u2",
            slot=SlotType.FACT, item_key="sess2_data",
            item_value="from_sess2",
        )

        profile1 = await service._profile_card.get_profile("t1", "u1")
        profile2 = await service._profile_card.get_profile("t1", "u2")
        assert len(profile1) == 1
        assert profile1[0].item_value == "from_sess1"
        assert len(profile2) == 1
        assert profile2[0].item_value == "from_sess2"

        await service.on_session_end("sess-001")
        profile2_after = await service._profile_card.get_profile("t1", "u2")
        assert len(profile2_after) == 1
        assert profile2_after[0].item_value == "from_sess2"


# ── 辅助方法测试 ────────────────────────────────────────────────────────


class TestSerializeProfile:
    """测试档案卡序列化辅助方法。"""

    def test_empty_items(self):
        """空列表应返回默认文本。"""
        result = MemoryService._serialize_profile([])
        assert result == "暂无用户档案信息"

    def test_formats_items_correctly(self):
        """应正确格式化条目。"""
        items = [
            ProfileItem(
                slot=SlotType.FACT, item_key="name",
                item_value="Alice", confidence=100,
                source=SourceType.USER_CONFIRMED, version=1,
                confirmed_at=time.time(),
                last_referenced_at=None,
                created_at=time.time(),
                updated_at=time.time(),
            )
        ]
        result = MemoryService._serialize_profile(items)
        assert "✓" in result
        assert "[fact]" in result
        assert "name" in result
        assert "Alice" in result
        assert "[已确认]" in result

    def test_truncates_long_profile(self):
        """超长档案应截断。"""
        items = [
            ProfileItem(
                slot=SlotType.FACT,
                item_key=f"key_{i}",
                item_value=f"very_long_value_{i}_" * 20,
                confidence=50,
                source=SourceType.DERIVED,
                version=1,
                confirmed_at=None,
                last_referenced_at=None,
                created_at=time.time(),
                updated_at=time.time(),
            )
            for i in range(100)
        ]
        result = MemoryService._serialize_profile(items)
        assert len(result.encode("utf-8")) <= 1500 or "已截断" in result


# ── 占位方法测试 ────────────────────────────────────────────────────────




# ── L3 路径测试 ──────────────────────────────────────────────────────────












class TestTurnRangeOverlap:
    """测试 _is_turn_range_overlap 静态方法。"""

    def test_overlapping_ranges(self):
        """重叠范围应返回 True。"""
        assert MemoryService._is_turn_range_overlap((10, 20), (15, 25)) is True
        assert MemoryService._is_turn_range_overlap((10, 20), (5, 15)) is True
        assert MemoryService._is_turn_range_overlap((10, 20), (10, 20)) is True
        assert MemoryService._is_turn_range_overlap((10, 20), (0, 100)) is True

    def test_non_overlapping_ranges(self):
        """不重叠范围应返回 False。"""
        assert MemoryService._is_turn_range_overlap((10, 20), (25, 30)) is False
        assert MemoryService._is_turn_range_overlap((10, 20), (0, 5)) is False

    def test_boundary_overlap(self):
        """边界相邻应返回 True（视为重叠）。"""
        assert MemoryService._is_turn_range_overlap((10, 20), (20, 30)) is True






# ── save_summary 测试 ──────────────────────────────────────────────────








# ── create_memory_service 工厂测试 ──────────────────────────────────────


class TestCreateMemoryService:
    """测试 create_memory_service 工厂函数。"""

    def test_creates_service_without_redis(self):
        """无 Redis 时应创建 MemoryService（profile_card 为 None）。"""
        from app.memory.service import create_memory_service
        svc = create_memory_service()
        assert isinstance(svc, MemoryService)
        assert svc._profile_card is None
        assert svc._summary_store is None
        svc._session_meta.get("test")  # 应可正常访问

    def test_creates_service_with_redis(self):
        """有 Redis 时应创建带 ProfileCard 的 MemoryService。"""
        from unittest.mock import patch

        from app.memory.service import create_memory_service
        fake_redis = MockRedis()
        with patch("app.memory.profile_card.get_pool"):
            svc = create_memory_service(redis=fake_redis)
        assert isinstance(svc, MemoryService)
        assert svc._profile_card is not None
