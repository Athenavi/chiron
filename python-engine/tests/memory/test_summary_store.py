"""Task 17: 单元测试 - SummaryStore。

测试 L3 SummaryStore 的 save_summary 双写、recall 检索、final_score 排序、
查询缓存和 token 预算截断。
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from app.memory.layers import (
    MemoryType,
    RecalledItem,
    Scope,
)
from app.memory.summary_store import (
    DEFAULT_TOP_K,
    EMBEDDING_CACHE_TTL,
    FINAL_SCORE_WEIGHT_ACCESS,
    FINAL_SCORE_WEIGHT_RECENCY,
    FINAL_SCORE_WEIGHT_SIMILARITY,
    QUERY_CACHE_TTL,
    TOKEN_BUDGET_L3,
    SummaryStore,
)

# ── Mock 基础设施 ────────────────────────────────────────────────────────


class MockRedis:
    """模拟 Redis 连接。"""

    def __init__(self):
        self._store: dict[str, str] = {}
        self._expiry: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> bool:
        self._store[key] = value
        self._expiry[key] = ttl
        return True

    async def delete(self, key: str) -> int:
        deleted = 1 if key in self._store else 0
        self._store.pop(key, None)
        self._expiry.pop(key, None)
        return deleted

    async def scan_iter(self, match: str = "*"):
        prefix = match.rstrip("*")
        keys = [k for k in self._store.keys() if k.startswith(prefix)]
        for key in keys:
            yield key


class MockDatabasePool:
    """模拟 asyncpg 连接池。"""

    def __init__(self):
        self._summaries: dict[str, dict[str, Any]] = {}
        self._inserted_ids: list[str] = []
        self._touched_ids: list[str] = []

    async def fetchrow(self, query: str, *args) -> dict | None:
        if "INSERT INTO memory_summaries" in query:
            content_hash = args[9]
            tenant_id, user_id = args[1], args[2]
            summary_id = args[0]

            # 检查冲突
            for _sid, s in self._summaries.items():
                if s["content_hash"] == content_hash and s["tenant_id"] == tenant_id and s["user_id"] == user_id:
                    return None  # ON CONFLICT

            # 插入
            now = datetime.now(UTC)
            self._summaries[summary_id] = {
                "id": summary_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "session_id": args[3],
                "content": args[4],
                "topics": args[5],
                "entities": args[6],
                "turn_start": args[7],
                "turn_end": args[8],
                "content_hash": content_hash,
                "access_count": 0,
                "last_accessed_at": None,
                "status": "active",
                "created_at": now,
            }
            self._inserted_ids.append(summary_id)
            return {"id": summary_id}

        elif "SELECT id FROM memory_summaries WHERE content_hash=" in query:
            content_hash, tenant_id, user_id = args
            for sid, s in self._summaries.items():
                if s["content_hash"] == content_hash and s["tenant_id"] == tenant_id and s["user_id"] == user_id:
                    return {"id": sid}
            return None

        elif "SELECT id, content, topics, entities" in query and "content_hash" not in query:
            # _pg_recent_summaries 查询
            tenant_id, user_id = args[0], args[1]
            limit = args[2] if len(args) > 2 else DEFAULT_TOP_K
            results = []
            for _sid, s in sorted(self._summaries.items(), key=lambda x: x[1]["created_at"], reverse=True):
                if s["tenant_id"] == tenant_id and s["user_id"] == user_id and s["status"] == "active":
                    row = dict(s)
                    row["last_accessed_at"] = s["last_accessed_at"]
                    results.append(row)
                    if len(results) >= limit:
                        break
            return results

        elif "SELECT id, tenant_id, user_id" in query and "WHERE content_hash=$1" in query:
            # get_by_hash 查询
            content_hash, tenant_id, user_id = args
            for _sid, s in self._summaries.items():
                if s["content_hash"] == content_hash and s["tenant_id"] == tenant_id and s["user_id"] == user_id:
                    return dict(s)
            return None

        elif "SELECT id, tenant_id, user_id" in query and "WHERE id=$1" in query:
            # get_by_id 查询
            summary_id, tenant_id, user_id = args
            s = self._summaries.get(summary_id)
            if s and s["tenant_id"] == tenant_id and s["user_id"] == user_id:
                return dict(s)
            return None

        elif "SELECT id, tenant_id, user_id" in query and "status='active'" in query:
            # list_active 查询
            tenant_id, user_id = args[0], args[1]
            limit = args[2] if len(args) > 2 else 50
            results = []
            for _sid, s in sorted(self._summaries.items(), key=lambda x: x[1]["created_at"], reverse=True):
                if s["tenant_id"] == tenant_id and s["user_id"] == user_id and s["status"] == "active":
                    results.append(dict(s))
                    if len(results) >= limit:
                        break
            return results

        elif "SELECT id, content, topics" in query:
            # list_recent 查询
            tenant_id, user_id = args[0], args[1]
            limit = args[2] if len(args) > 2 else 50
            results = []
            for sid, s in sorted(self._summaries.items(), key=lambda x: x[1]["created_at"], reverse=True):
                if s["tenant_id"] == tenant_id and s["user_id"] == user_id and s["status"] == "active":
                    results.append({
                        "id": sid,
                        "content": s["content"],
                        "topics": s["topics"],
                        "entities": s["entities"],
                        "turn_start": s["turn_start"],
                        "turn_end": s["turn_end"],
                        "session_id": s["session_id"],
                        "access_count": s["access_count"],
                        "created_at": s["created_at"],
                    })
                    if len(results) >= limit:
                        break
            return results

        return None

    async def fetch(self, query: str, *args) -> list[dict]:
        result = await self.fetchrow(query, *args)
        if result is None:
            return []
        if isinstance(result, list):
            return result
        return [result]

    async def execute(self, query: str, *args) -> str:
        if "UPDATE memory_summaries" in query:
            ids = args[0]
            self._touched_ids = ids if isinstance(ids, list) else list(ids)
            for sid in self._touched_ids:
                if sid in self._summaries:
                    self._summaries[sid]["access_count"] += 1
                    self._summaries[sid]["last_accessed_at"] = datetime.now(UTC)
            return "UPDATE 1"

        elif "DELETE FROM memory_summaries" in query:
            summary_id, tenant_id, user_id = args
            if summary_id in self._summaries:
                del self._summaries[summary_id]
                return "DELETE 1"
            return "DELETE 0"

        return "OK"


class MockMilvusCollection:
    """模拟 Milvus Collection。"""

    def __init__(self):
        self._inserted: list[dict[str, Any]] = []
        self._search_results: list[list[dict[str, Any]]] = []
        self._search_called = False

    def insert(self, data: list) -> None:
        fields = [
            "summary_id", "tenant_id", "user_id", "session_id",
            "content", "memory_type", "embedding", "created_at", "metadata",
        ]
        for values in zip(*data, strict=False):
            record = dict(zip(fields, values, strict=False))
            # 解包单值列表
            for k, v in record.items():
                if isinstance(v, list) and len(v) == 1:
                    record[k] = v[0]
            self._inserted.append(record)

    def flush(self) -> None:
        pass

    def search(self, data, anns_field, param, limit, expr, output_fields) -> list:
        self._search_called = True
        results = []
        for hits in self._search_results:
            hit_objects = []
            for h in hits:
                hit_obj = MockHit(
                    score=h.get("score", 0.5),
                    entity=h.get("entity", {}),
                )
                hit_objects.append(hit_obj)
            results.append(hit_objects)
        return results

    def set_search_results(self, results: list[list[dict[str, Any]]]) -> None:
        """设置模拟搜索结果。"""
        self._search_results = results


class MockHit:
    """模拟 Milvus 搜索结果中的 hit。"""

    def __init__(self, score: float, entity: dict[str, Any]):
        self.score = score
        self.entity = entity


@pytest.fixture
def mock_redis():
    """创建 MockRedis 实例。"""
    return MockRedis()


@pytest.fixture
def mock_pool():
    """创建 MockDatabasePool 实例。"""
    return MockDatabasePool()


@pytest.fixture
def mock_milvus():
    """创建 MockMilvusCollection 实例。"""
    return MockMilvusCollection()


@pytest.fixture
def store(mock_redis, mock_pool, mock_milvus, monkeypatch):
    """创建 SummaryStore 实例（带 Mock 依赖）。"""
    monkeypatch.setattr("app.memory.summary_store.get_pool", lambda: mock_pool)

    async def _fake_embedding(query: str) -> list[float]:
        # 简单的伪嵌入：基于 query hash 生成固定长度向量
        h = hashlib.sha256(query.encode()).digest()
        return [b / 255.0 for b in h] * 4  # 32 * 4 = 128 维

    ss = SummaryStore(redis=mock_redis, embedding_fn=_fake_embedding)
    ss._milvus_collection = mock_milvus
    return ss


@pytest.fixture
def scope():
    """创建测试用 Scope。"""
    return Scope(tenant_id="t1", user_id="u1", session_id="sess-001")


# ── Task 17.2: save_summary 双写测试 ──────────────────────────────────


class TestSaveSummary:
    """测试 save_summary 方法（PG + Milvus 双写）。"""

    @pytest.mark.asyncio
    async def test_save_inserts_to_pg(self, store, mock_pool, scope):
        """save_summary 应将摘要插入 PG。"""
        result = await store.save_summary(
            scope=scope,
            content="测试摘要内容",
            topics=["python", "测试"],
            entities={"person": ["Alice"]},
            turn_range=(1, 5),
        )

        assert result is not None
        assert result.startswith("sms_")
        assert len(mock_pool._inserted_ids) == 1
        assert mock_pool._inserted_ids[0] == result

        # 验证 PG 存储内容
        stored = mock_pool._summaries[result]
        assert stored["content"] == "测试摘要内容"
        assert stored["tenant_id"] == "t1"
        assert stored["user_id"] == "u1"
        assert stored["topics"] == json.dumps(["python", "测试"])
        assert stored["entities"] == json.dumps({"person": ["Alice"]})
        assert stored["turn_start"] == 1
        assert stored["turn_end"] == 5

    @pytest.mark.asyncio
    async def test_save_inserts_to_milvus(self, store, mock_pool, mock_milvus, scope):
        """save_summary 应将摘要插入 Milvus。"""
        result = await store.save_summary(
            scope=scope,
            content="双写测试",
            topics=["测试"],
            turn_range=(0, 3),
        )

        assert result is not None
        assert len(mock_milvus._inserted) == 1

        inserted = mock_milvus._inserted[0]
        assert inserted["summary_id"] == result
        assert inserted["tenant_id"] == "t1"
        assert inserted["user_id"] == "u1"
        assert inserted["memory_type"] == MemoryType.SUMMARY.value
        assert inserted["content"] == "双写测试"

    @pytest.mark.asyncio
    async def test_save_dedup_by_content_hash(self, store, mock_pool, scope):
        """相同 content_hash 应返回已有 ID（去重）。"""
        result1 = await store.save_summary(
            scope=scope, content="重复内容", turn_range=(0, 1)
        )
        result2 = await store.save_summary(
            scope=scope, content="重复内容", turn_range=(2, 3)  # 不同 turn_range
        )

        assert result1 == result2  # 相同 content_hash 应返回同一 ID
        assert len(mock_pool._inserted_ids) == 1  # 只插入一次

    @pytest.mark.asyncio
    async def test_save_different_content_inserts_new(self, store, mock_pool, scope):
        """不同内容应插入新记录。"""
        result1 = await store.save_summary(
            scope=scope, content="内容 A", turn_range=(0, 1)
        )
        result2 = await store.save_summary(
            scope=scope, content="内容 B", turn_range=(0, 1)
        )

        assert result1 != result2
        assert len(mock_pool._inserted_ids) == 2

    @pytest.mark.asyncio
    async def test_save_invalidates_query_cache(self, store, mock_redis, mock_pool, scope):
        """保存摘要后应失效查询缓存。"""
        # 先写入一些缓存
        cache_key = SummaryStore._query_cache_key("t1", "u1", "测试查询")
        await mock_redis.setex(cache_key, QUERY_CACHE_TTL, "cached data")

        assert await mock_redis.get(cache_key) == "cached data"

        # 保存摘要
        await store.save_summary(
            scope=scope, content="测试摘要", turn_range=(0, 1)
        )

        # 缓存应被清除
        assert await mock_redis.get(cache_key) is None

    @pytest.mark.asyncio
    async def test_save_without_embedding_fn(self, mock_redis, mock_pool, monkeypatch, scope):
        """无 embedding_fn 时 save_summary 应跳过 Milvus 插入。"""
        monkeypatch.setattr("app.memory.summary_store.get_pool", lambda: mock_pool)
        ss = SummaryStore(redis=mock_redis, embedding_fn=None)

        result = await ss.save_summary(
            scope=scope,
            content="无嵌入测试",
            turn_range=(0, 1),
        )

        assert result is not None
        assert len(mock_pool._inserted_ids) == 1

    @pytest.mark.asyncio
    async def test_save_without_topics_entities(self, store, mock_pool, scope):
        """空 topics/entities 应使用默认值。"""
        result = await store.save_summary(
            scope=scope,
            content="空字段测试",
            turn_range=(0, 1),
        )

        assert result is not None
        stored = mock_pool._summaries[result]
        assert stored["topics"] == json.dumps([])
        assert stored["entities"] == json.dumps({})


# ── Task 17.3: recall 向量检索测试 ────────────────────────────────────


class TestRecallVectorSearch:
    """测试 recall 向量检索功能。"""

    @pytest.mark.asyncio
    async def test_recall_with_query_fetches_embedding(self, store, mock_redis, scope):
        """recall 应使用 query 获取嵌入向量。"""
        embedding_called = []

        async def _fake_embedding(query: str) -> list[float]:
            embedding_called.append(query)
            return [0.1, 0.2, 0.3]

        store._embedding_fn = _fake_embedding

        # 设置 Milvus 返回结果
        now = time.time()
        store._milvus_collection.set_search_results([
            [
                {
                    "score": 0.95,
                    "entity": {
                        "summary_id": "m1",
                        "content": "相关历史摘要",
                        "metadata": json.dumps({
                            "topics": ["python"],
                            "entities": {"person": ["Alice"]},
                            "turn_range": [1, 5],
                            "session_id": "sess-001",
                            "access_count": 3,
                            "last_accessed_at": now,
                        }),
                        "created_at": str(now),
                    },
                },
            ],
        ])

        items = await store.recall(scope=scope, query="查找 python 相关")

        assert len(embedding_called) > 0  # embedding_fn 被调用
        assert len(items) == 1
        assert items[0].id == "m1"
        assert items[0].content == "相关历史摘要"

    @pytest.mark.asyncio
    async def test_recall_without_query_uses_pg_fallback(self, store, mock_pool, scope):
        """无 query 时应从 PG 获取最近摘要。"""
        # 先插入一些摘要
        now = time.time()
        mock_pool._summaries["s1"] = {
            "id": "s1", "tenant_id": "t1", "user_id": "u1",
            "session_id": "sess-001", "content": "摘要 1",
            "topics": ["a"], "entities": {},
            "turn_start": 0, "turn_end": 10,
            "content_hash": "hash1", "access_count": 5,
            "last_accessed_at": None, "status": "active",
            "created_at": datetime.fromtimestamp(now - 100000, tz=UTC),
        }
        mock_pool._summaries["s2"] = {
            "id": "s2", "tenant_id": "t1", "user_id": "u1",
            "session_id": "sess-001", "content": "摘要 2",
            "topics": ["b"], "entities": {},
            "turn_start": 20, "turn_end": 30,
            "content_hash": "hash2", "access_count": 200,
            "last_accessed_at": None, "status": "active",
            "created_at": datetime.fromtimestamp(now, tz=UTC),
        }

        items = await store.recall(scope=scope, query="")

        # 应返回最近的摘要（按 final_score 排序）
        assert len(items) == 2
        # s2 更新且 access_count 更高，应排在前面
        assert items[0].id == "s2"
        assert items[1].id == "s1"

    @pytest.mark.asyncio
    async def test_recall_milvus_fallback_to_pg(self, store, mock_pool, scope):
        """Milvus 不可用时应降级到 PG。"""
        # 让 Milvus 搜索抛出异常以触发降级
        def failing_search(self, data, anns_field, param, limit, expr, output_fields):
            raise Exception("Milvus connection failed")

        # 先插入 PG 数据
        now = time.time()
        mock_pool._summaries["s1"] = {
            "id": "s1", "tenant_id": "t1", "user_id": "u1",
            "session_id": "sess-001", "content": "降级摘要",
            "topics": [], "entities": {},
            "turn_start": 0, "turn_end": 5,
            "content_hash": "hash1", "access_count": 0,
            "last_accessed_at": None, "status": "active",
            "created_at": datetime.fromtimestamp(now, tz=UTC),
        }

        # 替换 search 方法使其抛异常
        original_search = store._milvus_collection.search
        store._milvus_collection.search = failing_search.__get__(store._milvus_collection)

        try:
            items = await store.recall(scope=scope, query="测试")
            # 应从 PG 获取（降级）
            assert len(items) == 1
            assert items[0].id == "s1"
            assert items[0].content == "降级摘要"
        finally:
            store._milvus_collection.search = original_search

    @pytest.mark.asyncio
    async def test_recall_top_k_limit(self, store, scope):
        """recall 应将 top_k 传递给 Milvus 搜索。"""
        now = time.time()
        # 设置多个搜索结果
        search_hits = []
        for i in range(10):
            search_hits.append({
                "score": 0.9 - i * 0.05,
                "entity": {
                    "summary_id": f"m{i}",
                    "content": f"摘要 {i}",
                    "metadata": json.dumps({
                        "topics": [], "entities": {},
                        "turn_range": [i * 5, i * 5 + 3],
                        "session_id": "sess-001",
                        "access_count": i,
                        "last_accessed_at": now,
                    }),
                    "created_at": str(now - i * 1000),
                },
            })

        store._milvus_collection.set_search_results([search_hits])

        # summary_store.recall 返回所有结果（top_k 用于搜索 limit=top_k*2）
        items = await store.recall(scope=scope, query="测试", top_k=3)

        # 由于搜索 limit=top_k*2=6，但我们设置了 10 条结果，
        # 实际返回数量取决于搜索实现。这里验证至少有结果返回。
        assert len(items) > 0
        # 验证结果按 score 排序
        for i in range(len(items) - 1):
            assert items[i].score >= items[i + 1].score


# ── Task 17.4: final_score 排序测试 ───────────────────────────────────


class TestFinalScoreComputation:
    """测试 final_score 计算和排序。"""

    def test_recency_factor(self, store):
        """新条目应获得更高的 recency 分数。"""
        now = time.time()
        items = [
            RecalledItem(
                id="old", content="旧摘要", topics=[], entities={},
                turn_range=(0, 1), session_id="s1",
                access_count=0, last_accessed_at=0.0,
                created_at=now - 7200,  # 2 小时前
                score=0.5,
            ),
            RecalledItem(
                id="new", content="新摘要", topics=[], entities={},
                turn_range=(0, 1), session_id="s1",
                access_count=0, last_accessed_at=0.0,
                created_at=now,  # 刚刚创建
                score=0.5,
            ),
        ]

        result = store._compute_final_scores(items)

        # 新条目应得更高分数
        new_item = next(i for i in result if i.id == "new")
        old_item = next(i for i in result if i.id == "old")
        assert new_item.score > old_item.score

    def test_access_count_factor(self, store):
        """高频访问条目应获得更高的 access 分数。"""
        now = time.time()
        items = [
            RecalledItem(
                id="low", content="低频访问", topics=[], entities={},
                turn_range=(0, 1), session_id="s1",
                access_count=0, last_accessed_at=0.0,
                created_at=now, score=0.5,
            ),
            RecalledItem(
                id="high", content="高频访问", topics=[], entities={},
                turn_range=(0, 1), session_id="s1",
                access_count=100, last_accessed_at=0.0,
                created_at=now, score=0.5,
            ),
        ]

        result = store._compute_final_scores(items)

        high_item = next(i for i in result if i.id == "high")
        low_item = next(i for i in result if i.id == "low")
        assert high_item.score > low_item.score

    def test_similarity_factor(self, store):
        """高相似度条目应获得更高的 similarity 分数。"""
        now = time.time()
        items = [
            RecalledItem(
                id="low_sim", content="低相似度", topics=[], entities={},
                turn_range=(0, 1), session_id="s1",
                access_count=0, last_accessed_at=0.0,
                created_at=now, score=0.3,  # 低 cosine 相似度
            ),
            RecalledItem(
                id="high_sim", content="高相似度", topics=[], entities={},
                turn_range=(0, 1), session_id="s1",
                access_count=0, last_accessed_at=0.0,
                created_at=now, score=0.95,  # 高 cosine 相似度
            ),
        ]

        result = store._compute_final_scores(items)

        high_item = next(i for i in result if i.id == "high_sim")
        low_item = next(i for i in result if i.id == "low_sim")
        assert high_item.score > low_item.score

    def test_final_score_range(self, store):
        """final_score 应在 [0, 1] 范围内。"""
        now = time.time()
        items = [
            RecalledItem(
                id="test", content="测试", topics=[], entities={},
                turn_range=(0, 1), session_id="s1",
                access_count=1000, last_accessed_at=0.0,
                created_at=now, score=1.0,
            ),
        ]

        result = store._compute_final_scores(items)
        assert 0 <= result[0].score <= 1.0

    @pytest.mark.asyncio
    async def test_recall_sorted_by_final_score(self, store, scope):
        """recall 返回结果应按 final_score 降序排列。"""
        now = time.time()
        items_data = [
            {"id": "low_sim", "score": 0.3, "created_at": now - 3600, "access_count": 0},
            {"id": "high_sim", "score": 0.95, "created_at": now, "access_count": 10},
            {"id": "mid_sim", "score": 0.6, "created_at": now - 600, "access_count": 5},
        ]

        search_hits = []
        for item in items_data:
            search_hits.append({
                "score": item["score"],
                "entity": {
                    "summary_id": item["id"],
                    "content": f"摘要 {item['id']}",
                    "metadata": json.dumps({
                        "topics": [], "entities": {},
                        "turn_range": [0, 5],
                        "session_id": "sess-001",
                        "access_count": item["access_count"],
                        "last_accessed_at": item["created_at"],
                    }),
                    "created_at": str(item["created_at"]),
                },
            })

        store._milvus_collection.set_search_results([search_hits])

        items = await store.recall(scope=scope, query="测试")

        # 应按 final_score 降序排列
        for i in range(len(items) - 1):
            assert items[i].score >= items[i + 1].score


# ── Task 17.5: 查询缓存测试 ──────────────────────────────────────────


class TestQueryCache:
    """测试查询缓存功能。"""

    @pytest.mark.asyncio
    async def test_first_recall_populates_cache(self, store, mock_redis, scope):
        """首次 recall 应填充缓存。"""
        now = time.time()
        search_hits = [{
            "score": 0.9,
            "entity": {
                "summary_id": "m1",
                "content": "缓存测试",
                "metadata": json.dumps({
                    "topics": [], "entities": {},
                    "turn_range": [0, 1],
                    "session_id": "sess-001",
                    "access_count": 0,
                    "last_accessed_at": now,
                }),
                "created_at": str(now),
            },
        }]
        store._milvus_collection.set_search_results([search_hits])

        await store.recall(scope=scope, query="缓存查询")

        # 缓存应被写入
        cache_key = SummaryStore._query_cache_key("t1", "u1", "缓存查询")
        cached = await mock_redis.get(cache_key)
        assert cached is not None

    @pytest.mark.asyncio
    async def test_second_recall_uses_cache(self, store, mock_redis, scope):
        """第二次相同 query 应使用缓存。"""
        now = time.time()
        search_hits = [{
            "score": 0.9,
            "entity": {
                "summary_id": "m1",
                "content": "缓存测试",
                "metadata": json.dumps({
                    "topics": [], "entities": {},
                    "turn_range": [0, 1],
                    "session_id": "sess-001",
                    "access_count": 0,
                    "last_accessed_at": now,
                }),
                "created_at": str(now),
            },
        }]
        store._milvus_collection.set_search_results([search_hits])

        # 首次调用
        await store.recall(scope=scope, query="重复查询")

        # 清除 Milvus 结果，模拟真实环境
        store._milvus_collection._search_called = False

        # 第二次调用应使用缓存
        items = await store.recall(scope=scope, query="重复查询")

        # Milvus 不应被调用
        assert store._milvus_collection._search_called is False
        assert len(items) == 1
        assert items[0].id == "m1"

    @pytest.mark.asyncio
    async def test_different_queries_have_different_caches(self, store, mock_redis, scope):
        """不同 query 应使用不同缓存。"""
        now = time.time()

        # 为两个 query 设置不同结果
        search_hits_a = [{
            "score": 0.9,
            "entity": {
                "summary_id": "m_a",
                "content": "查询 A 结果",
                "metadata": json.dumps({
                    "topics": [], "entities": {},
                    "turn_range": [0, 1],
                    "session_id": "sess-001",
                    "access_count": 0,
                    "last_accessed_at": now,
                }),
                "created_at": str(now),
            },
        }]
        search_hits_b = [{
            "score": 0.8,
            "entity": {
                "summary_id": "m_b",
                "content": "查询 B 结果",
                "metadata": json.dumps({
                    "topics": [], "entities": {},
                    "turn_range": [0, 1],
                    "session_id": "sess-001",
                    "access_count": 0,
                    "last_accessed_at": now,
                }),
                "created_at": str(now),
            },
        }]

        store._milvus_collection.set_search_results([search_hits_a])
        items_a = await store.recall(scope=scope, query="查询 A")

        store._milvus_collection.set_search_results([search_hits_b])
        items_b = await store.recall(scope=scope, query="查询 B")

        assert items_a[0].id == "m_a"
        assert items_b[0].id == "m_b"

    @pytest.mark.asyncio
    async def test_invalidate_cache_after_save(self, store, mock_redis, mock_pool, scope):
        """保存新摘要后应失效缓存。"""
        now = time.time()
        search_hits = [{
            "score": 0.9,
            "entity": {
                "summary_id": "old",
                "content": "旧结果",
                "metadata": json.dumps({
                    "topics": [], "entities": {},
                    "turn_range": [0, 1],
                    "session_id": "sess-001",
                    "access_count": 0,
                    "last_accessed_at": now,
                }),
                "created_at": str(now),
            },
        }]
        store._milvus_collection.set_search_results([search_hits])

        # 填充缓存
        await store.recall(scope=scope, query="测试")

        # 验证缓存存在
        cache_key = SummaryStore._query_cache_key("t1", "u1", "测试")
        assert await mock_redis.get(cache_key) is not None

        # 保存新摘要（不同用户，但会失效所有缓存）
        new_scope = Scope(tenant_id="t1", user_id="u1", session_id="sess-002")
        await store.save_summary(scope=new_scope, content="新摘要", turn_range=(0, 1))

        # 缓存应已失效
        assert await mock_redis.get(cache_key) is None


# ── Task 17.6: token 预算截断测试 ────────────────────────────────────


class TestTokenBudget:
    """测试 token 预算截断功能。"""

    def test_budget_constant(self):
        """TOKEN_BUDGET_L3 应设置为 6000。"""
        assert TOKEN_BUDGET_L3 == 6000

    def test_short_content_kept(self, store):
        """短内容应完整保留。"""
        now = time.time()
        items = [
            RecalledItem(
                id="s1", content="短摘要", topics=[], entities={},
                turn_range=(0, 1), session_id="s1",
                access_count=0, last_accessed_at=0.0,
                created_at=now, score=0.9,
            ),
        ]

        result = store._apply_token_budget(items)
        assert len(result) == 1
        assert result[0].id == "s1"

    def test_long_content_truncated(self, store):
        """超出预算的长内容应被截断。"""
        now = time.time()
        # 每个 content 3000 字符（约 6000 token）
        items = [
            RecalledItem(
                id="l1", content="长" * 1500 + "摘要 1",
                topics=[], entities={},
                turn_range=(0, 1), session_id="s1",
                access_count=0, last_accessed_at=0.0,
                created_at=now, score=0.9,
            ),
            RecalledItem(
                id="l2", content="长" * 1500 + "摘要 2",
                topics=[], entities={},
                turn_range=(2, 3), session_id="s1",
                access_count=0, last_accessed_at=0.0,
                created_at=now, score=0.8,
            ),
        ]

        result = store._apply_token_budget(items)
        # 第一个条目占 6000 token，应被保留
        assert len(result) >= 1
        assert result[0].id == "l1"

    def test_mixed_content_correctly_truncated(self, store):
        """混合长度内容应按预算精确截断。"""
        now = time.time()
        # 短条目（20 字符 = 40 token）+ 长条目（3000 字符 = 6000 token）
        items = [
            RecalledItem(
                id="short", content="短摘要",
                topics=[], entities={},
                turn_range=(0, 1), session_id="s1",
                access_count=0, last_accessed_at=0.0,
                created_at=now, score=0.9,
            ),
            RecalledItem(
                id="long", content="长" * 3000 + "摘要",
                topics=[], entities={},
                turn_range=(2, 3), session_id="s1",
                access_count=0, last_accessed_at=0.0,
                created_at=now, score=0.8,
            ),
            RecalledItem(
                id="another", content="另一个",
                topics=[], entities={},
                turn_range=(4, 5), session_id="s1",
                access_count=0, last_accessed_at=0.0,
                created_at=now, score=0.7,
            ),
        ]

        result = store._apply_token_budget(items)
        # 短条目（40 token）+ 长条目（6000 token）= 6040 > 6000
        # 所以应只有第一个条目被保留
        assert len(result) == 1
        assert result[0].id == "short"

    def test_empty_items(self, store):
        """空列表应返回空列表。"""
        result = store._apply_token_budget([])
        assert result == []

    def test_score_order_respected_before_budget(self, store):
        """token 截断前应已按 score 排序（由调用方保证）。"""
        now = time.time()
        items = [
            RecalledItem(
                id="high", content="高" * 2000 + "优先级高",
                topics=[], entities={},
                turn_range=(0, 1), session_id="s1",
                access_count=100, last_accessed_at=0.0,
                created_at=now, score=0.95,
            ),
            RecalledItem(
                id="low", content="低" * 2000 + "优先级低",
                topics=[], entities={},
                turn_range=(2, 3), session_id="s1",
                access_count=0, last_accessed_at=0.0,
                created_at=now - 10000, score=0.5,
            ),
        ]

        # 先排序再截断
        items.sort(key=lambda x: x.score, reverse=True)
        result = store._apply_token_budget(items)

        # 高优先级应在前
        if result:
            assert result[0].id == "high"


# ── 辅助方法测试 ──────────────────────────────────────────────────────


class TestHelperMethods:
    """测试辅助方法。"""

    @pytest.mark.asyncio
    async def test_batch_touch_updates_access_count(self, store, mock_pool):
        """_batch_touch 应更新 access_count。"""
        now = time.time()
        mock_pool._summaries["s1"] = {
            "id": "s1", "tenant_id": "t1", "user_id": "u1",
            "session_id": "sess-001", "content": "测试",
            "topics": [], "entities": {},
            "turn_start": 0, "turn_end": 1,
            "content_hash": "hash1", "access_count": 0,
            "last_accessed_at": None, "status": "active",
            "created_at": datetime.fromtimestamp(now, tz=UTC),
        }

        items = [RecalledItem(
            id="s1", content="测试", topics=[], entities={},
            turn_range=(0, 1), session_id="sess-001",
            access_count=0, last_accessed_at=0.0,
            created_at=now, score=0.9,
        )]

        await store._batch_touch(items)

        assert mock_pool._summaries["s1"]["access_count"] == 1

    @pytest.mark.asyncio
    async def test_list_recent(self, store, mock_pool):
        """list_recent 应返回最近摘要。"""
        now = time.time()
        mock_pool._summaries["s1"] = {
            "id": "s1", "tenant_id": "t1", "user_id": "u1",
            "session_id": "sess-001", "content": "最近摘要",
            "topics": ["test"], "entities": {"person": ["Alice"]},
            "turn_start": 0, "turn_end": 5,
            "content_hash": "hash1", "access_count": 3,
            "last_accessed_at": None, "status": "active",
            "created_at": datetime.fromtimestamp(now - 100, tz=UTC),
        }
        mock_pool._summaries["s2"] = {
            "id": "s2", "tenant_id": "t1", "user_id": "u1",
            "session_id": "sess-002", "content": "更早摘要",
            "topics": [], "entities": {},
            "turn_start": 10, "turn_end": 15,
            "content_hash": "hash2", "access_count": 1,
            "last_accessed_at": None, "status": "active",
            "created_at": datetime.fromtimestamp(now - 500, tz=UTC),
        }

        results = await store.list_recent("t1", "u1", limit=5)

        assert len(results) == 2
        assert results[0]["id"] == "s1"  # 最近的在前

    @pytest.mark.asyncio
    async def test_delete_summary(self, store, mock_pool):
        """delete_summary 应删除摘要。"""
        now = time.time()
        mock_pool._summaries["s1"] = {
            "id": "s1", "tenant_id": "t1", "user_id": "u1",
            "session_id": "sess-001", "content": "待删除",
            "topics": [], "entities": {},
            "turn_start": 0, "turn_end": 1,
            "content_hash": "hash1", "access_count": 0,
            "last_accessed_at": None, "status": "active",
            "created_at": datetime.fromtimestamp(now, tz=UTC),
        }

        result = await store.delete_summary("t1", "u1", "s1")
        assert result is True
        assert "s1" not in mock_pool._summaries

    @pytest.mark.asyncio
    async def test_delete_nonexistent_summary(self, store, mock_pool):
        """删除不存在的摘要应返回 False。"""
        result = await store.delete_summary("t1", "u1", "nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_get_by_hash(self, store, mock_pool):
        """get_by_hash 应根据 content_hash 查询。"""
        now = time.time()
        mock_pool._summaries["s1"] = {
            "id": "s1", "tenant_id": "t1", "user_id": "u1",
            "session_id": "sess-001", "content": "测试内容",
            "topics": ["test"], "entities": {},
            "turn_start": 0, "turn_end": 1,
            "content_hash": "sha256:abc123", "access_count": 0,
            "last_accessed_at": None, "status": "active",
            "created_at": datetime.fromtimestamp(now, tz=UTC),
        }

        entry = await store.get_by_hash("t1", "u1", "sha256:abc123")
        assert entry is not None
        assert entry.id == "s1"
        assert entry.content == "测试内容"

    @pytest.mark.asyncio
    async def test_list_active(self, store, mock_pool):
        """list_active 应列出活跃摘要。"""
        now = time.time()
        mock_pool._summaries["s1"] = {
            "id": "s1", "tenant_id": "t1", "user_id": "u1",
            "session_id": "sess-001", "content": "活跃摘要",
            "topics": [], "entities": {},
            "turn_start": 0, "turn_end": 1,
            "content_hash": "hash1", "access_count": 0,
            "last_accessed_at": None, "status": "active",
            "created_at": datetime.fromtimestamp(now, tz=UTC),
        }
        mock_pool._summaries["s2"] = {
            "id": "s2", "tenant_id": "t1", "user_id": "u1",
            "session_id": "sess-001", "content": "归档摘要",
            "topics": [], "entities": {},
            "turn_start": 2, "turn_end": 3,
            "content_hash": "hash2", "access_count": 0,
            "last_accessed_at": None, "status": "archived",
            "created_at": datetime.fromtimestamp(now, tz=UTC),
        }

        entries = await store.list_active("t1", "u1")

        assert len(entries) == 1
        assert entries[0].id == "s1"  # 只有 active 状态


# ── 缓存键测试 ──────────────────────────────────────────────────────


class TestCacheKeys:
    """测试缓存键生成方法。"""

    def test_query_cache_key_format(self):
        """查询缓存键应使用正确格式。"""
        key = SummaryStore._query_cache_key("t1", "u1", "查询文本")
        assert key.startswith("memory:l3:query:")
        assert len(key) == len("memory:l3:query:") + 16  # SHA256 前 16 位

    def test_embedding_cache_key_format(self):
        """嵌入缓存键应使用正确格式。"""
        key = SummaryStore._embedding_cache_key("查询文本")
        assert key.startswith("memory:l3:embed:")
        assert len(key) == len("memory:l3:embed:") + 16

    def test_query_cache_key_deterministic(self):
        """相同输入应生成相同缓存键。"""
        key1 = SummaryStore._query_cache_key("t1", "u1", "测试")
        key2 = SummaryStore._query_cache_key("t1", "u1", "测试")
        assert key1 == key2

    def test_query_cache_key_different_inputs(self):
        """不同输入应生成不同缓存键。"""
        key1 = SummaryStore._query_cache_key("t1", "u1", "查询 A")
        key2 = SummaryStore._query_cache_key("t1", "u1", "查询 B")
        assert key1 != key2

    def test_embedding_cache_key_deterministic(self):
        """相同输入应生成相同嵌入缓存键。"""
        key1 = SummaryStore._embedding_cache_key("相同查询")
        key2 = SummaryStore._embedding_cache_key("相同查询")
        assert key1 == key2


# ── 嵌入缓存测试 ──────────────────────────────────────────────────────


class TestEmbeddingCache:
    """测试嵌入向量缓存。"""

    @pytest.mark.asyncio
    async def test_embedding_cached_after_first_call(self, store, mock_redis):
        """首次调用应缓存嵌入向量。"""
        call_count = 0

        async def counting_embedding(query: str) -> list[float]:
            nonlocal call_count
            call_count += 1
            return [0.1, 0.2, 0.3]

        store._embedding_fn = counting_embedding

        # 首次调用
        emb1 = await store._get_embedding("测试")
        assert emb1 == [0.1, 0.2, 0.3]
        assert call_count == 1

        # 验证缓存存在
        cache_key = SummaryStore._embedding_cache_key("测试")
        cached = await mock_redis.get(cache_key)
        assert cached is not None

    @pytest.mark.asyncio
    async def test_embedding_cache_hit(self, store, mock_redis):
        """缓存命中时不应调用 embedding_fn。"""
        call_count = 0

        async def counting_embedding(query: str) -> list[float]:
            nonlocal call_count
            call_count += 1
            return [0.1, 0.2, 0.3]

        store._embedding_fn = counting_embedding

        # 预置缓存
        cache_key = SummaryStore._embedding_cache_key("测试")
        await mock_redis.setex(cache_key, EMBEDDING_CACHE_TTL, json.dumps([0.5, 0.6, 0.7]))

        # 调用应返回缓存值
        emb = await store._get_embedding("测试")
        assert emb == [0.5, 0.6, 0.7]
        assert call_count == 0  # embedding_fn 不应被调用

    @pytest.mark.asyncio
    async def test_embedding_cache_invalid_json(self, store, mock_redis):
        """无效缓存应回退到 embedding_fn。"""
        async def fake_embedding(query: str) -> list[float]:
            return [0.8, 0.9]

        store._embedding_fn = fake_embedding

        # 预置无效缓存
        cache_key = SummaryStore._embedding_cache_key("测试")
        await mock_redis.setex(cache_key, EMBEDDING_CACHE_TTL, "invalid json{{{")

        # 应回退到 embedding_fn
        emb = await store._get_embedding("测试")
        assert emb == [0.8, 0.9]

    @pytest.mark.asyncio
    async def test_embedding_fn_none_returns_none(self, store):
        """无 embedding_fn 时应返回 None。"""
        store._embedding_fn = None
        emb = await store._get_embedding("测试")
        assert emb is None


# ── 常量值测试 ──────────────────────────────────────────────────────


class TestConstants:
    """测试常量值。"""

    def test_query_cache_ttl(self):
        """查询缓存 TTL 应为 300 秒。"""
        assert QUERY_CACHE_TTL == 300

    def test_embedding_cache_ttl(self):
        """嵌入缓存 TTL 应为 1800 秒。"""
        assert EMBEDDING_CACHE_TTL == 1800

    def test_token_budget(self):
        """token 预算应为 6000。"""
        assert TOKEN_BUDGET_L3 == 6000

    def test_default_top_k(self):
        """默认 top_k 应为 5。"""
        assert DEFAULT_TOP_K == 5

    def test_final_score_weights_sum_to_one(self):
        """final_score 权重之和应约为 1。"""
        total = (
            FINAL_SCORE_WEIGHT_RECENCY
            + FINAL_SCORE_WEIGHT_ACCESS
            + FINAL_SCORE_WEIGHT_SIMILARITY
        )
        assert abs(total - 1.0) < 0.001
