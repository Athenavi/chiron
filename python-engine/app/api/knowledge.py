"""Knowledge Base API — CRUD operations with PostgreSQL."""

from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.db import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/kb", tags=["knowledge"])

# 单租户默认租户 ID（与 Go internal/db/seed.go 保持一致；两表 tenant_id 为 NOT NULL）
DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"


# ── Models ──


class KnowledgeBaseCreate(BaseModel):
    name: str
    description: str = ""
    type: str = "wiki"  # wiki | rag
    visibility: str = "private"  # public | private


class KnowledgeBaseUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    type: Optional[str] = None
    visibility: Optional[str] = None


class DocumentUpload(BaseModel):
    name: str
    file_type: str
    file_size_bytes: int = 0
    content: str = ""  # base64 or plain text


class KBQuery(BaseModel):
    query: str
    top_k: int = 5


# ── Helpers ──


def _serialize_kb(row: dict) -> dict:
    """Serialize a knowledge_bases row to JSON response."""
    config = row.get("config")
    if config and isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError:
            config = None
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row.get("description", ""),
        "type": row["type"],
        "visibility": row["visibility"],
        "status": row["status"],
        "document_count": row.get("document_count", 0),
        "total_size_bytes": row.get("total_size_bytes", 0),
        "credits_consumed": row.get("credits_consumed", 0),
        "config": config,
        "created_at": (
            row["created_at"].isoformat()
            if isinstance(row["created_at"], datetime)
            else str(row["created_at"])
        ),
        "updated_at": (
            row["updated_at"].isoformat()
            if isinstance(row["updated_at"], datetime)
            else str(row["updated_at"])
        ),
    }


def _serialize_doc(row: dict) -> dict:
    """Serialize a knowledge_documents row to JSON response."""
    metadata = row.get("metadata")
    if metadata and isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = None
    return {
        "id": row["id"],
        "knowledge_base_id": row["knowledge_base_id"],
        "name": row["name"],
        "file_url": row.get("file_url", ""),
        "file_type": row.get("file_type", ""),
        "file_size_bytes": row.get("file_size_bytes", 0),
        "chunk_count": row.get("chunk_count", 0),
        "status": row["status"],
        "error_message": row.get("error_message", ""),
        "metadata": metadata,
        "created_at": (
            row["created_at"].isoformat()
            if isinstance(row["created_at"], datetime)
            else str(row["created_at"])
        ),
    }


# ── Core Functions (testable without FastAPI) ──


async def list_knowledge_bases(user_id: str, tenant_id: str = "") -> dict:
    """List knowledge bases visible to this user.

    可见范围：
      - 自己的（user_id 匹配）
      - 公开的（visibility='public'，有意跨租户）
      - **同租户共享的**（visibility='tenant'）← 必须同时匹配 tenant_id

    最后一条原先只写了 `visibility = 'tenant'`、没有租户条件 —— 这等于把
    "租户内共享"实现成了"全站共享"：任何租户的用户都能列出并读取别人的知识库。
    """
    try:
        pool = get_pool()
    except RuntimeError:
        return {"knowledge_bases": [], "count": 0}

    rows = await pool.fetch(
        """SELECT id, name, COALESCE(description, '') as description, type, visibility, status,
                  document_count, total_size_bytes, credits_consumed, config, created_at, updated_at
           FROM knowledge_bases
           WHERE user_id = $1
              OR visibility = 'public'
              OR (visibility = 'tenant' AND tenant_id = $2)
           ORDER BY created_at DESC""",
        user_id,
        tenant_id or DEFAULT_TENANT_ID,
    )
    kbs = [_serialize_kb(dict(row)) for row in rows]
    return {"knowledge_bases": kbs, "count": len(kbs)}


