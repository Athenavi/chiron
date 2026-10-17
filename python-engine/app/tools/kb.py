"""kb 工具集 — 知识库检索能力工具化（六大互通的「知识库」侧）。

- kb_list：列出当前用户可见的知识库（自有 + public），供 agent 选择检索目标
- kb_search：在指定知识库内检索（wiki 全文检索 / RAG），返回片段与来源

归属校验：自己的 / visibility=public / 同租户共享的三选一才放行，规则与 API 层同源
（`app.kb_access`，过去这里内联了一份"owner 或 public"的副本，把租户共享的库判给不了）。
当前用户与租户取自工具上下文（agent/对话/工作流运行时已设置）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import DEFAULT_TENANT_ID
from app.kb_access import kb_access_allowed, kb_storage_tenant
from app.tools.context import get_tenant_id, get_user_id
from app.tools.registry import registry

logger = logging.getLogger(__name__)


async def _pool() -> Any:
    from app.db import get_pool

    try:
        return get_pool()
    except RuntimeError as e:
        raise RuntimeError(f"database not available: {e}") from e


async def kb_list(query: str = "", limit: int = 20) -> dict[str, Any]:
    """列出当前用户可见的知识库（自有 + public），可按名称过滤。"""
    user_id = get_user_id() or ""
    if not user_id:
        return {"error": "authentication context missing"}
    if limit <= 0 or limit > 100:
        limit = 20
    try:
        pool = await _pool()
    except RuntimeError as e:
        return {"error": str(e)}

    params: list[Any] = [user_id, get_tenant_id() or DEFAULT_TENANT_ID]
    where = (
        "(user_id = $1 OR visibility = 'public'"
        " OR (visibility = 'tenant' AND tenant_id = $2))"
    )
    if query:
        where += f" AND name ILIKE ${len(params) + 1}"
        params.append(f"%{query}%")
    params.append(limit)

    rows = await pool.fetch(
        f"""SELECT id, name, COALESCE(description, '') as description, type, visibility,
                   document_count
            FROM knowledge_bases
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${len(params)}""",
        *params,
    )
    kbs = [
        {
            "id": r["id"],
            "name": r["name"],
            "description": r["description"],
            "type": r["type"],
            "visibility": r["visibility"],
            "document_count": r["document_count"],
        }
        for r in rows
    ]
    return {"count": len(kbs), "knowledge_bases": kbs}


async def kb_search(kb_id: str, query: str, top_k: int = 5) -> dict[str, Any]:
    """在指定知识库内检索（wiki 全文检索；RAG 待实现时提示）。"""
    if not kb_id or not query:
        return {"error": "kb_id and query are required"}
    if top_k <= 0 or top_k > 20:
        top_k = 5
    user_id = get_user_id() or ""
    if not user_id:
        return {"error": "authentication context missing"}
    try:
        pool = await _pool()
    except RuntimeError as e:
        return {"error": str(e)}

    kb_row = await pool.fetchrow(
        """SELECT id, type, user_id, visibility, tenant_id
           FROM knowledge_bases WHERE id = $1""",
        kb_id,
    )
    if kb_row is None:
        return {"error": f"knowledge base not found: {kb_id}"}
    if not kb_access_allowed(kb_row, user_id, get_tenant_id()):
        return {"error": "not authorized to query this knowledge base"}

    kb_type = kb_row["type"]
    if kb_type == "wiki":
        try:
            rows = await pool.fetch(
                """SELECT id, name, file_type,
                          ts_rank(to_tsvector('chinese', COALESCE(convert_from(content, 'UTF8'), '')),
                                  plainto_tsquery('chinese', $1)) AS rank
               FROM knowledge_documents
               WHERE knowledge_base_id = $2
                 AND status = 'completed'
                 AND to_tsvector('chinese', COALESCE(convert_from(content, 'UTF8'), ''))
                     @@ plainto_tsquery('chinese', $1)
               ORDER BY rank DESC
               LIMIT $3""",
                query,
                kb_id,
                top_k,
            )
        except Exception as e:  # noqa: BLE001 — 全文检索失败降级返回空结果
            logger.warning("kb_search full-text failed (kb_id=%s): %s", kb_id, e)
            rows = []
        results = [
            {
                "id": r["id"],
                "name": r["name"],
                "file_type": r["file_type"],
                "rank": float(r["rank"]),
            }
            for r in rows
        ]
        return {"type": kb_type, "results": results}
    else:
        # RAG 向量检索：走新栈 RAGBuilder（kb_* collection + tenant 过滤）。
        # 旧 RAGRetriever 查共享 collection 且只按 tenant 收窄，会串读同租户其它库。
        try:
            from app.main import get_gateway
            from app.rag.builder import RAGBuilder

            builder = RAGBuilder(llm_gateway=await get_gateway())
            hits = await builder.query(
                kb_id=kb_id,
                query=query,
                top_k=top_k,
                threshold=0.45,
                tenant_id=kb_storage_tenant(kb_row),
            )
            results = [
                {
                    "document_id": h.get("doc_id", ""),
                    "chunk_index": h.get("chunk_index", 0),
                    "content": h.get("content", "")[:500],
                    "score": round(float(h.get("score", 0.0)), 4),
                }
                for h in hits
            ]
            return {"type": kb_type, "results": results}
        except Exception as e:
            logger.warning("kb_search RAG failed (kb_id=%s): %s", kb_id, e)
            return {"type": kb_type, "results": [], "error": str(e)}


registry.register(
    name="kb_list",
    description="List knowledge bases visible to the current user (own + public). Use this before kb_search to find a knowledge base id.",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "default": "",
                "description": "Optional name filter",
            },
            "limit": {"type": "integer", "default": 20},
        },
    },
    handler=kb_list,
)

registry.register(
    name="kb_search",
    description="Search a knowledge base by id and query; returns ranked document hits with sources. Use kb_list first to obtain kb_id.",
    parameters={
        "type": "object",
        "properties": {
            "kb_id": {
                "type": "string",
                "description": "Knowledge base id (from kb_list)",
            },
            "query": {"type": "string", "description": "Search query"},
            "top_k": {"type": "integer", "default": 5},
        },
        "required": ["kb_id", "query"],
    },
    handler=kb_search,
)
