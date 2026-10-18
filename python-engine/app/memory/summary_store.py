"""L3 SummaryStore Provider - 对话摘要存储与语义检索。

本模块实现 L3 层对话摘要的存储与检索：
- save_summary: 写 PG（memory_summaries 表，**含向量列**），配成 milvus 时再镜像一份到 Milvus
- recall: 按 `MEMORY_VECTOR_BACKEND` 选路 —— pg 走「取候选 + 本层精确余弦」，
  milvus 走 ANN；两级都拿不到时退回按时间取，并把 source 记成 `recent`（不再冒充语义命中）
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

from app.config import settings
from app.db import get_pool
from app.memory.layers import (
    CosineScorer,
    MemoryType,
    RecalledItem,
    Scope,
    SummaryEntry,
)
from app.memory.vector_backend import (
    MILVUS,
    FailureLog,
    active_embedding_model,
    parse_vector,
    report_space_mismatch,
    resolve,
    same_vector_space,
)
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

#: PG 语义扫描一次取多少条候选。不是"全表"：单租户/单用户的活跃摘要被保留期
#: （`archive_expired`）与这里的上限双重夹住，M12 实测 200 条 1-vs-N 精确扫描约 16ms，
#: 所以这一层不需要 ANN 索引（同迁移 0019 的判定）。
CANDIDATE_LIMIT = 200

_RECALL_COLUMNS = (
    "id, content, topics, entities, turn_start, turn_end, session_id, "
    "access_count, last_accessed_at, created_at"
)

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

    负责 PG 中 memory_summaries 表（正文 + 元数据 + 向量列）的读写，
    并按配置可选地维护一份 Milvus 镜像；查询结果经 Redis 缓存。
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
        self._milvus_fail = FailureLog("Milvus 摘要检索")

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
        """延迟获取 Milvus collection；不可达时由 FailureLog 大声一次。"""
        if self._milvus_collection is not None:
            return self._milvus_collection

        try:
            from pymilvus import Collection

            self._milvus_collection = Collection(MILVUS_COLLECTION)
            self._milvus_fail.success()
            return self._milvus_collection
        except Exception as e:
            self._milvus_fail.failure(f"connect: {e}")
            return None

    # ── save_summary ──────────────────────────────────────────────────────

    async def save_summary(
        self,
        scope: Scope,
        content: str,
        topics: list[str] | None = None,
        entities: dict[str, list[str]] | None = None,
        turn_range: tuple[int, int] = (0, 0),
        embedding: list[float] | None = None,
        embedding_model: str | None = None,
    ) -> str | None:
        """保存摘要（向量落 PG，配了 milvus 再镜像一份）。

        Args:
            scope: 记忆范围（含 tenant_id, user_id, session_id）。
            content: 摘要内容。
            topics: 主题列表。
            entities: 实体字典。
            turn_range: 覆盖的对话轮次范围。
            embedding: 已算好的向量。口径由 `Consolidator` 决定（`topics + 正文`），
                这里不补嵌；为 None 就如实写 NULL 列。

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

        # 嵌入在 INSERT **之前**：向量是这一行的一个列，分两次写就存在
        # "行落库了、向量没落"的中间态（PG 提交失败时镜像已经写出去了）。
        #
        # 这里**不再按正文补嵌**（B1：口径收敛到 Consolidator 的 `topics + 正文`）。
        # 补嵌本身会错：判重与召回比的是同一个向量空间，混进"裸正文"口径的行之后，
        # 相似度算出来的仍是 0~1 之间的数，看不出错，只是永远够不到阈值 —— 静默退化
        # 比没有向量更难查。没有向量就如实写 NULL，它照样有资格按时间被召回。

        # PG 插入
        try:
            row = await self._pool.fetchrow(
                """INSERT INTO memory_summaries
                   (id, tenant_id, user_id, session_id, content, topics, entities,
                    turn_start, turn_end, content_hash, access_count, last_accessed_at,
                    status, created_at, embedding, embedding_model, embedding_dim)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 0, NULL, 'active',
                           NOW(), $11, $12, $13)
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
                list(embedding) if embedding else None,
                embedding_model,
                len(embedding) if embedding else None,
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

        # Milvus 镜像（仅当后端配成 milvus —— 配成 pg 时没人查它，写了只是多一次外部调用）
        if embedding and resolve() == MILVUS:
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
        """插入向量到 Milvus（fail-soft：PG 已有数据，失败只记日志）。"""
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
            self._milvus_fail.success()
        except Exception as e:
            # PG 已经有这条（含向量），所以镜像失败不致命 —— 但必须记下来。
            self._milvus_fail.failure(f"insert {summary_id}: {e}")

    # ── recall ──────────────────────────────────────────────────────────

    async def recall(
        self,
        scope: Scope,
        query: str = "",
        top_k: int = DEFAULT_TOP_K,
    ) -> list[RecalledItem]:
        """召回相关摘要（语义检索 + final_score 排序，见下面的三级取数）。

        Args:
            scope: 记忆范围。
            query: 查询文本。
            top_k: 返回条数上限。

        Returns:
            按 final_score 排序的召回项列表；`source` 说明这一条是被哪一级选出来的
            （"vector" = 按查询向量筛过，"recent" = 只按时间取）。
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

        # 取数分三级，从"最准"到"最保底"：
        #   ① Milvus ANN —— 只有把它配成后端时才查（默认是 pg，见 `MEMORY_VECTOR_BACKEND`）；
        #   ② PG 精确余弦 —— 用 `memory_summaries.embedding`（迁移 0019 起这一列才有数据）；
        #   ③ PG 按时间 —— 连查询向量都拿不到（没有嵌入函数 / 嵌入失败 / 空 query）时的保底。
        # ②③ 都在 PG 里，所以"Milvus 没起来"不再等于"这一轮根本没有语义召回"。
        items: list[RecalledItem] = []
        if embedding:
            if resolve() == MILVUS:
                items = await self._milvus_search(tenant_id, user_id, embedding, top_k)
            if not items:
                items = await self._pg_vector_summaries(
                    tenant_id, user_id, embedding, top_k
                )
        if not items:
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
        embedding: list[float],
        top_k: int,
    ) -> list[RecalledItem]:
        """从 Milvus 做 ANN 检索；拿不到就返回空，由 `recall` 的下一级去查 PG。

        这里**不再**顺手降级到"按时间取"：那会跳过 ②（PG 里可能有向量、能算余弦），
        把一次 ANN 故障直接说成"只能按时间给你"。
        """
        coll = self._get_milvus_collection()
        if coll is None:
            return []

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
                            source="vector",
                        )
                    )
            self._milvus_fail.success()
            return items

        except Exception as e:
            self._milvus_fail.failure(f"search: {e}")
            return []

    async def _pg_vector_summaries(
        self,
        tenant_id: str,
        user_id: str,
        embedding: list[float],
        top_k: int,
    ) -> list[RecalledItem]:
        """② 在 PG 里做 1-vs-N 精确余弦（没有 ANN 索引，候选上限见 `CANDIDATE_LIMIT`）。

        取候选而不是取 top_k：这一级要的是"在全体有向量的摘要里挑最像的 k 条"，
        按时间取 k 条再在这 k 条里算相似度会挑出"新而不像"。
        """
        try:
            rows = await self._pool.fetch(
                f"""SELECT {_RECALL_COLUMNS}, embedding, embedding_model, embedding_dim
                   FROM memory_summaries
                   WHERE tenant_id=$1 AND user_id=$2 AND status='active'
                   ORDER BY created_at DESC
                   LIMIT $3""",
                tenant_id,
                user_id,
                CANDIDATE_LIMIT,
            )
        except Exception as e:
            logger.error("PG vector query failed for summaries: %s", e)
            return []

        # 只在同一向量空间里比：这一级挑的是"最像的 k 条"，而跨模型的余弦数虽然也在 0~1，
        # 却不对应任何语义 —— 它会把无关的摘要排到前面去（迁移 0020 记的正是这件事）。
        model_tag = active_embedding_model()
        scorer = CosineScorer(embedding)
        threshold = settings.memory_summary_min_cosine
        scored: list[tuple[float, Any]] = []
        skipped: list[Any] = []
        for row in rows:
            vec = parse_vector(row["embedding"])
            if not vec:
                # 迁移 0019 之前的行没有向量（本轮不做批量回填）：它仍然有资格被 ③ 按时间给出。
                continue
            recorded = (row.get("embedding_model"), row.get("embedding_dim"))
            if not same_vector_space(*recorded, model=model_tag, dim=len(embedding)):
                skipped.append(recorded)
                continue
            sim = scorer.score(vec)
            if sim < threshold:
                continue
            scored.append((sim, row))
        report_space_mismatch(skipped, model=model_tag, dim=len(embedding))

        scored.sort(key=lambda t: -t[0])
        return [
            self._row_to_recalled(row, score=sim, source="vector")
            for sim, row in scored[:top_k]
        ]

    async def _pg_recent_summaries(
        self,
        tenant_id: str,
        user_id: str,
        limit: int,
    ) -> list[RecalledItem]:
        """③ 按时间取最近摘要（保底，不是语义命中：source 记成 recent 让读侧如实报 mode）。"""
        try:
            rows = await self._pool.fetch(
                f"""SELECT {_RECALL_COLUMNS}
                   FROM memory_summaries
                   WHERE tenant_id=$1 AND user_id=$2 AND status='active'
                   ORDER BY created_at DESC
                   LIMIT $3""",
                tenant_id,
                user_id,
                limit,
            )
            return [
                self._row_to_recalled(row, score=0.0, source="recent") for row in rows
            ]
        except Exception as e:
            logger.error("PG query failed for summaries: %s", e)
            return []

    @staticmethod
    def _row_to_recalled(row: Any, *, score: float, source: str) -> RecalledItem:
        """memory_summaries 行 → RecalledItem（②③ 共用，差别只在 score 与 source）。"""
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

        return RecalledItem(
            id=row["id"],
            content=row["content"],
            topics=topics or [],
            entities=entities or {},
            turn_range=(row["turn_start"], row["turn_end"]),
            session_id=row["session_id"],
            access_count=row["access_count"],
            last_accessed_at=last_accessed,
            created_at=(
                row["created_at"].timestamp() if row["created_at"] else time.time()
            ),
            score=score,
            source=source,
        )

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
        """插入摘要（供 Consolidator 使用，兼容旧接口）。

        向量**透传给 `save_summary`** 而不是在这里再补一次：过去这两件事都干了 ——
        `save_summary` 自己按正文嵌一次并写 Milvus，这里又用调用方给的（`topics + 正文`）
        向量再写一次，同一个 `summary_id` 在 Milvus 里落两条**不同口径**的向量。
        """
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
            embedding=embedding,
            embedding_model=entry.embedding_model,
        )
        if result is None:
            raise RuntimeError("Failed to insert summary")

        created = await self.get_by_id(entry.tenant_id, entry.user_id, result)
        if created is None:
            raise RuntimeError(f"Summary not found after insert: {result}")

        # get_by_id 的投影不含向量，手上有就直接挂上，省一次 6KB 数组的往返。
        created.embedding = embedding
        created.embedding_model = entry.embedding_model
        created.embedding_dim = len(embedding) if embedding else None

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
        *,
        with_embedding: bool = False,
    ) -> list[SummaryEntry]:
        """列出活跃摘要。

        `with_embedding=True` 只该出现在要算余弦的地方（Consolidator 的近重复扫描、
        `MemoryService` 的退化召回）：默认投影不带向量，记忆面板列 50 条时
        反序列化 50×1536 个浮点数是纯浪费（同 M12 对容量淘汰那条路的处置）。
        """
        columns = (
            "id, tenant_id, user_id, session_id, content, topics, entities, "
            "turn_start, turn_end, content_hash, access_count, last_accessed_at, "
            "status, created_at, embedding, embedding_model, embedding_dim"
            if with_embedding
            else (
                "id, tenant_id, user_id, session_id, content, topics, entities, "
                "turn_start, turn_end, content_hash, access_count, last_accessed_at, "
                "status, created_at"
            )
        )
        try:
            rows = await self._pool.fetch(
                f"""SELECT {columns}
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
            # 投影没选这一列时 .get 返回 None = "这条没有向量"，与 NULL 同义。
            embedding=parse_vector(row.get("embedding")),
            embedding_model=row.get("embedding_model"),
            embedding_dim=row.get("embedding_dim"),
        )