async def create_knowledge_base(
    user_id: str,
    name: str,
    description: str = "",
    kb_type: str = "wiki",
    visibility: str = "private",
    tenant_id: str = "",
) -> dict:
    """Create a new knowledge base.

    tenant_id 必须由调用方（网关注入的租户）提供。原先恒写 DEFAULT_TENANT_ID 常量，
    导致所有租户的知识库都落在同一个"默认租户"上 —— 既然 visibility='tenant'
    是按 tenant_id 判断可见性的，写入端的租户就必须是真实的，否则两边永远对不上。
    """
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="name must not be empty")
    if kb_type not in ("wiki", "rag"):
        raise HTTPException(status_code=400, detail="type must be 'wiki' or 'rag'")
    if visibility not in ("public", "private", "tenant"):
        raise HTTPException(
            status_code=400,
            detail="visibility must be 'public', 'private', or 'tenant'",
        )

    pool = get_pool()
    kb_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    await pool.execute(
        """INSERT INTO knowledge_bases (id, tenant_id, user_id, name, description, type, visibility, status, created_at, updated_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, 'active', $8, $8)""",
        kb_id,
        tenant_id or DEFAULT_TENANT_ID,
        user_id,
        name.strip(),
        description,
        kb_type,
        visibility,
        now,
    )
    return {
        "id": kb_id,
        "name": name.strip(),
        "type": kb_type,
        "visibility": visibility,
    }


