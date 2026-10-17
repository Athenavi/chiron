"""rag_query 工具 — Agent 推理中使用的混合检索工具。

利用 HybridRetriever + RAGContextInjector 在 Agent 对话中实时检索知识库，
返回格式化的上下文片段供 LLM 参考。

kb_id 由模型给出，检索前按 `app.kb_access` 的可见性规则核验归属；
向量/全文两腿的租户过滤值取自 KB 行，与写入侧同源。

用法（Agent 自动触发）：
  工具名称: rag_query
  参数: { query, kb_id, top_k, use_hybrid, threshold }
"""

from __future__ import annotations

import logging
from typing import Any

from app.kb_access import kb_access_allowed, kb_storage_tenant
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

    pool = retriever._pg_pool  # noqa: SLF001

    # 如果没有指定 kb_id，尝试从用户上下文获取默认知识库
    if not kb_id:
        try:
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

    # kb_id 完全来自模型输出（可被提示注入操纵），必须先证明这个库调用者看得见；
    # 没有 PG 就无从核验 —— 宁可拒绝检索，也不要按调用方自称的租户读别人的库。
    if pool is None:
        return {"error": "knowledge base access cannot be verified: database unavailable"}
    try:
        kb_row = await pool.fetchrow(
            """SELECT id, user_id, visibility, tenant_id
               FROM knowledge_bases WHERE id = $1""",
            kb_id,
        )
    except Exception as e:  # noqa: BLE001 — 核验失败一律拒绝，不放行
        logger.warning("rag_query: kb 归属核验失败 kb_id=%s: %s", kb_id, e)
        return {"error": f"knowledge base access check failed: {e}"}
    if kb_row is None:
        return {"error": f"knowledge base not found: {kb_id}"}
    if not kb_access_allowed(kb_row, user_id, get_tenant_id()):
        return {"error": "not authorized to query this knowledge base"}

    try:
        results = await retriever.hybrid_search(
            query=query,
            kb_id=kb_id,
            # 过滤值取 KB 行（与写入侧同源），不再用 get_tenant_id() or "default"：
            # 那个值既可能是"没人认领"的字符串 default（恒零结果），也可能与库的真实租户不符。
            tenant_id=kb_storage_tenant(kb_row),
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
