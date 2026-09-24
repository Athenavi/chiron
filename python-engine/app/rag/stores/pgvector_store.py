"""
PostgreSQL pgvector 向量存储实现
"""

from __future__ import annotations

import logging
import re

from app.rag.stores.base import VectorStoreBase

logger = logging.getLogger(__name__)


class PgvectorStore(VectorStoreBase):
    """pgvector 向量存储"""

    def __init__(self, pool, table_name: str = "knowledge_chunk_vectors", embedding_dim: int = 1536):
        """
        Args:
            pool: asyncpg 连接池或兼容对象
            table_name: 向量表名
            embedding_dim: 向量维度
        """
        self._pool = pool
        self._table = table_name
        self._dim = embedding_dim

    async def _ensure_table(self):
        """确保表和扩展存在"""
        table = self._validate_table_name(self._table)
        async with self._pool.acquire() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    id TEXT PRIMARY KEY,
                    knowledge_base_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL DEFAULT 0,
                    content TEXT NOT NULL DEFAULT '',
                    embedding vector({self._dim})
                )
            """)
            # 创建索引（如果不存在）
            idx_name = f"idx_{table}_embedding"
            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS {idx_name}
                ON {table}
                USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 100)
            """)
            # 创建辅助索引
            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{table}_kb
                ON {table} (knowledge_base_id, tenant_id)
            """)

    @staticmethod
    def _format_vector(v: list[float]) -> str:
        """格式化向量为 pgvector 文本格式"""
        return f"[{','.join(format(float(x), '.8f') for x in v)}]"

    # ── 公开接口 ──

    async def insert(
        self,
        collection: str,
        ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict],
    ) -> int:
        """插入向量数据"""
        table = self._validate_table_name(self._table)
        await self._ensure_table()

        rows = []
        for _i, (vid, vec, payload) in enumerate(zip(ids, vectors, payloads, strict=False)):
            if len(vec) != self._dim:
                logger.warning("向量维度 %d != %d, 跳过", len(vec), self._dim)
                continue
            rows.append((
                vid,
                payload.get("kb_id", collection),
                payload.get("doc_id", ""),
                payload.get("tenant_id", ""),
                payload.get("chunk_index", 0),
                payload.get("content", "")[:65000],
                self._format_vector(vec),
            ))

        if not rows:
            raise ValueError("无有效向量可存储，pgvector 存储失败")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    f"""
                    INSERT INTO {table}
                        (id, knowledge_base_id, document_id, tenant_id, chunk_index, content, embedding)
                    SELECT id, knowledge_base_id, document_id, tenant_id, chunk_index, content, embedding::vector
                    FROM unnest(
                        $1::text[], $2::text[], $3::text[], $4::text[], $5::int[],
                        $6::text[], $7::text[]
                    ) AS t(id, knowledge_base_id, document_id, tenant_id, chunk_index, content, embedding)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    [r[0] for r in rows],
                    [r[1] for r in rows],
                    [r[2] for r in rows],
                    [r[3] for r in rows],
                    [r[4] for r in rows],
                    [r[5] for r in rows],
                    [r[6] for r in rows],
                )
        logger.info("pgvector insert: %d vectors", len(rows))
        return len(rows)

    async def search(
        self,
        collection: str,
        query_vector: list[float],
        top_k: int = 5,
        threshold: float = 0.5,
        filter_expr: str | None = None,
    ) -> list[dict]:
        """搜索相似向量（余弦相似度）"""
        table = self._validate_table_name(self._table)
        qv = self._format_vector(query_vector)

        # 构建 WHERE 子句
        # 安全：kb_id 通过参数化查询传入，避免注入
        conditions = [("knowledge_base_id = $1", [collection])]
        params: list = [collection]

        if filter_expr:
            # 安全：filter_expr 仅支持简单的 `field == "value"` 模式
            # 只允许字母数字、下划线、空格、引号、== 和括号
            # 移除所有可能造成 SQL 注入的字符
            cleaned = re.sub(r"[^A-Za-z0-9_\"' ==()]", "", filter_expr)
            # 将内部双引号转义为 SQL 安全形式
            cleaned = cleaned.replace("'", "''")
            conditions.append((f"({cleaned})", []))

        where_clause = " AND ".join(c[0] for c in conditions)
        query = f"""
            SELECT id, document_id, chunk_index, content,
                   1 - (embedding <=> $2::vector) AS score
            FROM {table}
            WHERE {where_clause}
              AND 1 - (embedding <=> $2::vector) >= $3
            ORDER BY embedding <=> $2::vector
            LIMIT $4
        """
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(query, *params, qv, threshold, top_k)
        except Exception as e:
            logger.error("pgvector search failed: %s", e)
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

    async def delete(
        self,
        collection: str,
        ids: list[str],
    ) -> int:
        """按主键 ID 列表删除"""
        table = self._validate_table_name(self._table)
        if not ids:
            return 0
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                f"DELETE FROM {table} WHERE id = ANY($1::text[])",
                ids,
            )
            # result 格式 "DELETE N"
            count = int(result.split()[-1]) if result.startswith("DELETE") else 0
            return count

    async def delete_by_document(
        self,
        collection: str,
        doc_id: str,
        tenant_id: str,
    ) -> int:
        """按文档 ID + 租户删除"""
        table = self._validate_table_name(self._table)
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                f"DELETE FROM {table} WHERE document_id = $1 AND tenant_id = $2",
                doc_id,
                tenant_id,
            )
            count = int(result.split()[-1]) if result.startswith("DELETE") else 0
            logger.info("pgvector delete_by_document: %s -> %d rows", doc_id, count)
            return count

    async def delete_by_kb(
        self,
        collection: str,
        kb_id: str,
        tenant_id: str,
    ) -> int:
        """按知识库 ID + 租户删除"""
        table = self._validate_table_name(self._table)
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                f"DELETE FROM {table} WHERE knowledge_base_id = $1 AND tenant_id = $2",
                kb_id,
                tenant_id,
            )
            count = int(result.split()[-1]) if result.startswith("DELETE") else 0
            logger.info("pgvector delete_by_kb: %s -> %d rows", kb_id, count)
            return count

    async def ensure_collection(
        self,
        name: str,
        dim: int,
        index_type: str = "IVF_FLAT",
    ) -> None:
        """确保表存在（dim 不影响表结构，仅记录日志）"""
        self._dim = dim
        await self._ensure_table()
        logger.info("pgvector table ensured: %s (dim=%d)", self._table, dim)

    async def close(self) -> None:
        """pgvector 无需释放连接（连接池由调用方管理）"""
        pass
