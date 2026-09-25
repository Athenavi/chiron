"""L3 SummaryStore Provider - 对话摘要存储与语义检索。

本模块实现 L3 层对话摘要的存储与检索：
- save_summary: 双写 PG（memory_summaries 表）+ Milvus（向量索引）
- recall: 向量检索 + final_score 排序 + token 预算控制
- 查询缓存: Redis 5 分钟缓存 query 结果
- 嵌入向量缓存: Redis 30 分钟缓存 query embedding
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

import redis.asyncio as aioredis

from app.db import get_pool
from app.memory.layers import MemoryType, RecalledItem, Scope, SummaryEntry
from app.redis_keys import rkey

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────

QUERY_CACHE_TTL = 300  # 查询缓存 TTL（5 分钟）
EMBEDDING_CACHE_TTL = 1800  # 嵌入缓存 TTL（30 分钟）
TOKEN_BUDGET_L3 = 6000  # L3 token 硬上限（约 6KB 文本）
DEFAULT_TOP_K = 5  # 默认返回条数
FINAL_SCORE_WEIGHT_RECENCY = 0.4  # recency 权重
FINAL_SCORE_WEIGHT_ACCESS = 0.3  # access_count 权重
FINAL_SCORE_WEIGHT_SIMILARITY = 0.3  # 相似度权重

# Milvus collection name
MILVUS_COLLECTION = "memory_store"
MILVUS_FIELD_SUMMARY_ID = "summary_id"
MILVUS_FIELD_TENANT_ID = "tenant_id"
MILVUS_FIELD_USER_ID = "user_id"
MILVUS_FIELD_SESSION_ID = "session_id"
MILVUS_FIELD_CONTENT = "content"
MILVUS_FIELD_MEMORY_TYPE = "memory_type"
MILVUS_FIELD_VECTOR = "embedding"
MILVUS_FIELD_CREATED_AT = "created_at"
MILVUS_FIELD_METADATA = "metadata"


class SummaryStore:
    """L3 摘要存储 Provider。

    负责管理 PostgreSQL 中 memory_summaries 表和 Milvus 向量索引的双写操作，
    并通过 Redis 缓存优化查询性能。
    """

    def __init__(self, redis: aioredis.Redis, embedding_fn: Any = None) -> None:
        """初始化 SummaryStore。

        Args:
            redis: Redis 连接实例（用于缓存）。
            embedding_fn: 嵌入向量生成函数（可选，延迟注入）。
        """
        self._redis = redis
        self._pool = get_pool()
        self._embedding_fn = embedding_fn
        self._milvus_collection = None

    # ── 缓存键生成 ──────────────────────────────────────────────────────

    @staticmethod
    def _query_cache_key(tenant_id: str, user_id: str, query: str) -> str:
        """生成查询缓存键。"""
        raw = f"{tenant_id}:{user_id}:{query}"
        h = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return rkey(f"memory:l3:query:{h}")

    @staticmethod
    def _embedding_cache_key(query: str) -> str:
        """生成嵌入缓存键。"""
        h = hashlib.sha256(query.encode()).hexdigest()[:16]
        return rkey(f"memory:l3:embed:{h}")

    # ── 嵌入向量缓存 ──────────────────────────────────────────────────────

    async def _get_embedding(self, query: str) -> list[float] | None:
        """获取嵌入向量（带缓存）。

        Args:
            query: 查询文本。

        Returns:
            嵌入向量或 None（如果 embedding_fn 未设置）。
        """
        if self._embedding_fn is None:
            return None

        # 检查缓存
        cache_key = self._embedding_cache_key(query)
        cached = await self._redis.get(cache_key)
        if cached:
            try:
                parsed: list[float] = json.loads(cached)
                return parsed
            except (json.JSONDecodeError, TypeError):
                pass

        # 生成嵌入
        try:
            embedding: list[float] | None = await self._embedding_fn(query)
            if embedding:
                # 写入缓存
                await self._redis.setex(
                    cache_key, EMBEDDING_CACHE_TTL, json.dumps(embedding)
                )
            return embedding
        except Exception as e:
            logger.error("Failed to generate embedding: %s", e)
            return None

    # ── Milvus 连接管理 ────────────────────────────────────────────────

    def _get_milvus_collection(self) -> Any:
        """延迟获取 Milvus collection。"""
        if self._milvus_collection is not None:
            return self._milvus_collection

        try:
            from pymilvus import Collection

            self._milvus_collection = Collection(MILVUS_COLLECTION)
            return self._milvus_collection
        except Exception as e:
            logger.warning("Failed to connect to Milvus: %s", e)
            return None

    # ── save_summary ──────────────────────────────────────────────────────

    async def save_summary(
        self,
        scope: Scope,
        content: str,
        topics: list[str] | None = None,
        entities: dict[str, list[str]] | None = None,
        turn_range: tuple[int, int] = (0, 0),
    ) -> str | None:
        """保存摘要（PG + Milvus 双写）。

        Args:
            scope: 记忆范围（含 tenant_id, user_id, session_id）。
            content: 摘要内容。
            topics: 主题列表。
            entities: 实体字典。
            turn_range: 覆盖的对话轮次范围。

        Returns:
            摘要 ID 或 None（失败时）。
        """
        import hashlib as hl
        import uuid

        summary_id: str = f"sms_{uuid.uuid4().hex[:20]}"
        tenant_id = scope.tenant_id
        user_id = scope.user_id
        session_id = scope.session_id or ""

        # 计算 content_hash
        raw = f"{tenant_id}:{user_id}:{content}"
        content_hash = f"sha256:{hl.sha256(raw.encode()).hexdigest()[:40]}"

        # PG 插入
        try:
            row = await self._pool.fetchrow(
                """INSERT INTO memory_summaries
                   (id, tenant_id, user_id, session_id, content, topics, entities,
                    turn_start, turn_end, content_hash, access_count, last_accessed_at,
                    status, created_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 0, NULL, 'active', NOW())
                   ON CONFLICT (tenant_id, user_id, content_hash) DO NOTHING
                   RETURNING id""",
                summary_id,
                tenant_id,
                user_id,
                session_id,
                content,
                json.dumps(topics or []),
                json.dumps(entities or {}),
                turn_range[0],
                turn_range[1],
                content_hash,
            )
            if row is None:
                # 已存在相同 hash，返回已有 ID
                existing = await self._pool.fetchrow(
                    "SELECT id FROM memory_summaries WHERE content_hash=$1 AND tenant_id=$2 AND user_id=$3",
                    content_hash,
                    tenant_id,
                    user_id,
                )
                return existing["id"] if existing else None

            summary_id = row["id"]
        except Exception as e:
            logger.error("PG insert failed for summary: %s", e)
            return None

        # Milvus 双写（fail-soft）
        embedding = await self._get_embedding(content)
        if embedding:
            await self._milvus_insert(
                summary_id,
                tenant_id,
                user_id,
                session_id,
                content,
                embedding,
                turn_range,
                topics,
                entities,
            )

        # 失效查询缓存
        await self._invalidate_query_cache(tenant_id, user_id)

        return summary_id

    async def _milvus_insert(
        self,
        summary_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        content: str,
        embedding: list[float],
        turn_range: tuple[int, int],
        topics: list[str] | None,
        entities: dict[str, list[str]] | None,
    ) -> None:
        """插入向量到 Milvus（fail-soft：失败只警告）。"""
        coll = self._get_milvus_collection()
        if coll is None:
            return

        try:
            import time as t

            metadata = json.dumps(
                {
                    "session_id": session_id,
                    "turn_range": list(turn_range),
                    "topics": topics or [],
                    "entities": entities or {},
                    "content_hash": "",
                    "access_count": 0,
                    "last_accessed_at": int(t.time()),
                    "status": "active",
                },
                ensure_ascii=False,
            )

            coll.insert(
                [
                    [summary_id],
                    [tenant_id],
                    [user_id],
                    [session_id],
                    [content],
                    [MemoryType.SUMMARY.value],
                    [embedding],
                    [str(int(t.time()))],
                    [metadata],
                ]
            )
            coll.flush()
        except Exception as e:
            logger.warning("Milvus insert failed (PG has data): %s", e)

    # ── recall ──────────────────────────────────────────────────────────

    async def recall(
        self,
        scope: Scope,
        query: str = "",
        top_k: int = DEFAULT_TOP_K,
    ) -> list[RecalledItem]:
        """召回相关摘要（向量检索 + final_score 排序）。

        Args:
            scope: 记忆范围。
            query: 查询文本。
            top_k: 返回条数上限。

        Returns:
            按 final_score 排序的召回项列表。
        """
        tenant_id = scope.tenant_id
        user_id = scope.user_id

        # 检查查询缓存
        if query:
            cached = await self._get_query_cache(tenant_id, user_id, query)
            if cached is not None:
                return cached

        # 获取嵌入向量
        embedding = await self._get_embedding(query) if query else None

        # 向量检索
        if embedding:
            items = await self._milvus_search(
                tenant_id, user_id, query, embedding, top_k
            )
        else:
            # 无嵌入时降级：从 PG 取最近摘要
            items = await self._pg_recent_summaries(tenant_id, user_id, top_k)

        # final_score 排序
        items = self._compute_final_scores(items)
        items.sort(key=lambda x: x.score, reverse=True)

        # token 预算截断
        items = self._apply_token_budget(items)

        # 写入缓存
        if query and items:
            await self._set_query_cache(tenant_id, user_id, query, items)

        # 更新 access_count
        await self._batch_touch(items)

        return items

    async def _milvus_search(
        self,
        tenant_id: str,
        user_id: str,
        query: str,
        embedding: list[float],
        top_k: int,
    ) -> list[RecalledItem]:
        """从 Milvus 检索向量相似度。"""
        coll = self._get_milvus_collection()
        if coll is None:
            return await self._pg_recent_summaries(tenant_id, user_id, top_k)

        try:
            results = coll.search(
                data=[embedding],
                anns_field=MILVUS_FIELD_VECTOR,
                param={"metric_type": "COSINE", "params": {"nprobe": 10}},
                limit=top_k * 2,  # 多取用于重排序
                expr=(
                    f'tenant_id == "{tenant_id}" && '
                    f'user_id == "{user_id}" && '
                    f'memory_type == "{MemoryType.SUMMARY.value}"'
                ),
                output_fields=[
                    MILVUS_FIELD_SUMMARY_ID,
                    MILVUS_FIELD_CONTENT,
                    MILVUS_FIELD_METADATA,
                    MILVUS_FIELD_CREATED_AT,
                ],
            )

            items = []
            for hits in results:
                for hit in hits:
                    metadata = json.loads(hit.entity.get(MILVUS_FIELD_METADATA, "{}"))
                    items.append(
                        RecalledItem(
                            id=hit.entity.get(MILVUS_FIELD_SUMMARY_ID, ""),
                            content=hit.entity.get(MILVUS_FIELD_CONTENT, ""),
                            topics=metadata.get("topics", []),
                            entities=metadata.get("entities", {}),
                            turn_range=tuple(metadata.get("turn_range", [0, 0])),
                            session_id=metadata.get("session_id", ""),
                            access_count=metadata.get("access_count", 0),
                            last_accessed_at=metadata.get("last_accessed_at", 0),
                            created_at=float(
                                hit.entity.get(MILVUS_FIELD_CREATED_AT, 0)
                            ),
                            score=hit.score,  # 初始为 cosine 相似度
                        )
                    )
            return items

        except Exception as e:
            logger.warning("Milvus search failed, falling back to PG: %s", e)
            return await self._pg_recent_summaries(tenant_id, user_id, top_k)

    async def _pg_recent_summaries(
        self,
        tenant_id: str,
        user_id: str,
        limit: int,
    ) -> list[RecalledItem]:
        """从 PG 获取最近摘要（降级方案）。"""
        try:
            rows = await self._pool.fetch(
                """SELECT id, content, topics, entities,
                          turn_start, turn_end, session_id,
                          access_count, last_accessed_at, created_at
                   FROM memory_summaries
                   WHERE tenant_id=$1 AND user_id=$2 AND status='active'
                   ORDER BY created_at DESC
                   LIMIT $3""",
                tenant_id,
                user_id,
                limit,
            )

            items = []
            for row in rows:
                topics = row["topics"]
                entities = row["entities"]
                if isinstance(topics, str):
                    topics = json.loads(topics)
                if isinstance(entities, str):
                    entities = json.loads(entities)

                last_accessed = row["last_accessed_at"]
                if last_accessed is not None:
                    last_accessed = last_accessed.timestamp()
                else:
                    last_accessed = 0.0

                items.append(
                    RecalledItem(
                        id=row["id"],
                        content=row["content"],
                        topics=topics or [],
                        entities=entities or {},
                        turn_range=(row["turn_start"], row["turn_end"]),
                        session_id=row["session_id"],
                        access_count=row["access_count"],
                        last_accessed_at=last_accessed,
                        created_at=(
                            row["created_at"].timestamp()
                            if row["created_at"]
                            else time.time()
                        ),
                        score=0.0,
                    )
                )
            return items
        except Exception as e:
            logger.error("PG query failed for summaries: %s", e)
            return []

    def _compute_final_scores(self, items: list[RecalledItem]) -> list[RecalledItem]:
        """计算 final_score 排序。

        final_score = w_recency * recency_factor
                    + w_access * access_factor
                    + w_similarity * similarity_factor
        """
        now = time.time()

        for item in items:
            # recency_factor: 越新越好（0-1）
            age_hours = (now - item.created_at) / 3600
            recency = max(0, 1.0 - min(age_hours / 720, 1.0))  # 30 天衰减

            # access_factor: 使用 log 压缩（0-1）
            import math

            access = min(math.log1p(item.access_count) / 5.0, 1.0)

            # similarity_factor: 初始 score 已经是 cosine 相似度
            similarity = item.score if 0 <= item.score <= 1 else 0.5

            item.score = (
                FINAL_SCORE_WEIGHT_RECENCY * recency
                + FINAL_SCORE_WEIGHT_ACCESS * access
                + FINAL_SCORE_WEIGHT_SIMILARITY * similarity
            )

        return items

    def _apply_token_budget(self, items: list[RecalledItem]) -> list[RecalledItem]:
        """按 token 预算截断（按 final_score 从高到低累加）。"""
        total_chars = 0
        result = []
        for item in items:
            item_chars = len(item.content) * 2  # 粗略估计：1 中文字 ≈ 2 token
            if total_chars + item_chars > TOKEN_BUDGET_L3:
                break
            total_chars += item_chars
            result.append(item)
        return result

    async def _batch_touch(self, items: list[RecalledItem]) -> None:
        """批量更新 access_count。"""
        if not items:
            return
        ids = [item.id for item in items]
        try:
            await self._pool.execute(
                """UPDATE memory_summaries
                   SET access_count = access_count + 1,
                       last_accessed_at = NOW()
                   WHERE id = ANY($1)""",
                ids,
            )
        except Exception as e:
            logger.warning("Batch touch failed: %s", e)

    # ── 查询缓存 ──────────────────────────────────────────────────────

    async def _get_query_cache(
        self, tenant_id: str, user_id: str, query: str
    ) -> list[RecalledItem] | None:
        """获取查询缓存。"""
        cache_key = self._query_cache_key(tenant_id, user_id, query)
        cached = await self._redis.get(cache_key)
        if cached:
            try:
                data = json.loads(cached)
                return [RecalledItem(**item) for item in data]
            except (json.JSONDecodeError, TypeError):
                pass
        return None

    async def _set_query_cache(
        self,
        tenant_id: str,
        user_id: str,
        query: str,
        items: list[RecalledItem],
    ) -> None:
        """设置查询缓存。"""
        cache_key = self._query_cache_key(tenant_id, user_id, query)
        try:
            data = [item.__dict__ for item in items]
            await self._redis.setex(
                cache_key, QUERY_CACHE_TTL, json.dumps(data, ensure_ascii=False)
            )
        except Exception as e:
            logger.warning("Set query cache failed: %s", e)

    async def _invalidate_query_cache(self, tenant_id: str, user_id: str) -> None:
        """失效指定用户的所有查询缓存。"""
        try:
            pattern = rkey("memory:l3:query:*")
            async for key in self._redis.scan_iter(match=pattern):
                await self._redis.delete(key)
        except Exception as e:
            logger.warning("Invalidate query cache failed: %s", e)

    # ── 辅助方法 ──────────────────────────────────────────────────────

    async def list_recent(
        self,
        tenant_id: str,
        user_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """列出最近的摘要（供 API 使用）。"""
        try:
            rows = await self._pool.fetch(
                """SELECT id, content, topics, entities,
                          turn_start, turn_end, session_id,
                          access_count, created_at
                   FROM memory_summaries
                   WHERE tenant_id=$1 AND user_id=$2 AND status='active'
                   ORDER BY created_at DESC
                   LIMIT $3""",
                tenant_id,
                user_id,
                limit,
            )
            return [
                {
                    "id": row["id"],
                    "content": row["content"],
                    "topics": (
                        row["topics"]
                        if isinstance(row["topics"], list)
                        else json.loads(row["topics"] or "[]")
                    ),
                    "entities": (
                        row["entities"]
                        if isinstance(row["entities"], dict)
                        else json.loads(row["entities"] or "{}")
                    ),
                    "turn_start": row["turn_start"],
                    "turn_end": row["turn_end"],
                    "session_id": row["session_id"],
                    "access_count": row["access_count"],
                    "created_at": (
                        row["created_at"].isoformat() if row["created_at"] else None
                    ),
                }
                for row in rows
            ]
        except Exception as e:
            logger.error("List recent summaries failed: %s", e)
            return []

    async def delete_summary(
        self,
        tenant_id: str,
        user_id: str,
        summary_id: str,
    ) -> bool:
        """删除摘要。"""
        try:
            result = await self._pool.execute(
                "DELETE FROM memory_summaries WHERE id=$1 AND tenant_id=$2 AND user_id=$3",
                summary_id,
                tenant_id,
                user_id,
            )
            deleted = "DELETE 1" in (result or "")
            if deleted:
                await self._invalidate_query_cache(tenant_id, user_id)
            return deleted
        except Exception as e:
            logger.error("Delete summary failed: %s", e)
            return False

    # ── Consolidator 兼容方法 ─────────────────────────────────────────

    async def get_by_hash(
        self,
        tenant_id: str,
        user_id: str,
        content_hash: str,
    ) -> SummaryEntry | None:
        """根据 content_hash 查询摘要（供 Consolidator 去重使用）。"""
        try:
            row = await self._pool.fetchrow(
                """SELECT id, tenant_id, user_id, session_id, content, topics, entities,
                          turn_start, turn_end, content_hash, access_count, last_accessed_at,
                          status, created_at
                   FROM memory_summaries
                   WHERE content_hash=$1 AND tenant_id=$2 AND user_id=$3""",
                content_hash,
                tenant_id,
                user_id,
            )
            if row is None:
                return None
            return self._row_to_entry(row)
        except Exception as e:
            logger.error("Get by hash failed: %s", e)
            return None

    async def insert(
        self,
        entry: SummaryEntry,
        embedding: list[float] | None = None,
    ) -> SummaryEntry:
        """插入摘要（供 Consolidator 使用，兼容旧接口）。"""
        result = await self.save_summary(
            scope=Scope(
                tenant_id=entry.tenant_id,
                user_id=entry.user_id,
                session_id=entry.session_id,
            ),
            content=entry.content,
            topics=entry.topics,
            entities=entry.entities,
            turn_range=(entry.turn_start, entry.turn_end),
        )
        if result is None:
            raise RuntimeError("Failed to insert summary")

        created = await self.get_by_id(entry.tenant_id, entry.user_id, result)
        if created is None:
            raise RuntimeError(f"Summary not found after insert: {result}")

        if embedding:
            await self._milvus_insert(
                created.id,
                created.tenant_id,
                created.user_id,
                created.session_id,
                created.content,
                embedding,
                (created.turn_start, created.turn_end),
                created.topics,
                created.entities,
            )
            created.embedding = embedding

        return created

    async def get_by_id(
        self,
        tenant_id: str,
        user_id: str,
        summary_id: str,
    ) -> SummaryEntry | None:
        """根据 ID 查询摘要（供 Consolidator 使用）。"""
        try:
            row = await self._pool.fetchrow(
                """SELECT id, tenant_id, user_id, session_id, content, topics, entities,
                          turn_start, turn_end, content_hash, access_count, last_accessed_at,
                          status, created_at
                   FROM memory_summaries
                   WHERE id=$1 AND tenant_id=$2 AND user_id=$3""",
                summary_id,
                tenant_id,
                user_id,
            )
            if row is None:
                return None
            return self._row_to_entry(row)
        except Exception as e:
            logger.error("Get by id failed: %s", e)
            return None

    async def list_active(
        self,
        tenant_id: str,
        user_id: str,
        limit: int = 50,
    ) -> list[SummaryEntry]:
        """列出活跃摘要（供 Consolidator 近重复检测使用）。"""
        try:
            rows = await self._pool.fetch(
                """SELECT id, tenant_id, user_id, session_id, content, topics, entities,
                          turn_start, turn_end, content_hash, access_count, last_accessed_at,
                          status, created_at
                   FROM memory_summaries
                   WHERE tenant_id=$1 AND user_id=$2 AND status='active'
                   ORDER BY created_at DESC
                   LIMIT $3""",
                tenant_id,
                user_id,
                limit,
            )
            return [self._row_to_entry(row) for row in rows]
        except Exception as e:
            logger.error("List active failed: %s", e)
            return []

    def _row_to_entry(self, row: Any) -> SummaryEntry:
        """将数据库行转换为 SummaryEntry。"""
        import json as j

        topics_raw = row["topics"]
        entities_raw = row["entities"]
        topics = (
            j.loads(topics_raw) if isinstance(topics_raw, str) else (topics_raw or [])
        )
        entities = (
            j.loads(entities_raw)
            if isinstance(entities_raw, str)
            else (entities_raw or {})
        )

        last_accessed = row["last_accessed_at"]
        if last_accessed is not None:
            last_accessed = last_accessed.timestamp()

        created_at = row["created_at"]
        if created_at is not None:
            created_at = created_at.timestamp()
        else:
            created_at = time.time()

        return SummaryEntry(
            id=row["id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            session_id=row["session_id"],
            content=row["content"],
            topics=topics,
            entities=entities,
            turn_start=row["turn_start"],
            turn_end=row["turn_end"],
            content_hash=row["content_hash"],
            access_count=row["access_count"],
            last_accessed_at=last_accessed,
            status=row["status"],
            created_at=created_at,
        )
