"""PR-3 Task 28: 集成测试 - 多租户隔离。

验证：
1. PG 行级 tenant_id 过滤：不同 tenant 间数据零泄漏。
2. SummaryStore 的 recall 方法严格按 tenant_id + user_id 过滤。
3. MemoryService 端到端跨租户隔离（双租户场景）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.memory.layers import (
    Scope,
)
from app.memory.profile_card import ProfileCard
from app.memory.service import MemoryService
from app.memory.session_meta import SessionMetaStore
from app.memory.summary_store import SummaryStore

# ── Mock 基础设施 ────────────────────────────────────────────────────


class _MemoryPool:
    """Mock asyncpg 连接池（仅关心 tenant_id/user_id 过滤）。"""

    def __init__(self):
        self._profile_rows: list[dict[str, Any]] = []
        self._summary_rows: list[dict[str, Any]] = []

    def add_profile(self, tenant_id, user_id, slot, item_key, item_value,
                   confidence=0.8, source="derived", version=1):
        self._profile_rows.append({
            "tenant_id": tenant_id, "user_id": user_id, "slot": slot,
            "item_key": item_key, "item_value": item_value,
            "confidence": confidence, "source": source, "version": version,
        })

    def add_summary(self, summary_id, tenant_id, user_id, content, topics=None,
                    entities=None, turn_start=0, turn_end=0, session_id="s"):
        self._summary_rows.append({
            "id": summary_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "session_id": session_id,
            "content": content,
            "topics": topics or [],
            "entities": entities or {},
            "turn_start": turn_start,
            "turn_end": turn_end,
            "access_count": 0,
            "last_accessed_at": None,
            "created_at": datetime.now(UTC),
        })

    async def fetch(self, query, *args):
        results = []
        if "FROM user_memory_profile" in query:
            for row in self._profile_rows:
                if row["tenant_id"] == args[0] and row["user_id"] == args[1]:
                    results.append(self._row_to_db(row))
        elif "FROM memory_summaries" in query and "SELECT" in query:
            # PG 回退路径：WHERE tenant_id=$1 AND user_id=$2 AND ... LIMIT $3
            tenant_id, user_id, limit = args[0], args[1], args[2]
            matched = [r for r in self._summary_rows
                       if r["tenant_id"] == tenant_id and r["user_id"] == user_id]
            # 按 created_at DESC + limit
            matched.sort(key=lambda r: r["created_at"], reverse=True)
            for r in matched[:limit]:
                results.append(self._summary_row_to_db(r))
        return results

    async def fetchrow(self, query, *args):
        if "INSERT INTO memory_summaries" in query and "RETURNING id" in query:
            return {"id": args[0]}
        if "FROM user_memory_profile" in query and "item_key = $4" in query:
            tenant_id, user_id, slot, item_key = args[0], args[1], args[2], args[3]
            for row in self._profile_rows:
                if (row["tenant_id"] == tenant_id
                        and row["user_id"] == user_id
                        and row["slot"] == slot
                        and row["item_key"] == item_key):
                    return self._row_to_db(row)
        if "FROM memory_summaries" in query and "WHERE content_hash=" in query:
            # 命中 OR 回退查找：返回第一个匹配行 id
            # 简化：总是返回 None 触发"未存在"分支
            return None
        return None

    async def executemany(self, query, *args, **kwargs):
        return None

    def _row_to_db(self, row):
        return {
            "slot": row["slot"],
            "item_key": row["item_key"],
            "item_value": row["item_value"],
            "confidence": row["confidence"],
            "source": row["source"],
            "version": row["version"],
            "confirmed_at": None,
            "last_referenced_at": None,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }

    def _summary_row_to_db(self, row):
        return {
            "id": row["id"],
            "content": row["content"],
            "topics": row["topics"],
            "entities": row["entities"],
            "turn_start": row["turn_start"],
            "turn_end": row["turn_end"],
            "session_id": row["session_id"],
            "access_count": row["access_count"],
            "last_accessed_at": None,
            "created_at": row["created_at"],
        }


class _FakeRedis:
    """内存 Redis stub。"""

    def __init__(self):
        self._store: dict[str, str] = {}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, ex=None):
        self._store[key] = value
        return True

    async def setex(self, key, ttl, value):
        self._store[key] = value
        return True

    async def delete(self, *keys):
        for k in keys:
            self._store.pop(k, None)
        return len(keys)


class _FakeVectorStore:
    """Milvus stub，用于 _milvus_search 路径。"""

    def __init__(self):
        self._items: list[dict[str, Any]] = []

    def insert(self, data, *args, **kwargs):
        """模拟 Milvus insert（将数据存储到 _items 以便搜索）。

        支持两种格式:
        1. 字典格式（测试直接调用）: {"id": ..., "tenant_id": ..., ...}
        2. Milvus 列表格式: [[summary_id], [tenant_id], ...]
        """
        if isinstance(data, dict):
            # 字典格式（测试直接调用）
            self._items.append(data.copy())
        elif isinstance(data, list) and len(data) >= 7:
            # Milvus 列表格式
            item = {
                "id": data[0][0] if isinstance(data[0], list) else data[0],
                "tenant_id": data[1][0] if isinstance(data[1], list) else data[1],
                "user_id": data[2][0] if isinstance(data[2], list) else data[2],
                "session_id": data[3][0] if isinstance(data[3], list) else data[3],
                "content": data[4][0] if isinstance(data[4], list) else data[4],
                "memory_type": data[5][0] if isinstance(data[5], list) else data[5],
                "created_at": data[7][0] if isinstance(data[7], list) else data[7],
                "metadata": data[8][0] if isinstance(data[8], list) else data[8] if len(data) > 8 else "{}",
                "final_score": 0.5,
            }
            self._items.append(item)

    def flush(self):
        pass

    def search(self, data, anns_field, param, limit, expr=None, output_fields=None):
        """模拟 Milvus search，返回符合 expr 过滤的数据。"""
        import json as _json
        tenant_id = None
        user_id = None
        memory_type = None
        if expr:
            # Milvus expr 使用 " && " 作为分隔符（两侧带空格）
            for part in expr.split(" && "):
                part = part.strip()
                if part.startswith('tenant_id == "'):
                    tenant_id = part[len('tenant_id == "'):-1]
                elif part.startswith('user_id == "'):
                    user_id = part[len('user_id == "'):-1]
                elif part.startswith('memory_type == "'):
                    memory_type = part[len('memory_type == "'):-1]

        matched = []
        for item in self._items:
            if tenant_id and item.get("tenant_id") != tenant_id:
                continue
            if user_id and item.get("user_id") != user_id:
                continue
            if memory_type and item.get("memory_type") != memory_type:
                continue
            # 构造 hit 对象（类似 pymilvus Hit）
            hit = MagicMock()
            metadata = item.get("metadata", "{}")
            if isinstance(metadata, dict):
                metadata = _json.dumps(metadata)
            hit.entity = {
                "summary_id": item.get("id", ""),
                "content": item.get("content", ""),
                "metadata": metadata,
                "created_at": item.get("created_at", 0),
            }
            hit.score = item.get("final_score", 0.5)
            matched.append(hit)

        return [matched[:limit]]


def _build_service(pool=None, redis=None, vector=None, with_embedding=True) -> MemoryService:
    """构造带隔离的 MemoryService（通过猴子补丁注入 pool 和 milvus）。

    Args:
        pool: Mock 数据库池。
        redis: Mock Redis 实例。
        vector: Mock 向量存储（设为 None 则不注入向量检索）。
        with_embedding: 是否启用嵌入函数（False 时 SummaryStore 走 PG 路径）。
    """
    pool = pool or _MemoryPool()
    redis = redis or _FakeRedis()
    vector = vector or _FakeVectorStore()

    # 通过猴子补丁为 ProfileCard 注入 pool
    import app.memory.profile_card as _pc_mod
    orig_get_pool_pc = _pc_mod.get_pool
    _pc_mod.get_pool = lambda: pool

    # 通过猴子补丁为 SummaryStore 注入 pool
    import app.memory.summary_store as _ss_mod
    orig_get_pool_ss = _ss_mod.get_pool
    _ss_mod.get_pool = lambda: pool

    try:
        profile = ProfileCard(redis=redis)

        embedding_fn = None
        if with_embedding:
            async def _fake_embedding(query: str):
                import hashlib
                h = hashlib.sha256(query.encode()).digest()
                return [b / 255.0 for b in h]
            embedding_fn = _fake_embedding

        summary = SummaryStore(redis=redis, embedding_fn=embedding_fn)
        if vector is not None and with_embedding:
            summary._milvus_collection = vector
        session_meta = SessionMetaStore()

        return MemoryService(
            session_meta_store=session_meta,
            profile_card=profile,
            summary_store=summary,
            producer=None,
        )
    finally:
        _pc_mod.get_pool = orig_get_pool_pc
        _ss_mod.get_pool = orig_get_pool_ss


# ── Tests ─────────────────────────────────────────────────────────────


class TestPGRowLevelFiltering:
    """PG 侧通过 tenant_id + user_id 精确过滤。"""

    @pytest.mark.asyncio
    async def test_tenant_a_cannot_see_tenant_b_profile(self):
        pool = _MemoryPool()
        pool.add_profile(tenant_id="tA", user_id="uA", slot="preference",
                         item_key="lang", item_value="en")
        pool.add_profile(tenant_id="tB", user_id="uB", slot="preference",
                         item_key="lang", item_value="zh")

        svc = _build_service(pool=pool)

        items_a = await svc._profile_card.get_profile(tenant_id="tA", user_id="uA")
        values_a = [i.item_value for i in items_a]
        assert "zh" not in values_a
        assert "en" in values_a

        items_b = await svc._profile_card.get_profile(tenant_id="tB", user_id="uB")
        values_b = [i.item_value for i in items_b]
        assert "en" not in values_b
        assert "zh" in values_b

    @pytest.mark.asyncio
    async def test_same_user_different_tenants_isolated(self):
        """同一 user_id 在不同 tenant 下的数据必须隔离。"""
        pool = _MemoryPool()
        pool.add_profile(tenant_id="tA", user_id="shared", slot="preference",
                         item_key="role", item_value="admin_A")
        pool.add_profile(tenant_id="tB", user_id="shared", slot="preference",
                         item_key="role", item_value="admin_B")

        svc = _build_service(pool=pool)

        items_a = await svc._profile_card.get_profile(tenant_id="tA", user_id="shared")
        values_a = [i.item_value for i in items_a]
        assert "admin_A" in values_a
        assert "admin_B" not in values_a

        items_b = await svc._profile_card.get_profile(tenant_id="tB", user_id="shared")
        values_b = [i.item_value for i in items_b]
        assert "admin_B" in values_b
        assert "admin_A" not in values_b


class TestVectorFiltering:
    """SummaryStore recall 通过 tenant_id/user_id 过滤。"""

    @pytest.mark.asyncio
    async def test_summary_store_recall_isolates_tenants(self):
        import time as _time
        now_ts = _time.time()
        vector = _FakeVectorStore()
        vector.insert({
            "id": "s1", "tenant_id": "tA", "user_id": "uA",
            "content": "tenant A secret about project X",
            "final_score": 0.9,
            "metadata": {"topics": ["project_x"], "entities": {}, "turn_range": [0, 0]},
            "created_at": now_ts,
            "memory_type": "summary",
        })
        vector.insert({
            "id": "s2", "tenant_id": "tB", "user_id": "uB",
            "content": "tenant B secret about project Y",
            "final_score": 0.9,
            "metadata": {"topics": ["project_y"], "entities": {}, "turn_range": [0, 0]},
            "created_at": now_ts,
            "memory_type": "summary",
        })

        pool = _MemoryPool()
        redis = _FakeRedis()

        import app.memory.summary_store as _ss_mod
        orig = _ss_mod.get_pool
        _ss_mod.get_pool = lambda: pool
        try:
            async def _fake_embedding(query):
                import hashlib
                h = hashlib.sha256(query.encode()).digest()
                return [b / 255.0 for b in h]

            store = SummaryStore(redis=redis, embedding_fn=_fake_embedding)
            store._milvus_collection = vector

            results_a = await store.recall(
                scope=Scope(tenant_id="tA", user_id="uA", session_id=""),
                query="project",
            )
            contents_a = [r.content for r in results_a]
            assert "tenant B" not in " ".join(contents_a)
            assert any("tenant A" in c for c in contents_a)

            results_b = await store.recall(
                scope=Scope(tenant_id="tB", user_id="uB", session_id=""),
                query="project",
            )
            contents_b = [r.content for r in results_b]
            assert "tenant A" not in " ".join(contents_b)
            assert any("tenant B" in c for c in contents_b)
        finally:
            _ss_mod.get_pool = orig


class TestEndToEndTenantIsolation:
    """端到端双租户场景，验证 recall 结果零泄漏。"""

    @pytest.mark.asyncio
    async def test_recall_cross_tenant_zero_leak(self):
        """通过 PG 回退路径验证跨租户零泄漏（预置 summary_rows）。"""
        pool = _MemoryPool()
        # 预置 profile
        pool.add_profile(tenant_id="tA", user_id="uA", slot="preference",
                         item_key="lang", item_value="English",
                         confidence=1.0, source="user_confirmed")
        pool.add_profile(tenant_id="tB", user_id="uB", slot="preference",
                         item_key="lang", item_value="中文",
                         confidence=1.0, source="user_confirmed")
        # 预置 summary（PG 回退路径使用）
        pool.add_summary("sA1", tenant_id="tA", user_id="uA",
                         content="tenant A summary about project X",
                         topics=["project_x"], session_id="sA")
        pool.add_summary("sB1", tenant_id="tB", user_id="uB",
                         content="tenant B summary about project Y",
                         topics=["project_y"], session_id="sB")

        # 使用 with_embedding=False，使 SummaryStore 走 PG 路径
        svc = _build_service(pool=pool, with_embedding=False)

        # tA recall → 不应看到 tB 数据
        # 使用非空查询触发 L3 recall（PG 路径）
        recalled_a = await svc.recall("tA", "uA", query="project")
        text_a = recalled_a.profile_block
        summary_contents_a = [i.content for i in recalled_a.summary_items]

        assert "English" in text_a
        assert "中文" not in text_a
        assert not any("project Y" in c for c in summary_contents_a)
        assert any("project X" in c for c in summary_contents_a)

        # tB recall → 不应看到 tA 数据
        recalled_b = await svc.recall("tB", "uB", query="project")
        text_b = recalled_b.profile_block
        summary_contents_b = [i.content for i in recalled_b.summary_items]

        assert "中文" in text_b
        assert "English" not in text_b
        assert not any("project X" in c for c in summary_contents_b)
        assert any("project Y" in c for c in summary_contents_b)

    @pytest.mark.asyncio
    async def test_recall_empty_for_unknown_tenant(self):
        svc = _build_service()

        recalled = await svc.recall("unknown_tenant", "ghost", query="anything")

        assert recalled.has_content is False
