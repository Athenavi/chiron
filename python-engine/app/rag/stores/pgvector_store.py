"""
PostgreSQL pgvector 向量存储实现
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.rag.stores.base import VectorStoreBase

logger = logging.getLogger(__name__)

# 表达式里的字段名 → 本表列名。写死映射而不是"任意合法标识符"：调用方按 Milvus
# 的 schema 命名（doc_id），本表列名不同（document_id），放行标识符只会放行错列。
# kb_id 不在这里 —— 它由 search() 的 collection 参数承担，见 _translate_filter。
_FILTER_COLUMNS = {
    "tenant_id": "tenant_id",
    "doc_id": "document_id",
}

# 单个 `field == "字符串字面量"`，值允许双引号或单引号、允许 Milvus 反斜杠转义。
# 锚定在头部，配合 _translate_filter 的循环把整条表达式逐段吃完。
_EQ_PREDICATE = re.compile(
    r"""\s*(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*==\s*"""
    r"""(?:(?P<dq>"(?:[^"\\]|\\.)*")|(?P<sq>'(?:[^'\\]|\\.)*'))"""
)
_AND = re.compile(r"\s+and\s+", re.IGNORECASE)


def _unescape_milvus_string(raw: str) -> str:
    """去掉 Milvus 表达式里的反斜杠转义，还原调用方本意的那个值。"""
    return re.sub(r"\\(.)", r"\1", raw[1:-1])


def _translate_filter(filter_expr: str) -> list[tuple[str, Any]]:
    """Milvus 口径的过滤表达式 → [(本表列名, 参数值)]，供参数化下推。

    只承认「`field == "value"` 的合取」这一子集（历史上三个调用方产出的就是这个形状）。
    翻译不了必须抛，不能退化成空结果：向量腿静默为空会被读成"知识库里没东西"，
    而真实原因通常是接线或表达式写法错了 —— 这正是本文件此前的故障。
    """
    rest = filter_expr
    out: list[tuple[str, Any]] = []
    while True:
        m = _EQ_PREDICATE.match(rest)
        if not m:
            raise ValueError(f"无法翻译向量过滤表达式: {filter_expr!r}")
        field = m.group("field")
        raw = m.group("dq") if m.group("dq") is not None else m.group("sq")
        if field != "kb_id":
            # kb_id 是 Milvus 里对"哪个集合"的复述。本表不按集合分表，
            # KB 约束由 search(collection=...) 下推；在这里再拼一次只会和它冲突
            # （RAGBuilder 写入裸 kb_id，HybridRetriever 传 kb_<id 的 - 换 _>），
            # 两个口径不一致时静默恒空。以 collection 为准。
            column = _FILTER_COLUMNS.get(field)
            if column is None:
                raise ValueError(f"向量过滤字段不受支持: {field!r} (表达式 {filter_expr!r})")
            out.append((column, _unescape_milvus_string(raw)))
        rest = rest[m.end():]
        tail = _AND.match(rest)
        if not tail:
            break
        rest = rest[tail.end():]
    if rest.strip():
        raise ValueError(f"向量过滤表达式含不支持的写法: {filter_expr!r}")
    return out


class PgvectorStore(VectorStoreBase):
    """pgvector 向量存储"""

    def __init__(
        self, pool: Any, table_name: str = "knowledge_chunk_vectors", embedding_dim: int = 1536
    ) -> None:
        """
        Args:
            pool: asyncpg 连接池或兼容对象
            table_name: 向量表名
            embedding_dim: 向量维度
        """
        self._pool = pool
        self._table = table_name
        self._dim = embedding_dim

    async def _ensure_table(self) -> None:
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
        payloads: list[dict[str, Any]],
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
    ) -> list[dict[str, Any]]:
        """搜索相似向量（余弦相似度）"""
        table = self._validate_table_name(self._table)
        qv = self._format_vector(query_vector)

        # 构建 WHERE 子句
        # 安全：kb_id 通过参数化查询传入，避免注入
        params: list[Any] = [collection]
        conditions = [f"knowledge_base_id = ${len(params)}"]

        if filter_expr:
            # 此前是把表达式清洗字符后原样拼进 WHERE。Milvus 写法（`kb_id == "x"`）
            # 在 PG 里既不是合法语法也不是合法列名，查询报错又被下面的 except 吞成
            # []，故障表现是"混合检索只剩全文腿"而不是异常 —— 现在逐条翻译成
            # 参数化条件，翻译不了就抛，让接线错误可见。
            for column, value in _translate_filter(filter_expr):
                params.append(value)
                conditions.append(f"{column} = ${len(params)}")

        # 占位符必须跟着 params 长度走：过滤条件多一条，向量/阈值/limit 就后移一位
        n = len(params)
        vec = f"${n + 1}::vector"
        score = f"1 - (embedding <=> {vec})"
        where_clause = " AND ".join(conditions)
        query = f"""
            SELECT id, document_id, chunk_index, content,
                   {score} AS score
            FROM {table}
            WHERE {where_clause}
              AND {score} >= ${n + 2}
            ORDER BY embedding <=> {vec}
            LIMIT ${n + 3}
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
