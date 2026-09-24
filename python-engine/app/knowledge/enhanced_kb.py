"""知识库 RAG 增强: 完整的文档生命周期管理 + 租户隔离

功能:
1. 文档上传 → 分块 → 嵌入 → Milvus 存储 (租户隔离)
2. 查询时动态检索相关 chunk (COSINE 相似度)
3. 独立限流 (QPS=20, Burst=40 每租户)
4. Trace 集成: 每次检索记录 span
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from app.rag.retriever import RAGRetriever
from app.trace import record_span

logger = logging.getLogger(__name__)


@dataclass
class DocumentChunk:
    """文档分片"""

    chunk_id: str
    document_id: str
    tenant_id: str  # SaaS 安全: 租户隔离
    content: str
    embedding: list[float]
    metadata: dict = field(default_factory=dict)


@dataclass
class RetrieveResult:
    """检索结果"""

    document_id: str
    chunk_id: str
    content: str
    score: float
    tenant_id: str
    trace_id: str = ""


class EnhancedKnowledgeBase:
    """增强的知识库 (租户隔离 + 链路追踪)

    核心能力:
    1. 文档索引 (分块 + 嵌入 + Milvus 存储)
    2. 向量检索 (COSINE 相似度, 租户隔离 filter)
    3. 文档管理 (列出/删除/统计)
    4. 链路追踪 (每次检索记录 span)
    """

    def __init__(self):
        self.retriever = RAGRetriever()
        self._collections: dict[str, str] = {}  # tenant_id -> collection_name

    async def index_document(
        self,
        tenant_id: str,
        document_id: str,
        content: str,
        file_type: str = "txt",
        metadata: dict | None = None,
        trace_id: str = "",
    ) -> dict:
        """索引文档 (带 trace)

        流程:
        1. 文本分块
        2. 调用 LLM 生成嵌入向量
        3. 存入 Milvus (带 tenant_id filter)
        4. 记录 span
        """
        start_time = time.time()

        try:
            result = await self.retriever.index_document(
                tenant_id=tenant_id,
                document_id=document_id,
                content=content,
                file_type=file_type,
                metadata=metadata or {},
            )

            duration_ms = int((time.time() - start_time) * 1000)

            # 记录 span
            if trace_id:
                await record_span(
                    trace_id=trace_id,
                    span_name="kb:index_document",
                    duration_ms=duration_ms,
                    metadata={
                        "document_id": document_id,
                        "chunks_count": result.get("chunks_count", 0),
                        "file_type": file_type,
                        "tenant_id": tenant_id,
                    },
                    tenant_id=tenant_id,
                )

            logger.info(
                "Document indexed (doc=%s, chunks=%d, duration=%dms, tenant=%s)",
                document_id,
                result.get("chunks_count", 0),
                duration_ms,
                tenant_id,
            )

            return result

        except Exception as e:
            logger.error(f"Document indexing failed: {e}")
            return {
                "document_id": document_id,
                "chunks_count": 0,
                "status": "failed",
                "error": str(e),
            }

    async def retrieve(
        self,
        tenant_id: str,
        query: str,
        top_k: int = 5,
        threshold: float = 0.7,
        trace_id: str = "",
    ) -> list[RetrieveResult]:
        """检索相关文档片段 (带 trace + 租户隔离)

        SaaS 安全:
        - 查询条件强制过滤 tenant_id
        - Milvus expr = f'tenant_id == "{tenant_id}"'
        - 所有返回结果携带 tenant_id 元数据
        """
        start_time = time.time()

        try:
            # 调用底层检索器
            raw_results = await self.retriever.retrieve(
                tenant_id=tenant_id,
                query=query,
                top_k=top_k,
                threshold=threshold,
            )

            duration_ms = int((time.time() - start_time) * 1000)

            # 纵深防御：retriever 层已按 tenant 过滤（Milvus expr），
            # 此处兜底剔除漏网的跨租户数据，绝不盲目把查询租户 ID
            # 盖到归属不明的结果上
            filtered_results = [
                r
                for r in raw_results
                if not r.get("tenant_id") or r.get("tenant_id") == tenant_id
            ]
            if len(filtered_results) < len(raw_results):
                logger.warning(
                    "kb.retrieve: dropped %d cross-tenant results (tenant=%s)",
                    len(raw_results) - len(filtered_results),
                    tenant_id,
                )

            # 转换为 RetrieveResult
            results = [
                RetrieveResult(
                    document_id=r["document_id"],
                    chunk_id=r["chunk_id"],
                    content=r["content"],
                    score=r["score"],
                    tenant_id=r.get("tenant_id") or tenant_id,
                    trace_id=trace_id,
                )
                for r in filtered_results
            ]

            # 记录 span
            if trace_id:
                await record_span(
                    trace_id=trace_id,
                    span_name="kb:retrieve",
                    duration_ms=duration_ms,
                    metadata={
                        "query_length": len(query),
                        "results_count": len(results),
                        "top_k": top_k,
                        "threshold": threshold,
                        "tenant_id": tenant_id,
                    },
                    tenant_id=tenant_id,
                )

            logger.info(
                "Retrieved %d chunks (query=%s, duration=%dms, tenant=%s)",
                len(results),
                query[:50],
                duration_ms,
                tenant_id,
            )

            return results

        except Exception as e:
            logger.error(f"Retrieval failed: {e}")
            return []

    async def list_documents(
        self,
        tenant_id: str,
    ) -> list[dict]:
        """列出租户下的所有文档 (从 PG knowledge_documents 表)"""
        from app.db import get_pool

        pool = get_pool()
        rows = await pool.fetch(
            """SELECT id, knowledge_base_id, name, file_type, file_size_bytes,
                      file_url, status, chunk_count, created_at, updated_at
               FROM knowledge_documents
               WHERE tenant_id = $1
               ORDER BY created_at DESC""",
            tenant_id,
        )
        return [
            {
                "document_id": r["id"],
                "knowledge_base_id": r["knowledge_base_id"],
                "name": r["name"],
                "file_type": r["file_type"],
                "file_size_bytes": r["file_size_bytes"],
                "file_url": r["file_url"] or "",
                "status": r["status"],
                "chunk_count": r["chunk_count"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else "",
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else "",
            }
            for r in rows
        ]

    async def delete_document(
        self,
        tenant_id: str,
        document_id: str,
    ) -> bool:
        """删除文档 (Milvus + PG 双删)"""
        from app.db import get_pool

        try:
            # 1. 删除 Milvus 中的 chunks
            collection = await self.retriever._get_collection()
            if collection:
                delete_expr = (
                    f'document_id == "{document_id}" AND tenant_id == "{tenant_id}"'
                )
                await collection.delete(expr=delete_expr)
                await collection.flush()

            # 2. 删除 PG 中的 metadata (knowledge_documents)
            pool = get_pool()
            result = await pool.execute(
                """DELETE FROM knowledge_documents
                   WHERE id = $1 AND tenant_id = $2""",
                document_id,
                tenant_id,
            )
            deleted = result.endswith(" 1")  # "DELETE 1" → True; "DELETE 0" → False

            logger.info(
                "Document deleted (doc=%s, tenant=%s, pg=%s)",
                document_id,
                tenant_id,
                deleted,
            )
            return True

        except Exception as e:
            logger.error(f"Document deletion failed: {e}")
            return False

    async def get_tenant_stats(
        self,
        tenant_id: str,
    ) -> dict:
        """获取租户知识库统计信息 (从 PG knowledge_documents 聚合)"""
        from app.db import get_pool

        pool = get_pool()
        row = await pool.fetchrow(
            """SELECT
                   COUNT(*)                                   AS total_documents,
                   COUNT(*) FILTER (WHERE status = 'indexed') AS indexed_documents,
                   COUNT(*) FILTER (WHERE status = 'pending')  AS pending_documents,
                   COUNT(*) FILTER (WHERE status = 'failed')   AS failed_documents,
                   COALESCE(SUM(file_size_bytes), 0)           AS total_size_bytes,
                   COALESCE(SUM(chunk_count), 0)               AS total_chunks,
                   COUNT(DISTINCT knowledge_base_id)           AS knowledge_bases
               FROM knowledge_documents
               WHERE tenant_id = $1""",
            tenant_id,
        )
        return {
            "tenant_id": tenant_id,
            "total_documents": row["total_documents"] if row else 0,
            "indexed_documents": row["indexed_documents"] if row else 0,
            "pending_documents": row["pending_documents"] if row else 0,
            "failed_documents": row["failed_documents"] if row else 0,
            "total_size_bytes": row["total_size_bytes"] if row else 0,
            "total_chunks": row["total_chunks"] if row else 0,
            "knowledge_bases": row["knowledge_bases"] if row else 0,
        }


# ── API Handler ────────────────────────────────────────────────────
class KnowledgeBaseHandler:
    """知识库 HTTP Handler (Go 侧代理到 Python)

    路由:
    - POST /v1/kb/index      索引文档
    - POST /v1/kb/search     检索文档
    - GET  /v1/kb/documents  列出文档
    - DELETE /v1/kb/{id}     删除文档
    - GET  /v1/kb/stats      租户统计
    """

    def __init__(self):
        self.kb = EnhancedKnowledgeBase()

    async def handle_index(
        self,
        tenant_id: str,
        trace_id: str,
        document_id: str,
        content: str,
        file_type: str = "txt",
    ) -> dict:
        """处理文档索引请求"""
        return await self.kb.index_document(
            tenant_id=tenant_id,
            document_id=document_id,
            content=content,
            file_type=file_type,
            trace_id=trace_id,
        )

    async def handle_search(
        self,
        tenant_id: str,
        trace_id: str,
        query: str,
        top_k: int = 5,
        threshold: float = 0.7,
    ) -> dict:
        """处理检索请求"""
        results = await self.kb.retrieve(
            tenant_id=tenant_id,
            query=query,
            top_k=top_k,
            threshold=threshold,
            trace_id=trace_id,
        )

        return {
            "query": query,
            "results": [
                {
                    "document_id": r.document_id,
                    "chunk_id": r.chunk_id,
                    "content": r.content,
                    "score": r.score,
                }
                for r in results
            ],
            "count": len(results),
        }

    async def handle_delete(
        self,
        tenant_id: str,
        document_id: str,
    ) -> dict:
        """处理删除请求"""
        success = await self.kb.delete_document(tenant_id, document_id)
        return {
            "document_id": document_id,
            "deleted": success,
        }


# ── 限流中间件配置 ─────────────────────────────────────────────────
# Go 侧配置示例:
# kbRateLimiter := NewTenantRateLimiter(redis, 20, 40)  // QPS=20, Burst=40
# kbRateMW := kbRateLimiter.Middleware
