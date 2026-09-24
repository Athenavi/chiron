"""
混合检索（Hybrid Search）— 向量检索 + 全文检索 + RRF 融合

核心策略：
1. 向量检索（余弦相似度）— 语义级匹配
2. PostgreSQL 全文检索（tsvector + tsquery）— 关键词级匹配
3. RRF (Reciprocal Rank Fusion) 融合两部分结果
4. 统一返回格式
"""

from __future__ import annotations

import logging
from typing import Any

from app.rag.stores import VectorStoreBase

logger = logging.getLogger(__name__)


class HybridRetriever:
    """混合检索器

    同时执行向量检索与全文检索，用 RRF 算法融合排名。
    """

    def __init__(
        self,
        vector_store: VectorStoreBase | None = None,
        pg_pool=None,
        rrf_k: int = 60,
    ):
        """
        Args:
            vector_store: 向量存储实例（MilvusStore / PgvectorStore）
            pg_pool: asyncpg 连接池（用于全文检索）
            rrf_k: RRF 融合常数，越大越平滑
        """
        self._vector_store = vector_store
        self._pg_pool = pg_pool
        self._rrf_k = rrf_k

    @property
    def vector_store(self) -> VectorStoreBase | None:
        return self._vector_store

    @vector_store.setter
    def vector_store(self, store: VectorStoreBase | None):
        self._vector_store = store

    async def hybrid_search(
        self,
        query: str,
        kb_id: str,
        tenant_id: str,
        top_k: int = 5,
        threshold: float = 0.5,
    ) -> list[dict[str, Any]]:
        """混合检索 — 向量 + 全文 + RRF 融合

        Returns:
            [{id, content, doc_id, chunk_index, score, source}]
            score 为 RRF 融合分数（0-1），source 标记 vector / fts / hybrid
        """
        # 1. 向量检索
        vec_results = await self._vector_search(query, kb_id, tenant_id, top_k)

        # 2. 全文检索
        fts_results = await self._fulltext_search(query, kb_id, tenant_id, top_k)

        # 3. RRF 融合
        fused = self._rrf_fuse(vec_results, fts_results, top_k)

        # 4. 标记来源并归一化分数
        vec_ids = {r["id"] for r in vec_results}
        fts_ids = {r["id"] for r in fts_results}

        for item in fused:
            if item["id"] in vec_ids and item["id"] in fts_ids:
                item["source"] = "hybrid"
            elif item["id"] in vec_ids:
                item["source"] = "vector"
            else:
                item["source"] = "fts"

        logger.info(
            "hybrid_search: q=%s vec=%d fts=%d fused=%d",
            query[:30], len(vec_results), len(fts_results), len(fused),
        )
        return fused

    async def _vector_search(
        self,
        query: str,
        kb_id: str,
        tenant_id: str,
        top_k: int,
        threshold: float = 0.5,
    ) -> list[dict[str, Any]]:
        """向量检索"""
        if not self._vector_store:
            logger.warning("vector_store 未配置，跳过向量检索")
            return []

        from app.llm.client import llm_client

        try:
            query_embedding = await llm_client.embed(query)
        except Exception as e:
            logger.error("向量检索嵌入失败: %s", e)
            return []

        if not query_embedding:
            return []

        safe_kb = self._vector_store._sanitize_expr(kb_id)
        safe_tenant = self._vector_store._sanitize_expr(tenant_id)
        filter_expr = f'kb_id == "{safe_kb}" AND tenant_id == "{safe_tenant}"'

        try:
            results = await self._vector_store.search(
                collection=kb_id,
                query_vector=query_embedding,
                top_k=top_k,
                threshold=threshold,
                filter_expr=filter_expr,
            )
        except Exception as e:
            logger.error("向量检索失败: %s", e)
            return []

        return results

    async def _fulltext_search(
        self,
        query: str,
        kb_id: str,
        tenant_id: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """PostgreSQL 全文检索

        使用 to_tsvector / plainto_tsquery 做中文 + 英文全文匹配。
        需要数据库表 knowledge_chunks 或 knowledge_documents 支持 tsvector 列。
        """
        if not self._pg_pool:
            logger.warning("pg_pool 未配置，跳过全文检索")
            return []

        # 清理查询词（防止 SQL 注入）
        cleaned = " ".join(query.split()[:50])
        if not cleaned.strip():
            return []

        try:
            async with self._pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT kc.id, kc.content, kc.chunk_index, kc.document_id,
                           ts_rank(to_tsvector('simple', kc.content),
                                   plainto_tsquery('simple', $1)) AS score
                    FROM knowledge_chunks kc
                    WHERE kc.knowledge_base_id = $2
                      AND kc.tenant_id = $3
                      AND to_tsvector('simple', kc.content) @@ plainto_tsquery('simple', $1)
                    ORDER BY score DESC
                    LIMIT $4
                    """,
                    cleaned,
                    kb_id,
                    tenant_id,
                    top_k,
                )
        except Exception as e:
            # 表可能不存在（未初始化全文索引）
            logger.debug("全文检索失败（可能无索引）: %s", e)
            return []

        return [
            {
                "id": row["id"],
                "content": row["content"],
                "doc_id": row["document_id"],
                "chunk_index": row["chunk_index"],
                "score": row["score"],
            }
            for row in rows
        ]

    def _rrf_fuse(
        self,
        vec_results: list[dict],
        fts_results: list[dict],
        top_k: int,
    ) -> list[dict]:
        """RRF (Reciprocal Rank Fusion) 融合

        每个元素在各自列表中的排名倒数即为分数：
        score = sum(1 / (k + rank))
        """
        if not vec_results:
            return fts_results[:top_k]
        if not fts_results:
            return vec_results[:top_k]

        # 构建排名映射
        vec_ranks = {r["id"]: i + 1 for i, r in enumerate(vec_results)}
        fts_ranks = {r["id"]: i + 1 for i, r in enumerate(fts_results)}

        all_ids = set(vec_ranks.keys()) | set(fts_ranks.keys())

        # 融合分数
        fused_scores: dict[str, float] = {}
        for id_ in all_ids:
            score = 0.0
            if id_ in vec_ranks:
                score += 1.0 / (self._rrf_k + vec_ranks[id_])
            if id_ in fts_ranks:
                score += 1.0 / (self._rrf_k + fts_ranks[id_])
            fused_scores[id_] = score

        # 按融合分数排序
        sorted_ids = sorted(fused_scores, key=fused_scores.get, reverse=True)[:top_k]

        # 合并结果（用向量结果中的内容，若没有则用全文结果）
        id_to_item: dict[str, dict] = {}
        for r in vec_results:
            id_to_item[r["id"]] = r
        for r in fts_results:
            if r["id"] not in id_to_item:
                id_to_item[r["id"]] = r

        out = []
        for id_ in sorted_ids:
            item = dict(id_to_item.get(id_, {}))
            item["score"] = fused_scores[id_]
            out.append(item)

        return out

    async def close(self) -> None:
        """释放资源"""
        if self._vector_store:
            await self._vector_store.close()
