"""kb 工具集 — 知识库能力工具化。

- ``kb_list``：列出当前用户可见的知识库（自有 + public + 同租户共享）
- ``kb_search``：在指定知识库内提问（PageIndex 推理式检索），返回答案与引用

**工具名刻意保持不变**：它们是 agent 能力面的稳定契约 —— ``app/core/capabilities``、
``app/core/task_router``、``app/core/agent_skill_selector`` 都按这些名字编排与选择能力，
改名等于在所有编排点上做一次同步手术。换掉的是实现（向量 + FTS → PageIndex）。

**provider 解析用知识库所有者的设置**：Cloud key 是用户级的（只有所有者配过），
调用者身份只用于权限校验（``kb_access_allowed``）。两件事混用会得到"能看见库却查不了"
（拿别人的身份去找 key）。

**返回形状变了**：旧 ``kb_search`` 返回 ``{results: [片段]}``，现在返回
``{answer, citations}`` —— PageIndex 是推理式检索，没有"top-k 相似片段"这一层。
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from app.config import DEFAULT_TENANT_ID
from app.kb_access import kb_access_allowed
from app.knowledge.errors import KnowledgeError
from app.knowledge.service import answer_question
from app.tools.context import get_tenant_id, get_tenant_id_int, get_user_id
from app.tools.registry import registry

logger = logging.getLogger(__name__)


async def _pool() -> Any:
    from app.db import get_pool

    try:
        return get_pool()
    except RuntimeError as e:
        raise RuntimeError(f"database not available: {e}") from e


async def kb_list(query: str = "", limit: int = 20) -> dict[str, Any]:
    """列出当前用户可见的知识库，可按名称过滤。"""
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
        f"""SELECT id, name, COALESCE(description, '') AS description, provider, visibility,
                   status, document_count
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
            "provider": r["provider"],
            "visibility": r["visibility"],
            "status": r["status"],
            "document_count": r["document_count"],
        }
        for r in rows
    ]
    return {"count": len(kbs), "knowledge_bases": kbs}


async def kb_search(kb_id: str, query: str, top_k: int = 5) -> dict[str, Any]:
    """在指定知识库内提问，返回答案与引用。

    ``top_k`` 仅为兼容旧调用方保留（推理式检索由 provider 决定读多少节点）。
    """
    if not kb_id or not query:
        return {"error": "kb_id and query are required"}
    user_id = get_user_id() or ""
    if not user_id:
        return {"error": "authentication context missing"}
    try:
        pool = await _pool()
    except RuntimeError as e:
        return {"error": str(e)}

    kb_row = await pool.fetchrow(
        """SELECT id, user_id, tenant_id, visibility, provider, status
             FROM knowledge_bases WHERE id = $1""",
        kb_id,
    )
    if kb_row is None:
        return {"error": f"knowledge base not found: {kb_id}"}
    if not kb_access_allowed(kb_row, user_id, get_tenant_id_int()):
        return {"error": "not authorized to query this knowledge base"}

    owner_id = str(kb_row["user_id"] or "")
    try:
        result, provider_name = await answer_question(
            kb_id=kb_id, user_id=owner_id, question=query
        )
    except KnowledgeError as exc:
        logger.warning("kb_search failed (kb_id=%s): %s", kb_id, exc)
        return {"error": str(exc), "provider": str(kb_row["provider"] or "")}

    citations = [asdict(c) for c in result.citations]
    return {
        "provider": provider_name,
        "answer": result.answer,
        "citations": citations,
        "count": len(citations),
    }


registry.register(
    name="kb_list",
    description=(
        "List knowledge bases visible to the current user (own + public + same-tenant). "
        "Use this before kb_search to find a knowledge base id."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "default": "", "description": "Optional name filter"},
            "limit": {"type": "integer", "default": 20},
        },
    },
    handler=kb_list,
)

registry.register(
    name="kb_search",
    description=(
        "Ask a question against a knowledge base; returns a reasoned answer with citations "
        "(document name / page). Use kb_list first to obtain kb_id."
    ),
    parameters={
        "type": "object",
        "properties": {
            "kb_id": {"type": "string", "description": "Knowledge base id (from kb_list)"},
            "query": {"type": "string", "description": "Natural-language question"},
            "top_k": {"type": "integer", "default": 5, "description": "Kept for compatibility"},
        },
        "required": ["kb_id", "query"],
    },
    handler=kb_search,
)
