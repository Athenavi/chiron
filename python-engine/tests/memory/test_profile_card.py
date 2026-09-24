"""Task 7: 单元测试 - L2 ProfileCard。

测试槽位 upsert 幂等性、version 递增逻辑、冲突规则、Redis 缓存穿透失效
及 200 条目淘汰逻辑。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest

from app.memory.layers import (
    ConflictRef,
    SlotType,
    SourceType,
)
from app.memory.profile_card import CACHE_TTL, ProfileCard

# ── Mock 基础设施 ────────────────────────────────────────────────────────


class MockDatabasePool:
    """模拟 asyncpg 连接池，用于 ProfileCard 测试。"""

    def __init__(self):
        self._items: dict[tuple, dict[str, Any]] = {}
        self._row_id_counter = 1

    def _key(self, tenant_id: str, user_id: str, slot: str, item_key: str) -> tuple:
        return (tenant_id, user_id, slot, item_key)

    def _make_row(self, item: dict[str, Any]) -> dict[str, Any]:
        """将内部存储转换为模拟的 asyncpg Row。"""
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

    async def fetch(self, query: str, *args) -> list[dict[str, Any]]:
        """模拟 fetch 查询。"""
        if "SELECT slot, item_key" in query and "FROM user_memory_profile" in query:
            if "WHERE tenant_id = $1 AND user_id = $2" in query:
                tenant_id, user_id = args[0], args[1]
                results = []
                for (tid, uid, _slot, _key), item in self._items.items():
                    if tid == tenant_id and uid == user_id:
                        results.append(self._make_row(item))
                results.sort(key=lambda r: (r["slot"], r["item_key"]))
                return results
        if "COUNT(*) as cnt" in query:
            tenant_id = args[0]
            limit = args[1]
            counts: dict[tuple, int] = {}
            for (tid, uid, _slot, _key), item in self._items.items():
                if item["source"] != "user_confirmed":
                    k = (tid, uid)
                    counts[k] = counts.get(k, 0) + 1
            return [
                {"tenant_id": tid, "user_id": uid, "cnt": cnt}
                for (tid, uid), cnt in counts.items()
                if cnt > limit and (tenant_id is None or tenant_id == tid)
            ]
        return []

    async def fetchrow(self, query: str, *args) -> dict[str, Any] | None:
        """模拟 fetchrow 查询。"""
        if "SELECT slot, item_key" in query and "slot = $3 AND item_key = $4" in query:
            tenant_id, user_id, slot, item_key = args[0], args[1], args[2], args[3]
            key = self._key(tenant_id, user_id, slot, item_key)
            item = self._items.get(key)
            return self._make_row(item) if item else None
        return None

    async def execute(self, query: str, *args) -> str:
        """模拟 execute 操作。"""
        if "INSERT INTO user_memory_profile" in query:
            # _insert_item 传: tenant_id, user_id, slot, item_key, item_value, confidence, source, now_ts
            tenant_id, user_id, slot, item_key = args[0], args[1], args[2], args[3]
            item_value, confidence, source = args[4], args[5], args[6]
            now_ts = args[7]  # datetime

            key = self._key(tenant_id, user_id, slot, item_key)
            self._items[key] = {
                "slot": slot,
                "item_key": item_key,
                "item_value": item_value,
                "confidence": confidence,
                "source": source,
                "version": 1,
                "confirmed_at": now_ts.timestamp() if source == "user_confirmed" else None,
                "last_referenced_at": None,
                "created_at": now_ts.timestamp(),
                "updated_at": now_ts.timestamp(),
            }
            return "INSERT 0 1"

        elif "UPDATE user_memory_profile" in query:
            # _update_item 传: tenant_id, user_id, slot, item_key, item_value, confidence, source, new_version, now_ts
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
            elif "last_referenced_at < $1" in query:
                threshold = args[0]
                deleted = 0
                for key, item in list(self._items.items()):
                    if item["source"] != "user_confirmed":
                        last_ref = item.get("last_referenced_at")
                        if (last_ref is None or last_ref < threshold) and item["confidence"] < 50:
                            del self._items[key]
                            deleted += 1
                return f"DELETE {deleted}"
            elif "ctid IN" in query:
                tenant_id, user_id, excess = args[0], args[1], args[2]
                candidates = []
                for (tid, uid, slot, key), item in self._items.items():
                    if tid == tenant_id and uid == user_id and item["source"] != "user_confirmed":
                        last_ref = item.get("last_referenced_at") or 0
                        score = item["confidence"] * last_ref
                        candidates.append((score, (tid, uid, slot, key)))
                candidates.sort(key=lambda x: x[0])
                deleted = 0
                for _, key_tuple in candidates[:excess]:
                    if key_tuple in self._items:
                        del self._items[key_tuple]
                        deleted += 1
                return f"DELETE {deleted}"
            return "DELETE 0"

        return "OK"


class MockRedis:
    """模拟 Redis，用于 ProfileCard 缓存测试。"""

    def __init__(self):
        self._store: dict[str, str] = {}
        self._expiry: dict[str, float] = {}
        self._sets: dict[str, set[str]] = {}

    async def get(self, key: str) -> str | None:
        """获取缓存值。"""
        if key in self._expiry:
            if time.time() > self._expiry[key]:
                del self._store[key]
                del self._expiry[key]
                return None
        return self._store.get(key)

    async def set(self, key: str, value: str, ex=None) -> bool:
        """设置缓存值。"""
        self._store[key] = value
        return True

    async def setex(self, key: str, ttl: int, value: str) -> bool:
        """设置带过期时间的缓存。"""
        self._store[key] = value
        self._expiry[key] = time.time() + ttl
        return True

    async def delete(self, *keys) -> bool:
        """删除缓存。"""
        for key in keys:
            self._store.pop(key, None)
            self._expiry.pop(key, None)
        return True

    async def sadd(self, key: str, value: str) -> bool:
        """向集合添加成员。"""
        if key not in self._sets:
            self._sets[key] = set()
        self._sets[key].add(value)
        return True

    async def srem(self, key: str, value: str) -> bool:
        """从集合移除成员。"""
        if key in self._sets:
            self._sets[key].discard(value)
        return True

    async def smembers(self, key: str) -> set[str]:
        """获取集合所有成员。"""
        return self._sets.get(key, set())

    async def expire(self, key: str, ttl: int) -> bool:
        """设置过期时间。"""
        self._expiry[key] = time.time() + ttl
        return True

    async def incr(self, key: str) -> int:
        """递增计数器。"""
        count = int(self._store.get(key, "0"))
        count += 1
        self._store[key] = str(count)
        return count


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def mock_redis() -> MockRedis:
    return MockRedis()


@pytest.fixture
def mock_pool() -> MockDatabasePool:
    return MockDatabasePool()


@pytest.fixture
def profile_card(mock_redis: MockRedis, mock_pool: MockDatabasePool) -> ProfileCard:
    from app.memory.conflict_manager import ConflictManager
    pc = ProfileCard.__new__(ProfileCard)
    pc._redis = mock_redis
    pc._pool = mock_pool
    pc._conflict_manager = ConflictManager(mock_redis)
    yield pc


# ── Task 7.2: 槽位 upsert 幂等性 ────────────────────────────────────────


class TestUpsertIdempotency:
    """测试 upsert 幂等性：同一 key 重复 upsert 应更新而非重复插入。"""

    @pytest.mark.asyncio
    async def test_upsert_new_item(self, profile_card: ProfileCard):
        """首次 upsert 应创建新条目。"""
        result = await profile_card.upsert_item(
            tenant_id="t1",
            user_id="u1",
            slot=SlotType.FACT,
            item_key="birthday",
            item_value="1990-01-01",
            confidence=80,
            source=SourceType.DERIVED,
        )
        assert result.success is True
        assert result.item is not None
        assert result.item.version == 1
        assert result.item.item_value == "1990-01-01"
        assert result.item.confidence == 80

    @pytest.mark.asyncio
    async def test_upsert_same_key_updates(self, profile_card: ProfileCard):
        """同一 key 再次 upsert 应更新现有条目。"""
        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="color",
            item_value="red", confidence=50,
            source=SourceType.DERIVED,
        )

        result = await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="color",
            item_value="blue", confidence=70,
            source=SourceType.DERIVED,
        )
        assert result.success is True
        assert result.item is not None
        assert result.item.version == 2
        assert result.item.item_value == "blue"

        profile = await profile_card.get_profile("t1", "u1")
        assert len(profile) == 1
        assert profile[0].item_value == "blue"

    @pytest.mark.asyncio
    async def test_upsert_different_slots_independent(self, profile_card: ProfileCard):
        """不同 slot 的同一 key 应独立存储。"""
        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.PREFERENCE, item_key="lang",
            item_value="Python", confidence=90,
            source=SourceType.DERIVED,
        )
        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="lang",
            item_value="Spanish", confidence=60,
            source=SourceType.DERIVED,
        )
        profile = await profile_card.get_profile("t1", "u1")
        assert len(profile) == 2


# ── Task 7.3: version 递增逻辑 ──────────────────────────────────────────


class TestVersionIncrement:
    """测试 version 递增逻辑。"""

    @pytest.mark.asyncio
    async def test_version_starts_at_1(self, profile_card: ProfileCard):
        """新条目 version 应为 1。"""
        result = await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="city",
            item_value="Beijing",
        )
        assert result.item.version == 1

    @pytest.mark.asyncio
    async def test_version_increments_by_1(self, profile_card: ProfileCard):
        """每次更新 version 应 +1。"""
        for i in range(3):
            result = await profile_card.upsert_item(
                tenant_id="t1", user_id="u1",
                slot=SlotType.FACT, item_key="counter",
                item_value=f"val_{i}",
            )
            assert result.item.version == i + 1

    @pytest.mark.asyncio
    async def test_version_preserved_across_sessions(self, profile_card: ProfileCard):
        """version 应在 upsert 之间保持。"""
        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="persistent",
            item_value="first",
        )
        result = await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="persistent",
            item_value="second",
        )
        assert result.item.version == 2


# ── Task 7.4: 冲突规则 ──────────────────────────────────────────────────


class TestConflictRules:
    """测试冲突规则：user_confirmed 不覆盖、derived 覆盖。"""

    @pytest.mark.asyncio
    async def test_derived_overwrites_derived(self, profile_card: ProfileCard):
        """derived 条目可被 derived 覆盖。"""
        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="job",
            item_value="engineer", source=SourceType.DERIVED,
        )
        result = await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="job",
            item_value="doctor", source=SourceType.DERIVED,
        )
        assert result.success is True
        assert result.conflict is None
        assert result.item.item_value == "doctor"

    @pytest.mark.asyncio
    async def test_tool_written_overwrites_derived(self, profile_card: ProfileCard):
        """tool_written 条目可覆盖 derived 条目。"""
        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="score",
            item_value=85, source=SourceType.DERIVED,
        )
        result = await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="score",
            item_value=92, source=SourceType.TOOL_WRITTEN,
        )
        assert result.success is True
        assert result.conflict is None

    @pytest.mark.asyncio
    async def test_user_confirmed_blocks_derived(self, profile_card: ProfileCard):
        """user_confirmed 条目不可被 derived 覆盖。"""
        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="email",
            item_value="user@example.com",
            source=SourceType.USER_CONFIRMED,
        )
        result = await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="email",
            item_value="other@example.com",
            source=SourceType.DERIVED,
        )
        assert result.success is False
        assert result.conflict is not None
        assert isinstance(result.conflict, ConflictRef)
        assert result.conflict.old_value == "user@example.com"
        assert result.conflict.new_value == "other@example.com"
        assert result.conflict.slot == SlotType.FACT
        assert result.conflict.item_key == "email"

    @pytest.mark.asyncio
    async def test_user_confirmed_blocks_tool_written(self, profile_card: ProfileCard):
        """user_confirmed 条目不可被 tool_written 覆盖。"""
        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.IDENTITY, item_key="name",
            item_value="Alice",
            source=SourceType.USER_CONFIRMED,
        )
        result = await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.IDENTITY, item_key="name",
            item_value="Bob",
            source=SourceType.TOOL_WRITTEN,
        )
        assert result.success is False
        assert result.conflict is not None
        assert result.conflict.old_value == "Alice"

    @pytest.mark.asyncio
    async def test_user_confirmed_allows_user_confirmed(self, profile_card: ProfileCard):
        """user_confirmed 条目可被 user_confirmed 覆盖。"""
        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.PREFERENCE, item_key="theme",
            item_value="dark",
            source=SourceType.USER_CONFIRMED,
        )
        result = await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.PREFERENCE, item_key="theme",
            item_value="light",
            source=SourceType.USER_CONFIRMED,
        )
        assert result.success is True
        assert result.conflict is None
        assert result.item.item_value == "light"


# ── Task 7.5: Redis 缓存穿透失效 ────────────────────────────────────────


class TestCacheInvalidation:
    """测试 Redis 缓存穿透与失效逻辑。"""

    @pytest.mark.asyncio
    async def test_get_profile_populates_cache(self, profile_card: ProfileCard, mock_redis: MockRedis):
        """get_profile 应将数据回填到缓存。"""
        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="test",
            item_value="value",
        )
        # 需要 get_profile 才会回填缓存
        await profile_card.get_profile("t1", "u1")
        cache_key = ProfileCard._cache_key("t1", "u1")
        cached = await mock_redis.get(cache_key)
        assert cached is not None

    @pytest.mark.asyncio
    async def test_cache_hit_returns_data(self, profile_card: ProfileCard, mock_redis: MockRedis):
        """缓存命中时应直接返回数据。"""
        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="cached",
            item_value="hit",
        )
        result = await profile_card.get_profile("t1", "u1")
        assert len(result) == 1
        assert result[0].item_value == "hit"

    @pytest.mark.asyncio
    async def test_upsert_invalidates_cache(self, profile_card: ProfileCard, mock_redis: MockRedis):
        """upsert 应失效缓存。"""
        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="inv_test",
            item_value="old",
        )
        # 先 get_profile 填充缓存
        await profile_card.get_profile("t1", "u1")
        cache_key = ProfileCard._cache_key("t1", "u1")
        assert await mock_redis.get(cache_key) is not None

        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="inv_test",
            item_value="new",
        )
        assert await mock_redis.get(cache_key) is None

    @pytest.mark.asyncio
    async def test_delete_invalidates_cache(self, profile_card: ProfileCard, mock_redis: MockRedis):
        """delete 应失效缓存。"""
        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="del_test",
            item_value="val",
        )
        # 先 get_profile 填充缓存
        await profile_card.get_profile("t1", "u1")
        cache_key = ProfileCard._cache_key("t1", "u1")
        assert await mock_redis.get(cache_key) is not None

        await profile_card.delete_item("t1", "u1", SlotType.FACT, "del_test")
        assert await mock_redis.get(cache_key) is None

    @pytest.mark.asyncio
    async def test_cache_ttl_expiry(self, mock_redis: MockRedis):
        """缓存应在 TTL 过期后失效。"""
        cache_key = ProfileCard._cache_key("t1", "u1")
        await mock_redis.setex(cache_key, CACHE_TTL, '[{"slot":"fact","item_key":"k"}]')
        assert await mock_redis.get(cache_key) is not None

        mock_redis._expiry[cache_key] = time.time() - 1
        assert await mock_redis.get(cache_key) is None

    @pytest.mark.asyncio
    async def test_corrupted_cache_fallback(self, profile_card: ProfileCard, mock_redis: MockRedis):
        """缓存损坏时应回退到数据库。"""
        cache_key = ProfileCard._cache_key("t1", "u1")
        await mock_redis.setex(cache_key, 60, "corrupted_json")

        result = await profile_card.get_profile("t1", "u1")
        assert isinstance(result, list)


# ── Task 7.6: 200 条目淘汰逻辑 ──────────────────────────────────────────


class TestEviction:
    """测试 200 条目软限淘汰逻辑。"""

    @pytest.mark.asyncio
    async def test_evict_below_limit_no_change(self, profile_card: ProfileCard):
        """条目数少于限制时不应淘汰。"""
        for i in range(10):
            await profile_card.upsert_item(
                tenant_id="t1", user_id="u1",
                slot=SlotType.FACT, item_key=f"item_{i}",
                item_value=f"val_{i}", confidence=50,
            )
        evicted = await profile_card.evict_over_limit("t1")
        assert evicted == 0

    @pytest.mark.asyncio
    async def test_evict_removes_lowest_score(self, profile_card: ProfileCard):
        """超限时应淘汰低置信度条目。"""
        for i in range(205):
            conf = 10 + (i % 5) * 20
            await profile_card.upsert_item(
                tenant_id="t1", user_id="u1",
                slot=SlotType.FACT, item_key=f"item_{i}",
                item_value=f"val_{i}", confidence=conf,
            )
        evicted = await profile_card.evict_over_limit("t1")
        assert evicted > 0

    @pytest.mark.asyncio
    async def test_evict_preserves_user_confirmed(self, profile_card: ProfileCard):
        """淘汰时应保留 user_confirmed 条目。"""
        for i in range(10):
            await profile_card.upsert_item(
                tenant_id="t1", user_id="u1",
                slot=SlotType.IDENTITY, item_key=f"confirmed_{i}",
                item_value=f"val_{i}", confidence=100,
                source=SourceType.USER_CONFIRMED,
            )
        for i in range(200):
            await profile_card.upsert_item(
                tenant_id="t1", user_id="u1",
                slot=SlotType.FACT, item_key=f"derived_{i}",
                item_value=f"val_{i}", confidence=50,
                source=SourceType.DERIVED,
            )

        await profile_card.evict_over_limit("t1")
        profile = await profile_card.get_profile("t1", "u1")
        confirmed_keys = [p.item_key for p in profile if p.source == SourceType.USER_CONFIRMED]
        for i in range(10):
            assert f"confirmed_{i}" in confirmed_keys

    @pytest.mark.asyncio
    async def test_evict_specific_tenant_only(self, profile_card: ProfileCard):
        """指定 tenant_id 时应只处理该租户。"""
        for i in range(205):
            await profile_card.upsert_item(
                tenant_id="t1", user_id="u1",
                slot=SlotType.FACT, item_key=f"t1_item_{i}",
                item_value=f"val_{i}", confidence=50,
            )
        for i in range(10):
            await profile_card.upsert_item(
                tenant_id="t2", user_id="u2",
                slot=SlotType.FACT, item_key=f"t2_item_{i}",
                item_value=f"val_{i}", confidence=50,
            )

        await profile_card.evict_over_limit("t1")
        profile_t2 = await profile_card.get_profile("t2", "u2")
        assert len(profile_t2) == 10

    @pytest.mark.asyncio
    async def test_evict_all_tenants(self, profile_card: ProfileCard):
        """不指定 tenant_id 时应处理所有超限用户。"""
        for i in range(205):
            await profile_card.upsert_item(
                tenant_id="t1", user_id="u1",
                slot=SlotType.FACT, item_key=f"t1_item_{i}",
                item_value=f"val_{i}", confidence=50,
            )
        for i in range(210):
            await profile_card.upsert_item(
                tenant_id="t2", user_id="u2",
                slot=SlotType.FACT, item_key=f"t2_item_{i}",
                item_value=f"val_{i}", confidence=50,
            )

        evicted = await profile_card.evict_over_limit()
        assert evicted > 0


# ── 补充测试 ────────────────────────────────────────────────────────────


class TestDeleteItem:
    """测试 delete_item 操作。"""

    @pytest.mark.asyncio
    async def test_delete_existing_item(self, profile_card: ProfileCard):
        """删除已存在的条目应返回 True。"""
        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="to_delete",
            item_value="bye",
        )
        result = await profile_card.delete_item("t1", "u1", SlotType.FACT, "to_delete")
        assert result is True

        profile = await profile_card.get_profile("t1", "u1")
        assert len(profile) == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent_item(self, profile_card: ProfileCard):
        """删除不存在的条目应返回 False。"""
        result = await profile_card.delete_item("t1", "u1", SlotType.FACT, "ghost")
        assert result is False


class TestArchiveLowConfidence:
    """测试归档低置信度条目。"""

    @pytest.mark.asyncio
    async def test_archive_removes_old_low_confidence(self, profile_card: ProfileCard):
        """归档应删除 180 天前引用且置信度 < 50 的条目。"""
        now = time.time()
        old_threshold = now - 180 * 24 * 3600

        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="low_conf",
            item_value="old_data", confidence=30,
            source=SourceType.DERIVED,
        )

        pc = profile_card
        pool = pc._pool
        key = ("t1", "u1", "fact", "low_conf")
        if key in pool._items:
            pool._items[key]["last_referenced_at"] = old_threshold - 86400

        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="high_conf",
            item_value="important", confidence=80,
            source=SourceType.DERIVED,
        )
        key2 = ("t1", "u1", "fact", "high_conf")
        if key2 in pool._items:
            pool._items[key2]["last_referenced_at"] = now - 1000

        archived = await profile_card.archive_low_confidence()
        assert archived >= 1

        profile = await profile_card.get_profile("t1", "u1")
        keys = [p.item_key for p in profile]
        assert "high_conf" in keys
        assert "low_conf" not in keys

    @pytest.mark.asyncio
    async def test_archive_preserves_user_confirmed(self, profile_card: ProfileCard):
        """归档应保留 user_confirmed 条目。"""
        now = time.time()
        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.IDENTITY, item_key="preserve_me",
            item_value="important", confidence=30,
            source=SourceType.USER_CONFIRMED,
        )
        pc = profile_card
        pool = pc._pool
        key = ("t1", "u1", "identity", "preserve_me")
        if key in pool._items:
            pool._items[key]["last_referenced_at"] = now - 200 * 86400

        await profile_card.archive_low_confidence()
        profile = await profile_card.get_profile("t1", "u1")
        keys = [p.item_key for p in profile]
        assert "preserve_me" in keys


class TestGetProfile:
    """测试 get_profile 方法。"""

    @pytest.mark.asyncio
    async def test_get_profile_empty(self, profile_card: ProfileCard):
        """获取空档案卡应返回空列表。"""
        profile = await profile_card.get_profile("new_tenant", "new_user")
        assert profile == []

    @pytest.mark.asyncio
    async def test_get_profile_sorted(self, profile_card: ProfileCard):
        """档案卡应按 slot + item_key 排序。"""
        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="z_item",
            item_value="z",
        )
        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.IDENTITY, item_key="a_item",
            item_value="a",
        )
        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.PREFERENCE, item_key="m_item",
            item_value="m",
        )
        profile = await profile_card.get_profile("t1", "u1")
        # 按 slot 字符串值排序: fact < identity < preference (按字母序)
        slots = [p.slot for p in profile]
        assert slots == [SlotType.FACT, SlotType.IDENTITY, SlotType.PREFERENCE]

    @pytest.mark.asyncio
    async def test_get_profile_isolated_by_tenant(self, profile_card: ProfileCard):
        """不同租户的档案卡应隔离。"""
        await profile_card.upsert_item(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, item_key="shared",
            item_value="tenant1_val",
        )
        await profile_card.upsert_item(
            tenant_id="t2", user_id="u1",
            slot=SlotType.FACT, item_key="shared",
            item_value="tenant2_val",
        )
        profile1 = await profile_card.get_profile("t1", "u1")
        profile2 = await profile_card.get_profile("t2", "u1")
        assert len(profile1) == 1
        assert profile1[0].item_value == "tenant1_val"
        assert len(profile2) == 1
        assert profile2[0].item_value == "tenant2_val"
