"""Task 19: 单元测试 - L3 语义查询。

测试 MemoryService.recall 方法的 L2+L3 合并、去重逻辑和 fail-soft 降级。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest

from app.memory.layers import (
    RecalledItem,
    RecallResult,
    Scope,
    SlotType,
    SourceType,
)
from app.memory.profile_card import ProfileCard
from app.memory.service import MemoryService
from app.memory.session_meta import SessionMetaStore

# ── Mock / Fake 基础设施 ─────────────────────────────────────────────────


class MockRedis:
    """模拟 Redis 连接。"""

    def __init__(self):
        self._store: dict[str, str] = {}
        self._expiry: dict[str, float] = {}
        self._sets: dict[str, set[str]] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ex=None) -> bool:
        self._store[key] = value
        return True

    async def setex(self, key: str, ttl: int, value: str) -> bool:
        self._store[key] = value
        self._expiry[key] = time.time() + ttl
        return True

    async def delete(self, *keys) -> int:
        for key in keys:
            self._store.pop(key, None)
            self._expiry.pop(key, None)
        return len(keys)

    async def sadd(self, key: str, value: str) -> bool:
        if key not in self._sets:
            self._sets[key] = set()
        self._sets[key].add(value)
        return True

    async def srem(self, key: str, value: str) -> bool:
        if key in self._sets:
            self._sets[key].discard(value)
        return True

    async def smembers(self, key: str) -> set[str]:
        return self._sets.get(key, set())

    async def expire(self, key: str, ttl: int) -> bool:
        self._expiry[key] = time.time() + ttl
        return True

    async def incr(self, key: str) -> int:
        count = int(self._store.get(key, "0"))
        count += 1
        self._store[key] = str(count)
        return count


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
        # 处理 _get_item 查询
        if "SELECT slot, item_key" in query and "FROM user_memory_profile" in query:
            if "WHERE tenant_id = $1 AND user_id = $2" in query and "slot = $3" in query:
                tenant_id, user_id, slot, item_key = args[0], args[1], args[2], args[3]
                key = self._key(tenant_id, user_id, slot, item_key)
                item = self._items.get(key)
                return self._make_row(item) if item else None
        return None

    async def execute(self, query, *args):
        if "INSERT INTO user_memory_profile" in query:
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
            tenant_id, user_id, slot, item_key = args[0], args[1], args[2], args[3]
            key = self._key(tenant_id, user_id, slot, item_key)
            if key in self._items:
                del self._items[key]
                return "DELETE 1"
            return "DELETE 0"
        return "OK"


class FakeSummaryStore:
    """模拟 SummaryStore 接口。"""

    def __init__(
        self,
        recall_result: list[RecalledItem] | None = None,
        should_fail: bool = False,
    ):
        self._recall_result = recall_result or []
        self._should_fail = should_fail
        self.last_recall_kwargs: dict = {}
        self.recall_called = False

    async def recall(
        self,
        scope: Scope,
        query: str = "",
        top_k: int = 5,
    ) -> list[RecalledItem]:
        """模拟 recall 方法。"""
        self.recall_called = True
        self.last_recall_kwargs = {
            "scope": scope,
            "query": query,
            "top_k": top_k,
        }
        if self._should_fail:
            raise RuntimeError("L3 recall failed (Milvus unavailable)")
        return self._recall_result


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
def service_with_l3(profile_card):
    """创建带 L3 的 MemoryService。"""
    session_meta_store = SessionMetaStore()
    svc = MemoryService(
        session_meta_store=session_meta_store,
        profile_card=profile_card,
        summary_store=FakeSummaryStore(),
    )
    return svc


@pytest.fixture
def service_without_l3(profile_card):
    """创建无 L3 的 MemoryService。"""
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

    import asyncio

    async def _seed():
        # 直接使用 profile_card 写入数据
        await profile_card.upsert_item(
            tenant_id="t1",
            user_id="u1",
            slot=SlotType.FACT,
            item_key="pref_lang",
            item_value="Python",
            confidence=90,
            source=SourceType.USER_CONFIRMED,
        )

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_seed())
    finally:
        loop.close()

    return svc


# ── 辅助函数 ──────────────────────────────────────────────────────────────


def _make_recalled(
    item_id: str,
    content: str,
    turn_range: tuple[int, int],
    score: float = 0.9,
) -> RecalledItem:
    """创建 RecalledItem 实例。"""
    return RecalledItem(
        id=item_id,
        content=content,
        topics=[],
        entities={},
        turn_range=turn_range,
        session_id="sess-001",
        access_count=0,
        last_accessed_at=0.0,
        created_at=time.time(),
        score=score,
    )


# ── 19.1: L2 + L3 合并测试 ──────────────────────────────────────────────


class TestL2L3Merge:
    """测试 recall 方法 L2 + L3 合并。"""

    @pytest.mark.asyncio
    async def test_returns_both_l2_and_l3(self, seeded_service):
        """recall 应同时返回 L2 档案卡和 L3 摘要。"""
        svc = seeded_service
        time.time()
        fake_l3 = FakeSummaryStore(recall_result=[
            _make_recalled("m1", "历史对话摘要 1", (0, 10), score=0.95),
            _make_recalled("m2", "历史对话摘要 2", (20, 30), score=0.9),
        ])
        svc._summary_store = fake_l3

        result = await svc.recall("t1", "u1", query="test query")

        # L2 档案卡应有内容
        assert result.profile_block is not None
        assert "Python" in result.profile_block

        # L3 摘要应有 2 条
        assert len(result.summary_items) == 2
        assert result.summary_items[0].id == "m1"
        assert result.summary_items[1].id == "m2"

    @pytest.mark.asyncio
    async def test_l3_disabled_returns_only_l2(self, seeded_service):
        """L3 未启用时只返回 L2。"""
        svc = seeded_service
        svc._summary_store = None  # 禁用 L3

        result = await svc.recall("t1", "u1", query="test")

        assert result.profile_block is not None
        assert result.summary_items == []

    @pytest.mark.asyncio
    async def test_no_query_skips_l3(self, seeded_service):
        """无 query 时跳过 L3 检索。"""
        svc = seeded_service
        fake_l3 = FakeSummaryStore(recall_result=[
            _make_recalled("m1", "摘要", (0, 5)),
        ])
        svc._summary_store = fake_l3

        result = await svc.recall("t1", "u1", query="")  # 空 query

        # L3 不应被调用
        assert fake_l3.recall_called is False
        assert result.summary_items == []
        assert result.profile_block is not None

    @pytest.mark.asyncio
    async def test_top_k_default_value(self, service_with_l3):
        """默认 top_k=5 应传递给 summary_store。"""
        svc = service_with_l3
        fake_l3 = FakeSummaryStore()
        svc._summary_store = fake_l3

        await svc.recall("t1", "u1", query="test")

        assert fake_l3.last_recall_kwargs["top_k"] == 5

    @pytest.mark.asyncio
    async def test_top_k_custom_value(self, service_with_l3):
        """自定义 top_k 应正确传递。"""
        svc = service_with_l3
        fake_l3 = FakeSummaryStore()
        svc._summary_store = fake_l3

        await svc.recall("t1", "u1", query="test", top_k=10)

        assert fake_l3.last_recall_kwargs["top_k"] == 10

    @pytest.mark.asyncio
    async def test_l2_profile_empty_but_l3_has_results(self, service_with_l3):
        """L2 为空但 L3 有结果时应正常返回。"""
        svc = service_with_l3
        time.time()
        fake_l3 = FakeSummaryStore(recall_result=[
            _make_recalled("m1", "历史摘要", (0, 5)),
        ])
        svc._summary_store = fake_l3

        result = await svc.recall("t1", "u1", query="test")

        # L2 应为空（无档案数据）
        assert result.profile_block is not None
        # L3 应有结果
        assert len(result.summary_items) == 1


# ── 19.2: 去重逻辑（摘要与窗口重叠）测试 ──────────────────────────────


class TestTurnRangeDedup:
    """测试摘要与窗口重叠的去重逻辑。"""

    @pytest.mark.asyncio
    async def test_exclude_overlapping_range(self, service_with_l3):
        """应排除与 exclude_turn_range 重叠的摘要。"""
        svc = service_with_l3
        time.time()
        fake_l3 = FakeSummaryStore(recall_result=[
            _make_recalled("overlap", "重叠摘要", (10, 20), score=0.95),
            _make_recalled("no_overlap", "不重叠摘要", (50, 60), score=0.9),
        ])
        svc._summary_store = fake_l3

        result = await svc.recall(
            "t1", "u1", query="test",
            exclude_turn_range=(15, 25),  # 与 (10,20) 重叠
        )

        assert len(result.summary_items) == 1
        assert result.summary_items[0].id == "no_overlap"

    @pytest.mark.asyncio
    async def test_multiple_overlapping_ranges(self, service_with_l3):
        """多个重叠范围应全部排除。"""
        svc = service_with_l3
        time.time()
        fake_l3 = FakeSummaryStore(recall_result=[
            _make_recalled("m1", "摘要1", (0, 10), score=0.95),
            _make_recalled("m2", "摘要2", (15, 25), score=0.9),
            _make_recalled("m3", "摘要3", (40, 50), score=0.85),
        ])
        svc._summary_store = fake_l3

        result = await svc.recall(
            "t1", "u1", query="test",
            exclude_turn_range=(5, 30),  # 与前两个重叠
        )

        assert len(result.summary_items) == 1
        assert result.summary_items[0].id == "m3"

    @pytest.mark.asyncio
    async def test_no_exclude_returns_all(self, service_with_l3):
        """无 exclude_turn_range 时应返回所有结果。"""
        svc = service_with_l3
        time.time()
        fake_l3 = FakeSummaryStore(recall_result=[
            _make_recalled("m1", "摘要1", (0, 10)),
            _make_recalled("m2", "摘要2", (20, 30)),
        ])
        svc._summary_store = fake_l3

        result = await svc.recall("t1", "u1", query="test")

        assert len(result.summary_items) == 2

    @pytest.mark.asyncio
    async def test_boundary_overlap_excluded(self, service_with_l3):
        """边界相邻的范围应视为重叠并排除。"""
        svc = service_with_l3
        time.time()
        fake_l3 = FakeSummaryStore(recall_result=[
            _make_recalled("boundary", "边界摘要", (10, 20)),
            _make_recalled("safe", "安全摘要", (30, 40)),  # 不与 (20,25) 重叠
        ])
        svc._summary_store = fake_l3

        result = await svc.recall(
            "t1", "u1", query="test",
            exclude_turn_range=(20, 25),  # 边界与 (10,20) 重叠
        )

        # (10,20) 与 (20,25) 边界相邻，应视为重叠
        # (30,40) 与 (20,25) 不重叠，应保留
        assert len(result.summary_items) == 1
        assert result.summary_items[0].id == "safe"

    def test_is_turn_range_overlap_static(self):
        """测试 _is_turn_range_overlap 静态方法的各种边界情况。"""
        # 完全重叠
        assert MemoryService._is_turn_range_overlap((10, 20), (15, 25)) is True
        assert MemoryService._is_turn_range_overlap((10, 20), (5, 15)) is True
        assert MemoryService._is_turn_range_overlap((10, 20), (10, 20)) is True

        # 边界相邻
        assert MemoryService._is_turn_range_overlap((10, 20), (20, 30)) is True

        # 不重叠
        assert MemoryService._is_turn_range_overlap((10, 20), (25, 30)) is False
        assert MemoryService._is_turn_range_overlap((10, 20), (0, 5)) is False

        # 一方包含另一方
        assert MemoryService._is_turn_range_overlap((10, 50), (20, 30)) is True
        assert MemoryService._is_turn_range_overlap((20, 30), (10, 50)) is True


# ── 19.3: fail-soft 降级测试 ──────────────────────────────────────────


class TestFailSoftDegradation:
    """测试 fail-soft 降级（Milvus 异常）。"""

    @pytest.mark.asyncio
    async def test_l3_exception_returns_empty(self, service_with_l3):
        """L3 抛出异常时应返回空 L3 列表。"""
        svc = service_with_l3
        fake_l3 = FakeSummaryStore(should_fail=True)
        svc._summary_store = fake_l3

        result = await svc.recall("t1", "u1", query="test")

        assert result.summary_items == []

    @pytest.mark.asyncio
    async def test_l3_exception_preserves_l2(self, service_with_l3):
        """L3 异常时 L2 仍应正常返回。"""
        svc = service_with_l3
        # 预填 L2 数据
        await svc.update_profile(
            tenant_id="t1", user_id="u1",
            slot=SlotType.PREFERENCE, item_key="lang",
            item_value="Python", confidence=95,
            source=SourceType.USER_CONFIRMED,
        )

        fake_l3 = FakeSummaryStore(should_fail=True)
        svc._summary_store = fake_l3

        result = await svc.recall("t1", "u1", query="test")

        # L2 应正常返回
        assert result.profile_block is not None
        assert "Python" in result.profile_block
        # L3 应为空
        assert result.summary_items == []

    @pytest.mark.asyncio
    async def test_l2_exception_preserves_l3(self, service_with_l3):
        """L2 异常时 L3 仍应正常返回。"""
        svc = service_with_l3
        time.time()
        fake_l3 = FakeSummaryStore(recall_result=[
            _make_recalled("m1", "摘要内容", (0, 5)),
        ])
        svc._summary_store = fake_l3

        # 模拟 L2 不可用
        svc._profile_card = None

        result = await svc.recall("t1", "u1", query="test")

        # L3 应正常返回
        assert len(result.summary_items) == 1
        # L2 应有降级提示
        assert result.profile_block is not None

    @pytest.mark.asyncio
    async def test_both_l2_l3_fail_returns_default(self, service_with_l3):
        """L2 和 L3 都失败时应返回默认值。"""
        svc = service_with_l3
        fake_l3 = FakeSummaryStore(should_fail=True)
        svc._summary_store = fake_l3
        svc._profile_card = None

        result = await svc.recall("t1", "u1", query="test")

        # 应有合理的默认值
        assert result.profile_block is not None
        assert result.summary_items == []

    @pytest.mark.asyncio
    async def test_summary_store_is_none_no_error(self, service_without_l3):
        """summary_store 为 None 时不应抛出异常。"""
        svc = service_without_l3

        result = await svc.recall("t1", "u1", query="test")

        assert isinstance(result, RecallResult)
        assert result.summary_items == []

    @pytest.mark.asyncio
    async def test_recall_result_type(self, service_with_l3):
        """recall 应返回 RecallResult 类型。"""
        svc = service_with_l3
        result = await svc.recall("t1", "u1")

        assert isinstance(result, RecallResult)
        assert hasattr(result, "profile_block")
        assert hasattr(result, "summary_items")


# ── 多租户隔离测试 ──────────────────────────────────────────────────────


class TestL3TenantIsolation:
    """测试 L3 语义查询的多租户隔离。"""

    @pytest.mark.asyncio
    async def test_different_tenants_separate_l3(self, service_with_l3):
        """不同租户应使用独立的 L3 查询。"""
        svc = service_with_l3
        fake_l3 = FakeSummaryStore(recall_result=[
            _make_recalled("m1", "租户1的摘要", (0, 5)),
        ])
        svc._summary_store = fake_l3

        # 租户 1 查询
        await svc.recall("tenant1", "user1", query="test")

        # 验证传递了正确的 tenant_id
        assert fake_l3.last_recall_kwargs["scope"].tenant_id == "tenant1"

        # 租户 2 查询
        await svc.recall("tenant2", "user2", query="test")

        assert fake_l3.last_recall_kwargs["scope"].tenant_id == "tenant2"


# ── 边界条件测试 ──────────────────────────────────────────────────────


class TestL3EdgeCases:
    """测试 L3 语义查询边界条件。"""

    @pytest.mark.asyncio
    async def test_empty_recall_result(self, service_with_l3):
        """L3 返回空列表时应正常处理。"""
        svc = service_with_l3
        fake_l3 = FakeSummaryStore(recall_result=[])
        svc._summary_store = fake_l3

        result = await svc.recall("t1", "u1", query="test")

        assert result.summary_items == []

    @pytest.mark.asyncio
    async def test_large_top_k(self, service_with_l3):
        """大 top_k 值应正确传递。"""
        svc = service_with_l3
        fake_l3 = FakeSummaryStore()
        svc._summary_store = fake_l3

        await svc.recall("t1", "u1", query="test", top_k=100)

        assert fake_l3.last_recall_kwargs["top_k"] == 100

    @pytest.mark.asyncio
    async def test_recall_with_special_characters(self, service_with_l3):
        """包含特殊字符的 query 应正常处理。"""
        svc = service_with_l3
        fake_l3 = FakeSummaryStore()
        svc._summary_store = fake_l3

        result = await svc.recall("t1", "u1", query="test <script> & \"quotes\"")

        assert isinstance(result, RecallResult)
        assert fake_l3.last_recall_kwargs["query"] == "test <script> & \"quotes\""

    @pytest.mark.asyncio
    async def test_recall_with_unicode_query(self, service_with_l3):
        """包含 Unicode 的 query 应正常处理。"""
        svc = service_with_l3
        fake_l3 = FakeSummaryStore()
        svc._summary_store = fake_l3

        result = await svc.recall("t1", "u1", query="学习 Python 编程 🍀")

        assert isinstance(result, RecallResult)
        assert fake_l3.last_recall_kwargs["query"] == "学习 Python 编程 🍀"


# ── 性能相关测试 ──────────────────────────────────────────────────────


class TestL3Performance:
    """测试 L3 语义查询性能相关行为。"""

    @pytest.mark.asyncio
    async def test_recall_returns_quickly_without_query(self, service_with_l3):
        """无 query 时应快速返回（不调用 L3）。"""
        svc = service_with_l3
        fake_l3 = FakeSummaryStore()
        svc._summary_store = fake_l3

        result = await svc.recall("t1", "u1", query="")

        # L3 不应被调用
        assert fake_l3.recall_called is False
        # 应立即返回
        assert isinstance(result, RecallResult)

    @pytest.mark.asyncio
    async def test_multiple_recalls_same_session(self, service_with_l3):
        """同一会话多次 recall 应独立工作。"""
        svc = service_with_l3
        time.time()
        fake_l3 = FakeSummaryStore(recall_result=[
            _make_recalled("m1", "第一次摘要", (0, 5)),
        ])
        svc._summary_store = fake_l3


        # 第一次 recall
        result1 = await svc.recall("t1", "u1", query="test1")
        assert len(result1.summary_items) == 1

        # 第二次 recall（不同 query）
        fake_l3._recall_result = [
            _make_recalled("m2", "第二次摘要", (10, 15)),
        ]
        result2 = await svc.recall("t1", "u1", query="test2")
        assert len(result2.summary_items) == 1
        assert result2.summary_items[0].id == "m2"
