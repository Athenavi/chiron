"""rag_query 工具 — Agent 推理中使用的混合检索工具。

利用 HybridRetriever + RAGContextInjector 在 Agent 对话中实时检索知识库，
返回格式化的上下文片段供 LLM 参考。

用法（Agent 自动触发）：
  工具名称: rag_query
  参数: { query, kb_id, top_k, use_hybrid, threshold }
"""

from __future__ import annotations

import logging
from typing import Any

from app.tools.context import get_tenant_id, get_user_id
from app.tools.registry import registry

logger = logging.getLogger(__name__)


async def _get_hybrid_retriever() -> Any:
    """延迟初始化 HybridRetriever（依赖 MilvusStore + PG 连接池）"""
    from app.config import settings
    from app.db import get_pool
    from app.rag.hybrid_search import HybridRetriever
    from app.rag.stores.milvus_store import MilvusStore

    pool = None
    try:
        pool = get_pool()
    except RuntimeError:
        logger.warning("rag_query: PG pool not available, fulltext search disabled")

    vector_store = None
    try:
        milvus_addr = settings.milvus_address
        if milvus_addr:
            host = milvus_addr.split(":")[0]
            port = int(milvus_addr.split(":")[1]) if ":" in milvus_addr else 19530
            vector_store = MilvusStore(host=host, port=port)
    except Exception as e:
        logger.warning("rag_query: Milvus not available: %s", e)

    return HybridRetriever(vector_store=vector_store, pg_pool=pool)


async def rag_query(
    query: str,
    kb_id: str = "",
    top_k: int = 5,
    use_hybrid: bool = True,
    threshold: float = 0.45,
) -> dict[str, Any]:
    """在知识库中执行混合检索（向量 + 全文），返回相关文档片段。

    Agent 推理过程中自动调用，无需手动触发。
    """
    tenant_id = get_tenant_id() or "default"
    user_id = get_user_id() or ""
    if not user_id:
        return {"error": "authentication context missing"}

    if not query.strip():
        return {"error": "query is required"}

    if top_k <= 0 or top_k > 20:
        top_k = 5

    try:
        retriever = await _get_hybrid_retriever()
    except Exception as e:
        logger.error("rag_query: retriever init failed: %s", e)
        return {"error": f"retriever unavailable: {e}"}

    # 如果没有指定 kb_id，尝试从用户上下文获取默认知识库
    if not kb_id:
        try:
            pool = retriever._pg_pool  # noqa: SLF001
            if pool:
                row = await pool.fetchrow(
                    """SELECT id FROM knowledge_bases
                       WHERE user_id = $1 AND type = 'rag'
                       ORDER BY created_at DESC LIMIT 1""",
                    user_id,
                )
                if row:
                    kb_id = row["id"]
        except Exception as e:
            logger.debug("rag_query: no default kb found: %s", e)

    if not kb_id:
        return {"error": "no knowledge base id provided and no default found"}

    try:
        results = await retriever.hybrid_search(
            query=query,
            kb_id=kb_id,
            tenant_id=tenant_id,
            top_k=top_k,
            threshold=threshold,
        )
    except Exception as e:
        logger.error("rag_query: hybrid_search failed: %s", e)
        return {"error": str(e), "results": []}

    # 格式化返回
    formatted = [
        {
            "id": r.get("id", ""),
            "content": r.get("content", "")[:1000],
            "doc_id": r.get("doc_id", ""),
            "chunk_index": r.get("chunk_index", 0),
            "score": round(r.get("score", 0.0), 4),
            "source": r.get("source", "vector"),
        }
        for r in results
    ]

    return {
        "kb_id": kb_id,
        "query": query,
        "count": len(formatted),
        "results": formatted,
    }


registry.register(
    name="rag_query",
    description="Hybrid search (vector + full-text) across RAG knowledge base. Returns ranked document chunks with scores. Call this when the user asks questions that may be answered by their knowledge base.",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query (natural language question)",
            },
            "kb_id": {
                "type": "string",
                "description": "Knowledge base id (optional; uses default if omitted)",
                "default": "",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return",
                "default": 5,
            },
            "use_hybrid": {
                "type": "boolean",
                "description": "Enable hybrid search (vector + full-text), otherwise pure vector",
                "default": True,
            },
            "threshold": {
                "type": "number",
                "description": "Minimum similarity threshold (0.0-1.0)",
                "default": 0.45,
            },
        },
        "required": ["query"],
    },
    handler=rag_query,
)