async def get_knowledge_base(kb_id: str, user_id: str, tenant_id: str = "") -> dict:
    """Get a single knowledge base by id（自己的 / 公开的 / 同租户共享的）。

    与 ``list_knowledge_bases`` 用**同一套**可见性规则。两处一旦不一致，
    就会出现"列表里看不到、但猜到 id 就能直接读"的绕过。
    """
    pool = get_pool()
    row = await pool.fetchrow(
        """SELECT id, name, COALESCE(description, '') as description, type, visibility, status,
                  document_count, total_size_bytes, credits_consumed, config, created_at, updated_at
           FROM knowledge_bases
           WHERE id = $1
             AND (user_id = $2
                  OR visibility = 'public'
                  OR (visibility = 'tenant' AND tenant_id = $3))""",
        kb_id,
        user_id,
        tenant_id or DEFAULT_TENANT_ID,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return _serialize_kb(dict(row))


async def update_knowledge_base(
    kb_id: str,
    user_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    kb_type: Optional[str] = None,
    visibility: Optional[str] = None,
) -> dict:
    """Update a knowledge base (owner only). Public KBs cannot change type."""
    pool = get_pool()

    # Verify ownership
    existing = await pool.fetchrow(
        "SELECT id, user_id, visibility FROM knowledge_bases WHERE id = $1",
        kb_id,
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if existing["user_id"] != user_id:
        raise HTTPException(
            status_code=403, detail="not authorized to update this knowledge base"
        )

    # Public KBs can't change type
    if kb_type is not None and existing["visibility"] == "public":
        raise HTTPException(
            status_code=400, detail="cannot change type of a public knowledge base"
        )

    if kb_type is not None and kb_type not in ("wiki", "rag"):
        raise HTTPException(status_code=400, detail="type must be 'wiki' or 'rag'")

    # Dynamic UPDATE — only set provided fields
    updates: list[str] = []
    params: list[Any] = []
    idx = 1
    if name is not None:
        if not name.strip():
            raise HTTPException(status_code=400, detail="name must not be empty")
        updates.append(f"name = ${idx}")
        params.append(name.strip())
        idx += 1
    if description is not None:
        updates.append(f"description = ${idx}")
        params.append(description)
        idx += 1
    if kb_type is not None:
        updates.append(f"type = ${idx}")
        params.append(kb_type)
        idx += 1
    if visibility is not None:
        if visibility not in ("public", "private", "tenant"):
            raise HTTPException(
                status_code=400,
                detail="visibility must be 'public', 'private', or 'tenant'",
            )
        updates.append(f"visibility = ${idx}")
        params.append(visibility)
        idx += 1

    if not updates:
        raise HTTPException(status_code=400, detail="no fields to update")

    updates.append(f"updated_at = ${idx}")
    params.append(datetime.now(timezone.utc))
    idx += 1

    params.append(kb_id)
    query = f"UPDATE knowledge_bases SET {', '.join(updates)} WHERE id = ${idx}"
    await pool.execute(query, *params)

    return {"id": kb_id, "updated": True}


async def delete_knowledge_base(
    kb_id: str, user_id: str, is_admin: bool = False
) -> dict:
    """Delete a knowledge base. Admin can delete any; users can only delete their own."""
    pool = get_pool()

    existing = await pool.fetchrow(
        "SELECT id, user_id FROM knowledge_bases WHERE id = $1",
        kb_id,
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")

    if not is_admin and existing["user_id"] != user_id:
        raise HTTPException(
            status_code=403, detail="not authorized to delete this knowledge base"
        )

    await pool.execute("DELETE FROM knowledge_bases WHERE id = $1", kb_id)
    return {"id": kb_id, "deleted": True}


# ── Core Functions — Documents ──


async def upload_document(
    kb_id: str,
    user_id: str,
    name: str,
    file_type: str,
    file_size_bytes: int = 0,
    content: bytes | None = None,
) -> dict:
    """Upload a document into a knowledge base.

    Verifies the KB exists, is not currently building, inserts the document
    row (including raw content for later async RAG build), and atomically
    updates the KB aggregate stats.
    """
    pool = get_pool()

    # Verify KB exists and is owned by (or visible to) user
    kb_row = await pool.fetchrow(
        "SELECT id, status, user_id FROM knowledge_bases WHERE id = $1",
        kb_id,
    )
    if kb_row is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if kb_row["user_id"] != user_id:
        raise HTTPException(
            status_code=403, detail="not authorized to upload to this knowledge base"
        )

    # Reject if KB is currently building
    if kb_row["status"] == "building":
        raise HTTPException(
            status_code=409,
            detail="knowledge base is currently building, try again later",
        )

    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="document name must not be empty")

    doc_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # Insert the document
    await pool.execute(
        """INSERT INTO knowledge_documents
               (id, tenant_id, knowledge_base_id, user_id, name, file_type, file_size_bytes, status, content, created_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8, $9)""",
        doc_id,
        DEFAULT_TENANT_ID,
        kb_id,
        user_id,
        name.strip(),
        file_type,
        file_size_bytes,
        content,
        now,
    )

    # Update KB aggregate stats
    await pool.execute(
        """UPDATE knowledge_bases
           SET document_count = document_count + 1,
               total_size_bytes = total_size_bytes + $1,
               updated_at = $2
           WHERE id = $3""",
        file_size_bytes,
        now,
        kb_id,
    )

    return {
        "id": doc_id,
        "knowledge_base_id": kb_id,
        "name": name.strip(),
        "file_type": file_type,
        "file_size_bytes": file_size_bytes,
        "status": "pending",
        "created_at": now.isoformat(),
    }


async def list_documents(kb_id: str, user_id: str) -> dict:
    """List all documents in a knowledge base."""
    pool = get_pool()

    # Verify KB exists and is accessible
    kb_row = await pool.fetchrow(
        "SELECT id, user_id, visibility FROM knowledge_bases WHERE id = $1",
        kb_id,
    )
    if kb_row is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if kb_row["user_id"] != user_id and kb_row["visibility"] != "public":
        raise HTTPException(
            status_code=403, detail="not authorized to view this knowledge base"
        )

    rows = await pool.fetch(
        """SELECT id, knowledge_base_id, name, file_url, file_type, file_size_bytes,
                  chunk_count, status, error_message, metadata, created_at
           FROM knowledge_documents
           WHERE knowledge_base_id = $1
           ORDER BY created_at DESC""",
        kb_id,
    )
    docs = [_serialize_doc(dict(row)) for row in rows]
    return {"documents": docs, "count": len(docs)}


@router.delete("/{kb_id}/documents")
async def delete_doc(
    kb_id: str,
    request: Request,
    user_id: str = Query("", alias="user_id"),
    doc_id: str = Query("", alias="doc_id"),
) -> dict:
    """删除单个文档（前端批量删除循环调用；删除后重算 KB 统计）。"""
    if not doc_id:
        raise HTTPException(status_code=400, detail="doc_id is required")
    pool = get_pool()

    kb_row = await pool.fetchrow(
        "SELECT id, user_id FROM knowledge_bases WHERE id = $1", kb_id
    )
    if kb_row is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if kb_row["user_id"] != user_id:
        raise HTTPException(
            status_code=403, detail="not authorized to delete documents"
        )

    await pool.execute(
        "DELETE FROM knowledge_documents WHERE id = $1 AND knowledge_base_id = $2",
        doc_id,
        kb_id,
    )
    await pool.execute(
        """UPDATE knowledge_bases
           SET document_count = (SELECT COUNT(*) FROM knowledge_documents WHERE knowledge_base_id = $1),
               total_size_bytes = COALESCE((SELECT SUM(file_size_bytes) FROM knowledge_documents WHERE knowledge_base_id = $1), 0),
               updated_at = NOW()
           WHERE id = $1""",
        kb_id,
    )
    return {"deleted": doc_id, "knowledge_base_id": kb_id}


# ── Core Functions — Build & Query ──


async def _enqueue_rag_build(
    kb_id: str, user_id: str, estimated_cost: float, tenant_id: str = ""
) -> str:
    """投递 rag_index 异步任务（RAG 知识库构建）"""

    from app.queue.producer import QueueProducer
    from app.redis_client import get_redis

    pool = get_pool()
    docs = await pool.fetch(
        "SELECT id, file_type, name FROM knowledge_documents WHERE knowledge_base_id = $1",
        kb_id,
    )
    redis = await get_redis()
    try:
        producer = QueueProducer(redis)
        task_id = await producer.enqueue(
            "rag_index",
            # 这里曾经写成 tenant_id=user_id —— 把用户 ID 当租户用。后果是同一租户的
            # 多个用户各自形成一个"伪租户"，与 knowledge_bases 里的真实 tenant_id 对不上：
            # 要么检索不到（功能故障），要么形成按用户切分的假隔离。
            tenant_id=tenant_id or DEFAULT_TENANT_ID,
            payload={
                "kb_id": kb_id,
                "user_id": user_id,
                "documents": [
                    {
                        "doc_id": d["id"],
                        "file_type": d["file_type"],
                        "filename": d["name"],
                    }
                    for d in docs
                ],
                "estimated_cost": estimated_cost,
            },
        )
        return task_id
    finally:
        await redis.aclose()


async def build_knowledge_base(kb_id: str, user_id: str, tenant_id: str = "") -> dict:
    """Build a knowledge base — mark documents as completed and deduct credits."""
    pool = get_pool()

    # Fetch KB
    kb_row = await pool.fetchrow(
        """SELECT id, type, status, document_count, total_size_bytes, user_id
           FROM knowledge_bases WHERE id = $1""",
        kb_id,
    )
    if kb_row is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if kb_row["user_id"] != user_id:
        raise HTTPException(
            status_code=403, detail="not authorized to build this knowledge base"
        )

    doc_count = kb_row["document_count"] or 0
    if doc_count == 0:
        raise HTTPException(
            status_code=400, detail="cannot build knowledge base with no documents"
        )

    # Calculate cost（credits 为 INTEGER 列，统一向上取整与 Go 侧整数口径一致）
    char_count = kb_row["total_size_bytes"] or 0
    kb_type = kb_row["type"]
    coefficient = 0.5 if kb_type == "wiki" else 2.0
    rate = 0.001
    estimated_cost = min(math.ceil(char_count * coefficient * rate), 499)

    # Check user credits
    user_row = await pool.fetchrow(
        "SELECT credits FROM users WHERE id = $1",
        user_id,
    )
    if user_row is None:
        raise HTTPException(status_code=404, detail="user not found")

    user_credits = user_row["credits"] or 0
    if user_credits < estimated_cost:
        raise HTTPException(status_code=402, detail="insufficient credits")

    # Mark KB as building（条件更新：并发下仅一个请求能抢到，防 TOCTOU 双投递）
    now = datetime.now(timezone.utc)
    res = await pool.execute(
        "UPDATE knowledge_bases SET status = 'building', updated_at = $1 WHERE id = $2 AND status <> 'building'",
        now,
        kb_id,
    )
    if res.startswith("UPDATE 0"):
        raise HTTPException(
            status_code=409, detail="knowledge base is currently building"
        )

    if kb_type == "rag":
        # 异步 RAG 构建：投递 rag_index 任务，由 queue worker 执行向量构建，
        # 完成后由 worker 更新文档状态、激活知识库并扣费
        try:
            task_id = await _enqueue_rag_build(kb_id, user_id, estimated_cost, tenant_id=tenant_id)
        except Exception:
            # 回滚 building 状态，避免 Redis 不可用时 KB 永久卡死（上传被 409 拒绝）
            await pool.execute(
                "UPDATE knowledge_bases SET status = 'draft', updated_at = $1 WHERE id = $2",
                datetime.now(timezone.utc),
                kb_id,
            )
            raise
        return {
            "status": "building",
            "task_id": task_id,
            "estimated_cost": round(estimated_cost, 4),
            "doc_count": doc_count,
            "type": kb_type,
        }

    # Transaction: update documents, deduct credits, activate KB
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """UPDATE knowledge_documents
                   SET status = 'completed'
                   WHERE knowledge_base_id = $1""",
                kb_id,
            )
            await conn.execute(
                "UPDATE users SET credits = credits - $1 WHERE id = $2",
                estimated_cost,
                user_id,
            )
            await conn.execute(
                """UPDATE knowledge_bases
                   SET status = 'active', credits_consumed = credits_consumed + $1, updated_at = $2
                   WHERE id = $3""",
                estimated_cost,
                now,
                kb_id,
            )

    return {
        "status": "active",
        "estimated_cost": round(estimated_cost, 4),
        "doc_count": doc_count,
        "type": kb_type,
    }


async def query_knowledge_base(
    kb_id: str,
    user_id: str,
    query: str,
    top_k: int = 5,
) -> dict:
    """Query a knowledge base using full-text search (wiki) or RAG."""
    pool = get_pool()

    # Fetch KB and verify access
    kb_row = await pool.fetchrow(
        """SELECT id, type, user_id, visibility
           FROM knowledge_bases WHERE id = $1""",
        kb_id,
    )
    if kb_row is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if kb_row["user_id"] != user_id and kb_row["visibility"] != "public":
        raise HTTPException(
            status_code=403, detail="not authorized to query this knowledge base"
        )

    kb_type = kb_row["type"]

    if kb_type == "wiki":
        try:
            # content 列为 BYTEA（异步 RAG 构建时保存的原始文件字节），
            # 全文搜索前必须转回文本；convert_from 对非法 UTF-8 会抛错，
            # 捕获后降级返回空结果而非 500（二进制文档本就不适合全文检索）
            rows = await pool.fetch(
                """SELECT id, knowledge_base_id, name, file_type, status,
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
        except Exception as e:
            logger.warning("wiki full-text search failed (kb_id=%s): %s", kb_id, e)
            rows = []
        results = [
            {
                "id": row["id"],
                "knowledge_base_id": row["knowledge_base_id"],
                "name": row["name"],
                "file_type": row["file_type"],
                "rank": float(row["rank"]),
            }
            for row in rows
        ]
    else:
        # RAG 向量检索
        try:
            from app.rag.retriever import RAGRetriever

            retriever = RAGRetriever()
            # 从 KB 获取 tenant_id
            tenant_row = await pool.fetchrow(
                "SELECT tenant_id FROM knowledge_bases WHERE id = $1",
                kb_id,
            )
            tenant_id = tenant_row["tenant_id"] if tenant_row else "default"
            hits = await retriever.retrieve(
                tenant_id=tenant_id,
                query=query,
                top_k=top_k,
                threshold=0.45,
            )
            results = [
                {
                    "document_id": h.get("document_id", ""),
                    "chunk_id": h.get("chunk_id", ""),
                    "content": h.get("content", "")[:500],
                    "score": round(h.get("score", 0.0), 4),
                }
                for h in hits
            ]
        except Exception as e:
            logger.warning("RAG query failed (kb_id=%s): %s", kb_id, e)
            results = []

    return {"type": kb_type, "results": results}


async def admin_list_knowledge_bases() -> dict:
    """List all public knowledge bases (admin/owner only)."""
    try:
        pool = get_pool()
    except RuntimeError:
        return {"knowledge_bases": [], "count": 0}

    rows = await pool.fetch(
        """SELECT id, name, COALESCE(description, '') as description, type, visibility, status,
                  document_count, total_size_bytes, credits_consumed, config, created_at, updated_at
           FROM knowledge_bases
           WHERE visibility = 'public'
           ORDER BY created_at DESC""",
    )
    kbs = [_serialize_kb(dict(row)) for row in rows]
    return {"knowledge_bases": kbs, "count": len(kbs)}


# ── Routes ──


@router.get("")
async def list_kb(
    request: Request,
    user_id: str = Query("", alias="user_id"),
    tenant_id: str = Query("", alias="tenant_id"),
):
    """List knowledge bases."""
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    return await list_knowledge_bases(user_id, tenant_id=tenant_id)


@router.post("")
async def create_kb(
    request: Request,
    body: KnowledgeBaseCreate,
    user_id: str = Query("", alias="user_id"),
    tenant_id: str = Query("", alias="tenant_id"),
):
    """Create a knowledge base."""
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    return await create_knowledge_base(
        user_id=user_id,
        name=body.name,
        description=body.description,
        kb_type=body.type,
        visibility=body.visibility,
        tenant_id=tenant_id,
    )


@router.get("/admin/list")
async def admin_list_kb(request: Request):
    """Admin endpoint: list all public knowledge bases."""
    role = request.headers.get("X-User-Role", "")
    if role not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="admin or owner role required")
    return await admin_list_knowledge_bases()


@router.get("/{kb_id}")
async def get_kb(
    kb_id: str,
    request: Request,
    user_id: str = Query("", alias="user_id"),
    tenant_id: str = Query("", alias="tenant_id"),
):
    """Get a knowledge base by id."""
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    return await get_knowledge_base(kb_id=kb_id, user_id=user_id, tenant_id=tenant_id)


@router.put("/{kb_id}")
async def update_kb(
    kb_id: str,
    request: Request,
    body: KnowledgeBaseUpdate,
    user_id: str = Query("", alias="user_id"),
):
    """Update a knowledge base."""
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    return await update_knowledge_base(
        kb_id=kb_id,
        user_id=user_id,
        name=body.name,
        description=body.description,
        kb_type=body.type,
        visibility=body.visibility,
    )


@router.delete("/{kb_id}")
async def delete_kb(
    kb_id: str,
    request: Request,
    user_id: str = Query("", alias="user_id"),
):
    """Delete a knowledge base（仅限拥有者）。

    这里曾经接受 `?is_admin=true` 作为"管理员可删任意知识库"的判据 ——
    但那是**调用方自报**的查询参数，等于把授权决定权交给了请求者。
    网关虽然会重写查询串、使它在网关路径下暂不可利用，但那是"靠上游兜住"，
    不是这里的正确性：任何直连路径（引擎端口可达 / 内部 token 泄露）都能立刻利用。

    真正的管理员删除应当走独立的 admin 端点，并基于服务端可信身份判断。
    """
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    return await delete_knowledge_base(kb_id=kb_id, user_id=user_id)


@router.post("/{kb_id}/documents")
async def upload_doc(
    kb_id: str,
    request: Request,
    body: DocumentUpload,
    user_id: str = Query("", alias="user_id"),
):
    """Upload a document to a knowledge base."""
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    # content 字段: base64 或纯文本 → 解码为 bytes 落库（异步 RAG 构建时使用）
    content = None
    if body.content:
        try:
            import base64

            # validate=True：非法 base64 字符直接抛错，避免纯文本被静默解成垃圾字节
            content = base64.b64decode(body.content, validate=True)
        except Exception:
            content = body.content.encode("utf-8")
    return await upload_document(
        kb_id=kb_id,
        user_id=user_id,
        name=body.name,
        file_type=body.file_type,
        file_size_bytes=body.file_size_bytes,
        content=content,
    )


@router.get("/{kb_id}/documents")
async def list_docs(
    kb_id: str,
    request: Request,
    user_id: str = Query("", alias="user_id"),
):
    """List documents in a knowledge base."""
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    return await list_documents(kb_id=kb_id, user_id=user_id)


@router.post("/{kb_id}/build")
async def build_kb(
    kb_id: str,
    request: Request,
    user_id: str = Query("", alias="user_id"),
    tenant_id: str = Query("", alias="tenant_id"),
):
    """Build a knowledge base."""
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    return await build_knowledge_base(kb_id=kb_id, user_id=user_id, tenant_id=tenant_id)


@router.post("/{kb_id}/query")
async def query_kb(
    kb_id: str,
    request: Request,
    body: KBQuery,
    user_id: str = Query("", alias="user_id"),
):
    """Query a knowledge base."""
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    return await query_knowledge_base(
        kb_id=kb_id,
        user_id=user_id,
        query=body.query,
        top_k=body.top_k,
    )


@router.get("/{kb_id}/documents/{doc_id}/preview")
async def preview_document(
    kb_id: str,
    doc_id: str,
    request: Request,
    user_id: str = Query("", alias="user_id"),
):
    """预览文档内容 — 返回文档元信息及前 N 个分块的内容拼接"""
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")

    # 验证知识库归属
    kb_row = await get_pool().fetchrow(
        "SELECT id, user_id, visibility FROM knowledge_bases WHERE id = $1",
        kb_id,
    )
    if kb_row is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if kb_row["user_id"] != user_id and kb_row["visibility"] != "tenant":
        raise HTTPException(status_code=403, detail="access denied")

    pool = get_pool()
    doc_row = await pool.fetchrow(
        """SELECT id, knowledge_base_id, name, file_type, file_size_bytes,
                  chunk_count, status, error_message, metadata, created_at
           FROM knowledge_documents
           WHERE id = $1 AND knowledge_base_id = $2""",
        doc_id,
        kb_id,
    )
    if doc_row is None:
        raise HTTPException(status_code=404, detail="document not found")

    doc = dict(doc_row)

    # 尝试从 knowledge_chunks 表获取前 5 个分块作为预览内容
    content_parts = []
    try:
        chunk_rows = await pool.fetch(
            """SELECT content FROM knowledge_chunks
               WHERE document_id = $1 AND knowledge_base_id = $2
               ORDER BY chunk_index ASC
               LIMIT 5""",
            doc_id,
            kb_id,
        )
        content_parts = [r["content"] for r in chunk_rows if r["content"]]
    except Exception:
        # knowledge_chunks 表可能不存在
        pass

    doc["content"] = "\n\n".join(content_parts) if content_parts else ""
    if len(doc["content"]) > 10000:
        doc["content"] = doc["content"][:10000] + "\n\n…（内容已截断，仅展示前 10000 字符）"

    # 删除 metadata 中可能存在的敏感信息（如 file_url 中的签名）
    if "metadata" in doc and isinstance(doc["metadata"], dict):
        doc["metadata"] = {k: v for k, v in doc["metadata"].items() if k not in ("file_url", "signed_url")}

    return {"data": doc}
