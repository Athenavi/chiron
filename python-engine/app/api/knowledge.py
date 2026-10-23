"""Knowledge Base API — PageIndex 驱动的知识库。

与旧实现的四处**语义变化**（前端与工具都按新语义消费）：

1. **provider 二选一**：知识库的 ``provider`` 在创建时确定，且必须与用户当前的
   ``user_knowledge_settings.provider`` 一致。不一致返回 409 —— 否则会出现"库建在自托管、
   用户却已切到 Cloud"，表现为"传了文档却永远查不到"。
2. **上传 ≠ 索引**：上传把原文写进对象存储并落 ``pending``；索引是队列任务 ``kb_index``。
   旧实现把"投递任务"当成功，检索时才发现是空库。
3. **查询返回答案 + 引用**：PageIndex 是推理式检索，没有"top-k 相似片段"这个概念。
4. **不在这里扣费**：Cloud 由 PageIndex 按页计费，自托管的成本是 LLM 用量。Chiron 侧的
   计费口径需要产品定（``credits_consumed`` 字段保留但不再自增）。
"""

from __future__ import annotations

import base64
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.config import DEFAULT_TENANT_ID
from app.db import get_pool
from app.kb_access import kb_access_allowed
from app.knowledge import blob, settings_store
from app.knowledge.errors import KnowledgeError, ProviderUnavailableError
from app.knowledge.providers import registry
from app.knowledge.providers.base import (
    KNOWN_PROVIDERS,
    STATUS_DISABLED,
    STATUS_ERROR,
    is_known_provider,
)
from app.knowledge.service import answer_question

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/kb", tags=["knowledge"])

#: 单次上传的原文上限（base64 解码后）。SaaS 下必须有个闸门：
#: 没有上限的上传等于把对象存储与内存交给调用方。
MAX_UPLOAD_BYTES = 64 * 1024 * 1024

#: 预览时按文本读取的扩展名（PDF 等二进制交给下载/引用，不做文本内联）
_TEXT_PREVIEW_SUFFIXES = {".txt", ".md", ".csv", ".json", ".html", ".htm", ".log"}

_ALLOWED_VISIBILITY = ("public", "private", "tenant")


def _now_naive() -> datetime:
    """当前 UTC 时间（**naive**）。

    知识库表的时间列是 ``timestamp without time zone``：asyncpg 对这类列传 aware datetime 会
    直接抛 ``can't subtract offset-naive and offset-aware datetimes``。统一按 UTC 的 naive 值落库。
    """
    return datetime.now(UTC).replace(tzinfo=None)


# ── Models ──


class KnowledgeBaseCreate(BaseModel):
    name: str
    description: str = ""
    provider: str = ""  # selfhosted | cloud（留空 = 跟随用户设置）
    visibility: str = "private"  # public | private | tenant


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    visibility: str | None = None


class DocumentUpload(BaseModel):
    name: str
    file_type: str = ""
    file_size_bytes: int = 0
    content: str = ""  # base64 或纯文本


class BuildBody(BaseModel):
    #: 重建索引。增量模式会跳过已 ready 的文档 —— 升级 pageindex 后必须走它。
    force: bool = False


class KBQuery(BaseModel):
    query: str
    top_k: int = 5  # 保留字段以兼容旧前端；推理式检索不使用 top-k


class UserSettingsUpdate(BaseModel):
    provider: str
    cloud_api_key: str | None = None  # 省略 = 沿用已存的 key；空串 = 清空


# ── Serialization ──


def _serialize_kb(row: Any) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data["id"],
        "name": data["name"],
        "description": data.get("description") or "",
        "provider": data["provider"],
        "visibility": data["visibility"],
        "status": data["status"],
        "document_count": data.get("document_count", 0),
        "total_size_bytes": data.get("total_size_bytes", 0),
        "credits_consumed": data.get("credits_consumed", 0),
        "error_message": data.get("error_message") or "",
        "config": _maybe_json(data.get("config")),
        "created_at": _iso(data.get("created_at")),
        "updated_at": _iso(data.get("updated_at")),
    }


def _serialize_doc(row: Any) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data["id"],
        "knowledge_base_id": data["knowledge_base_id"],
        "name": data["name"],
        "file_type": data.get("file_type") or "",
        "file_size_bytes": data.get("file_size_bytes", 0),
        "page_count": data.get("page_count", 0),
        "status": data["status"],
        "pageindex_doc_id": data.get("pageindex_doc_id") or "",
        "error_message": data.get("error_message") or "",
        "metadata": _maybe_json(data.get("metadata")),
        "created_at": _iso(data.get("created_at")),
        "updated_at": _iso(data.get("updated_at")),
    }


def _iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value else "")


def _maybe_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value:
        import json

        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return None


# ── Access helpers ──


def _require_kb_access(kb_row: Any, user_id: str, tenant_id: str, *, action: str) -> None:
    """KB 可见性 gate —— 规则本体在 ``app.kb_access``，与工具层共用一份。"""
    if not kb_access_allowed(kb_row, user_id, tenant_id):
        raise HTTPException(
            status_code=403, detail=f"not authorized to {action} this knowledge base"
        )


async def _load_user_provider(user_id: str) -> str:
    """取用户当前 provider；未选择时给出可操作的提示（400 而非 500）。"""
    user_settings = await settings_store.load(user_id)
    if user_settings is None or not user_settings.provider:
        raise HTTPException(
            status_code=400,
            detail="请先在知识库设置中选择 provider（自托管 / PageIndex Cloud）",
        )
    return user_settings.provider


async def _assert_provider_matches(*, user_id: str, kb_provider: str) -> None:
    current = await _load_user_provider(user_id)
    if current != kb_provider:
        raise HTTPException(
            status_code=409,
            detail=(
                f"该知识库使用 {kb_provider}，与当前设置 {current} 不一致；"
                "请先切换设置或改用一致的知识库"
            ),
        )


async def _resolve_provider_or_error(*, user_id: str, index_ref: str = "") -> Any:
    provider, _settings, health = await registry.resolve_for_user(user_id, folder_id=index_ref)
    if provider is None or not health.ok:
        raise HTTPException(
            status_code=503, detail=health.detail or "知识库 provider 不可用"
        )
    return provider


# ── Knowledge base CRUD ──


async def list_knowledge_bases(user_id: str, tenant_id: int = 0) -> dict[str, Any]:
    """列出可见知识库：自己的 + 公开的 + **同租户共享的**。

    最后一条必须同时匹配 ``tenant_id``：只写 ``visibility='tenant'`` 等于把"租户内共享"
    实现成"全站共享"。
    """
    try:
        pool = get_pool()
    except RuntimeError:
        return {"knowledge_bases": [], "count": 0}

    rows = await pool.fetch(
        """SELECT id, name, COALESCE(description, '') AS description, provider, visibility, status,
                  document_count, total_size_bytes, credits_consumed, config, error_message,
                  created_at, updated_at
             FROM knowledge_bases
            WHERE user_id = $1
               OR visibility = 'public'
               OR (visibility = 'tenant' AND tenant_id = $2)
            ORDER BY created_at DESC""",
        user_id,
        tenant_id or DEFAULT_TENANT_ID,
    )
    kbs = [_serialize_kb(row) for row in rows]
    return {"knowledge_bases": kbs, "count": len(kbs)}


async def create_knowledge_base(
    *,
    user_id: str,
    tenant_id: str,
    name: str,
    description: str = "",
    provider: str = "",
    visibility: str = "private",
) -> dict[str, Any]:
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="name must not be empty")
    if visibility not in _ALLOWED_VISIBILITY:
        raise HTTPException(
            status_code=400, detail="visibility must be 'public', 'private', or 'tenant'"
        )

    current_provider = await _load_user_provider(user_id)
    if provider and not is_known_provider(provider):
        raise HTTPException(
            status_code=400, detail=f"provider must be one of {KNOWN_PROVIDERS}"
        )
    if provider and provider != current_provider:
        raise HTTPException(
            status_code=409,
            detail=f"知识库 provider 必须与当前设置一致（当前为 {current_provider}）",
        )
    effective_provider = current_provider

    pool = get_pool()
    kb_id = str(uuid.uuid4())
    # index_ref 在首次索引时由 provider 决定（自托管=索引目录，Cloud=folder），此处留空
    await pool.execute(
        """INSERT INTO knowledge_bases
               (id, tenant_id, user_id, name, description, provider, visibility, status,
                config, created_at, updated_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, '{}'::jsonb, $9, $9)""",
        kb_id,
        tenant_id or DEFAULT_TENANT_ID,
        user_id,
        name.strip(),
        description,
        effective_provider,
        visibility,
        STATUS_DISABLED,
        _now_naive(),
    )
    logger.info("knowledge base created id=%s provider=%s", kb_id, effective_provider)
    return await get_knowledge_base(kb_id, user_id, tenant_id)


async def get_knowledge_base(kb_id: str, user_id: str, tenant_id: int = 0) -> dict[str, Any]:
    """取单个知识库（可见性规则与列表**同一份**，否则会出现"列表看不到但猜到 id 能读"）。"""
    pool = get_pool()
    row = await pool.fetchrow(
        """SELECT id, name, COALESCE(description, '') AS description, provider, visibility, status,
                  document_count, total_size_bytes, credits_consumed, config, error_message,
                  created_at, updated_at
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
    return _serialize_kb(row)


async def update_knowledge_base(
    kb_id: str,
    user_id: str,
    name: str | None = None,
    description: str | None = None,
    visibility: str | None = None,
) -> dict[str, Any]:
    """更新知识库（仅 owner）。provider 不可改 —— 换 provider 等于换索引，必须重建。"""
    pool = get_pool()
    existing = await pool.fetchrow(
        "SELECT id, user_id FROM knowledge_bases WHERE id = $1", kb_id
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if existing["user_id"] != user_id:
        raise HTTPException(
            status_code=403, detail="not authorized to update this knowledge base"
        )

    updates: list[str] = []
    params: list[Any] = []
    if name is not None:
        if not name.strip():
            raise HTTPException(status_code=400, detail="name must not be empty")
        params.append(name.strip())
        updates.append(f"name = ${len(params)}")
    if description is not None:
        params.append(description)
        updates.append(f"description = ${len(params)}")
    if visibility is not None:
        if visibility not in _ALLOWED_VISIBILITY:
            raise HTTPException(
                status_code=400, detail="visibility must be 'public', 'private', or 'tenant'"
            )
        params.append(visibility)
        updates.append(f"visibility = ${len(params)}")

    if not updates:
        raise HTTPException(status_code=400, detail="no fields to update")

    params.append(kb_id)
    await pool.execute(
        f"UPDATE knowledge_bases SET {', '.join(updates)}, updated_at = NOW() WHERE id = ${len(params)}",
        *params,
    )
    return {"id": kb_id, "updated": True}


async def delete_knowledge_base(
    kb_id: str, user_id: str, is_admin: bool = False
) -> dict[str, Any]:
    """删除知识库：先清 provider 侧文档与原文对象，再删库（文档随外键级联）。

    **provider 侧清理失败不阻断删除**：用户要求删除数据时必须删得掉；失败记日志，
    由离线巡检收敛（否则一个失效的 Cloud key 会让用户永远删不掉自己的库）。
    """
    pool = get_pool()
    kb = await pool.fetchrow(
        "SELECT id, user_id, tenant_id, provider, index_ref FROM knowledge_bases WHERE id = $1",
        kb_id,
    )
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if not is_admin and kb["user_id"] != user_id:
        raise HTTPException(
            status_code=403, detail="not authorized to delete this knowledge base"
        )

    docs = await pool.fetch(
        """SELECT id, storage_key, pageindex_doc_id FROM knowledge_documents
            WHERE knowledge_base_id = $1""",
        kb_id,
    )

    if kb["provider"] == "cloud" or kb["provider"] == "selfhosted":
        try:
            provider, _st, health = await registry.resolve_for_user(
                str(kb["user_id"]), folder_id=str(kb["index_ref"] or "")
            )
        except Exception as exc:  # noqa: BLE001
            provider, health = None, None
            logger.warning("knowledge delete: provider resolve failed kb=%s: %s", kb_id, exc)
        if provider is not None and health is not None and health.ok:
            for doc in docs:
                pageindex_doc_id = str(doc["pageindex_doc_id"] or "")
                if not pageindex_doc_id:
                    continue
                try:
                    await provider.delete_document(
                        _context_for(kb), pageindex_doc_id=pageindex_doc_id
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "knowledge delete: provider delete failed kb=%s doc=%s: %s",
                        kb_id,
                        doc["id"],
                        exc,
                    )

    store = blob.get_blob_store()
    for doc in docs:
        storage_key = str(doc["storage_key"] or "")
        if not storage_key:
            continue
        try:
            store.delete(storage_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("knowledge delete: blob delete failed key=%s: %s", storage_key, exc)

    await pool.execute("DELETE FROM knowledge_bases WHERE id = $1", kb_id)
    logger.info("knowledge base deleted id=%s", kb_id)
    return {"id": kb_id, "deleted": True}


def _context_for(kb: Any) -> Any:
    from app.knowledge.providers.base import ProviderContext

    return ProviderContext(
        kb_id=str(kb["id"]),
        tenant_id=str(kb["tenant_id"] or ""),
        user_id=str(kb["user_id"] or ""),
        index_ref=str(kb["index_ref"] or ""),
    )


# ── Documents ──


async def upload_document(
    *,
    kb_id: str,
    user_id: str,
    name: str,
    file_type: str = "",
    file_size_bytes: int = 0,
    content: bytes | None = None,
) -> dict[str, Any]:
    """上传原文：写对象存储 + 落 ``pending`` 行，等待 ``kb_index`` 索引。"""
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="document name must not be empty")

    pool = get_pool()
    kb = await pool.fetchrow(
        "SELECT id, tenant_id, user_id, provider, status FROM knowledge_bases WHERE id = $1",
        kb_id,
    )
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if kb["user_id"] != user_id:
        raise HTTPException(
            status_code=403, detail="not authorized to upload to this knowledge base"
        )
    await _assert_provider_matches(user_id=str(user_id), kb_provider=str(kb["provider"]))

    payload = content or b""
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过上限（{MAX_UPLOAD_BYTES // (1024 * 1024)} MB）",
        )

    # 类型在**上传时**就校验：等索引阶段才拒绝会让人以为"传成功了"
    provider = await _resolve_provider_or_error(user_id=str(user_id))
    if not provider.supports(name):
        raise HTTPException(
            status_code=400,
            detail=f"当前 provider 不支持该文件类型（{name.rsplit('.', 1)[-1] if '.' in name else '未知'}）",
        )

    doc_id = str(uuid.uuid4())
    storage_key = blob.build_key(str(kb["tenant_id"] or ""), kb_id, doc_id, name.strip())
    store = blob.get_blob_store()
    import asyncio

    try:
        await asyncio.to_thread(store.put, storage_key, payload, _content_type(name, file_type))
    except Exception as exc:  # noqa: BLE001 — 对象存储故障是 503（可重试），不是 500
        logger.error("knowledge upload: blob put failed kb=%s: %s", kb_id, exc)
        raise HTTPException(status_code=503, detail=f"原文存储不可用: {exc}") from exc

    now = _now_naive()
    await pool.execute(
        """INSERT INTO knowledge_documents
               (id, knowledge_base_id, tenant_id, user_id, name, file_type, file_size_bytes,
                storage_key, status, created_at, updated_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending', $9, $9)""",
        doc_id,
        kb_id,
        str(kb["tenant_id"] or DEFAULT_TENANT_ID),
        user_id,
        name.strip(),
        (file_type or name.rsplit(".", 1)[-1]).lower(),
        file_size_bytes or len(payload),
        storage_key,
        now,
    )
    await _refresh_kb_stats(pool, kb_id)
    return await _get_document_row(pool, kb_id, doc_id)


def _content_type(name: str, file_type: str) -> str:
    suffix = (file_type or name.rsplit(".", 1)[-1] or "").lower().lstrip(".")
    return {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "doc": "application/msword",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "txt": "text/plain",
        "md": "text/markdown",
    }.get(suffix, "application/octet-stream")


async def list_documents(kb_id: str, user_id: str, tenant_id: int = 0) -> dict[str, Any]:
    pool = get_pool()
    kb = await pool.fetchrow(
        "SELECT id, user_id, visibility, tenant_id FROM knowledge_bases WHERE id = $1", kb_id
    )
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    _require_kb_access(kb, user_id, tenant_id, action="view")

    rows = await pool.fetch(
        """SELECT id, knowledge_base_id, name, file_type, file_size_bytes, page_count, status,
                  pageindex_doc_id, error_message, metadata, created_at, updated_at
             FROM knowledge_documents
            WHERE knowledge_base_id = $1
            ORDER BY created_at DESC""",
        kb_id,
    )
    docs = [_serialize_doc(row) for row in rows]
    return {"documents": docs, "count": len(docs)}


async def _get_document_row(pool: Any, kb_id: str, doc_id: str) -> dict[str, Any]:
    row = await pool.fetchrow(
        """SELECT id, knowledge_base_id, name, file_type, file_size_bytes, page_count, status,
                  pageindex_doc_id, error_message, metadata, created_at, updated_at
             FROM knowledge_documents WHERE id = $1 AND knowledge_base_id = $2""",
        doc_id,
        kb_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="document not found")
    return _serialize_doc(row)


async def _refresh_kb_stats(pool: Any, kb_id: str) -> None:
    """重算 KB 聚合统计（上传/删除后调用）。"""
    await pool.execute(
        """UPDATE knowledge_bases
              SET document_count = (SELECT COUNT(*) FROM knowledge_documents WHERE knowledge_base_id = $1),
                  total_size_bytes = COALESCE(
                      (SELECT SUM(file_size_bytes) FROM knowledge_documents WHERE knowledge_base_id = $1), 0),
                  updated_at = NOW()
            WHERE id = $1""",
        kb_id,
    )


async def delete_document(kb_id: str, user_id: str, doc_id: str) -> dict[str, Any]:
    """删除文档：provider 侧删除 + 原文对象删除 + 行删除 + 统计重算。"""
    pool = get_pool()
    kb = await pool.fetchrow(
        "SELECT id, user_id, tenant_id, provider, index_ref FROM knowledge_bases WHERE id = $1",
        kb_id,
    )
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if kb["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="not authorized to delete documents")

    doc = await pool.fetchrow(
        """SELECT id, storage_key, pageindex_doc_id FROM knowledge_documents
            WHERE id = $1 AND knowledge_base_id = $2""",
        doc_id,
        kb_id,
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")

    pageindex_doc_id = str(doc["pageindex_doc_id"] or "")
    if pageindex_doc_id:
        try:
            provider, _st, health = await registry.resolve_for_user(
                str(kb["user_id"]), folder_id=str(kb["index_ref"] or "")
            )
            if provider is not None and health.ok:
                await provider.delete_document(
                    _context_for(kb), pageindex_doc_id=pageindex_doc_id
                )
        except Exception as exc:  # noqa: BLE001 — 与整库删除同一条取舍
            logger.warning("knowledge delete doc: provider delete failed doc=%s: %s", doc_id, exc)

    storage_key = str(doc["storage_key"] or "")
    if storage_key:
        try:
            blob.get_blob_store().delete(storage_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("knowledge delete doc: blob delete failed key=%s: %s", storage_key, exc)

    await pool.execute(
        "DELETE FROM knowledge_documents WHERE id = $1 AND knowledge_base_id = $2", doc_id, kb_id
    )
    await _refresh_kb_stats(pool, kb_id)
    return {"id": doc_id, "knowledge_base_id": kb_id, "deleted": True}


# ── Build & Query ──


async def _enqueue_kb_index(
    *, kb_id: str, user_id: str, tenant_id: str, force: bool = False
) -> str:
    """投递 ``kb_index`` 任务。

    租户只从 KB 行反查（不接受调用方传值），与 worker 侧的约定一致。
    """
    from app.queue.producer import QueueProducer
    from app.redis_client import get_redis

    redis = await get_redis()
    try:
        producer = QueueProducer(redis)
        return await producer.enqueue(
            "kb_index",
            tenant_id=tenant_id or DEFAULT_TENANT_ID,
            payload={"kb_id": kb_id, "user_id": user_id, "force": bool(force)},
        )
    finally:
        await redis.aclose()


async def build_knowledge_base(
    kb_id: str, user_id: str, tenant_id: int = 0, force: bool = False
) -> dict[str, Any]:
    """投递索引任务（把 pending 文档交给 ``kb_index``）。"""
    pool = get_pool()
    kb = await pool.fetchrow(
        "SELECT id, user_id, provider, status, document_count FROM knowledge_bases WHERE id = $1",
        kb_id,
    )
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if kb["user_id"] != user_id:
        raise HTTPException(
            status_code=403, detail="not authorized to build this knowledge base"
        )
    if (kb["document_count"] or 0) == 0:
        raise HTTPException(
            status_code=400, detail="cannot build knowledge base with no documents"
        )
    await _assert_provider_matches(user_id=str(user_id), kb_provider=str(kb["provider"]))
    # 先确认 provider 真的可用，再投递：否则用户会等一个注定失败的长任务
    await _resolve_provider_or_error(user_id=str(user_id))

    task_id = await _enqueue_kb_index(
        kb_id=kb_id, user_id=user_id, tenant_id=tenant_id, force=force
    )
    return {
        "status": "indexing",
        "task_id": task_id,
        "provider": kb["provider"],
        "force": bool(force),
    }


async def query_knowledge_base(
    *, kb_id: str, user_id: str, query: str, tenant_id: int = 0, top_k: int = 5
) -> dict[str, Any]:
    """在知识库内提问，返回答案与引用。"""
    if not query or not query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty")

    pool = get_pool()
    kb = await pool.fetchrow(
        "SELECT id, user_id, visibility, tenant_id, provider FROM knowledge_bases WHERE id = $1",
        kb_id,
    )
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    _require_kb_access(kb, user_id, tenant_id, action="query")
    # 读操作也要校验 provider 一致：否则在错误的索引上"检索成功但零命中"
    await _assert_provider_matches(user_id=str(user_id), kb_provider=str(kb["provider"]))

    try:
        result, provider_name = await answer_question(
            kb_id=kb_id, user_id=user_id, question=query.strip()
        )
    except ProviderUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except KnowledgeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "provider": provider_name,
        "answer": result.answer,
        "citations": [
            {
                "doc_name": c.doc_name,
                "page": c.page,
                "block_id": c.block_id,
                "snippet": c.snippet,
            }
            for c in result.citations
        ],
    }


async def preview_document(
    kb_id: str, doc_id: str, user_id: str, tenant_id: int = 0
) -> dict[str, Any]:
    """文档预览：文本类返回头部内容；二进制返回元信息并提示下载/引用。"""
    pool = get_pool()
    kb = await pool.fetchrow(
        "SELECT id, user_id, visibility, tenant_id FROM knowledge_bases WHERE id = $1", kb_id
    )
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    _require_kb_access(kb, user_id, tenant_id, action="preview")

    row = await pool.fetchrow(
        """SELECT id, knowledge_base_id, name, file_type, file_size_bytes, page_count, status,
                  pageindex_doc_id, error_message, metadata, storage_key, created_at, updated_at
             FROM knowledge_documents WHERE id = $1 AND knowledge_base_id = $2""",
        doc_id,
        kb_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="document not found")

    doc = _serialize_doc(row)
    doc["content"] = ""
    suffix = f".{(row['file_type'] or '').lower().lstrip('.')}"
    if suffix in _TEXT_PREVIEW_SUFFIXES and row["storage_key"]:
        try:
            import asyncio

            raw = await asyncio.to_thread(blob.get_blob_store().get, str(row["storage_key"]))
            doc["content"] = raw[:10000].decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 — 预览失败不该 500
            logger.warning("knowledge preview: read failed doc=%s: %s", doc_id, exc)
    return {"data": doc}


async def admin_list_knowledge_bases() -> dict[str, Any]:
    """管理员视角：全部公开知识库。"""
    try:
        pool = get_pool()
    except RuntimeError:
        return {"knowledge_bases": [], "count": 0}

    rows = await pool.fetch(
        """SELECT id, name, COALESCE(description, '') AS description, provider, visibility, status,
                  document_count, total_size_bytes, credits_consumed, config, error_message,
                  created_at, updated_at
             FROM knowledge_bases
            WHERE visibility = 'public'
            ORDER BY created_at DESC""",
    )
    kbs = [_serialize_kb(row) for row in rows]
    return {"knowledge_bases": kbs, "count": len(kbs)}


# ── User settings ──


async def get_user_settings(user_id: str) -> dict[str, Any]:
    """用户知识库设置（不回传 key 明文，只给"是否已配置"+指纹）。"""
    stored = await settings_store.load(user_id)
    system_health = registry.selfhosted_health()
    return {
        "provider": stored.provider if stored else "",
        "status": stored.status if stored else STATUS_DISABLED,
        "error_message": stored.error_message if stored else "",
        "cloud_key_configured": bool(stored and stored.cloud_api_key),
        "providers": [
            {
                "name": "selfhosted",
                "label": "自托管（PageIndex local）",
                "available": system_health.ok,
                "status": system_health.status,
                "detail": system_health.detail,
            },
            {
                "name": "cloud",
                "label": "PageIndex Cloud",
                "available": bool(stored and stored.cloud_api_key),
                "status": stored.status if stored and stored.provider == "cloud" else STATUS_DISABLED,
                "detail": "",
            },
        ],
    }


async def put_user_settings(
    *, user_id: str, tenant_id: str, provider: str, cloud_api_key: str | None
) -> dict[str, Any]:
    """保存 provider 选择（Cloud 时同时落密文）。"""
    if not is_known_provider(provider):
        raise HTTPException(
            status_code=400, detail=f"provider must be one of {KNOWN_PROVIDERS}"
        )
    # 已有知识库不允许随意切换 provider：库的索引在旧 provider 侧，切了就是"查不到"
    pool = get_pool()
    mismatched = await pool.fetchval(
        "SELECT COUNT(*) FROM knowledge_bases WHERE user_id = $1 AND provider <> $2",
        user_id,
        provider,
    )
    if mismatched:
        raise HTTPException(
            status_code=409,
            detail=f"还有 {mismatched} 个知识库使用其它 provider，请先删除或重建后再切换",
        )
    try:
        saved = await settings_store.save(
            user_id=user_id, tenant_id=tenant_id, provider=provider, cloud_api_key=cloud_api_key
        )
    except ProviderUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except KnowledgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "provider": saved.provider,
        "status": saved.status,
        "cloud_key_configured": bool(saved.cloud_api_key),
    }


async def verify_user_settings(user_id: str) -> dict[str, Any]:
    """显式连通性探测（"测试连接"）：不放在启动路径上。"""
    provider, stored, health = await registry.resolve_for_user(user_id)
    if provider is None or stored is None:
        return {"ok": False, "status": health.status, "detail": health.detail}

    result = health
    # 探测方法名兼容：RemoteProvider 用 `probe`（async HTTP），旧实现用 `verify`（同步）。
    # 之前只找 `verify`，于是"测试连接"永远退回本地配置检查 —— 服务没起也会显示正常。
    import inspect

    probe = getattr(provider, "probe", None) or getattr(provider, "verify", None)
    if health.ok and callable(probe):
        try:
            if inspect.iscoroutinefunction(probe):
                result = await probe()
            else:
                import asyncio

                result = await asyncio.to_thread(probe)
        except Exception as exc:  # noqa: BLE001
            from app.knowledge.providers.base import HealthResult

            result = HealthResult(False, STATUS_ERROR, str(exc))
    await settings_store.update_status(
        user_id=user_id, status=result.status, error_message="" if result.ok else result.detail
    )
    return {"ok": result.ok, "status": result.status, "detail": result.detail}


# ── Routes ──


@router.get("")
async def list_kb(
    request: Request,
    user_id: str = Query("", alias="user_id"),
    tenant_id: int = Query(0, alias="tenant_id"),
) -> dict[str, Any]:
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    return await list_knowledge_bases(user_id, tenant_id=tenant_id)


@router.post("")
async def create_kb(
    request: Request,
    body: KnowledgeBaseCreate,
    user_id: str = Query("", alias="user_id"),
    tenant_id: int = Query(0, alias="tenant_id"),
) -> dict[str, Any]:
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    return await create_knowledge_base(
        user_id=user_id,
        tenant_id=tenant_id,
        name=body.name,
        description=body.description,
        provider=body.provider,
        visibility=body.visibility,
    )


# 注意顺序：``/settings`` 与 ``/admin/list`` 必须注册在 ``/{kb_id}`` 之前，
# 否则会被当成 kb_id 吃掉。
@router.get("/settings")
async def get_settings_route(
    request: Request, user_id: str = Query("", alias="user_id")
) -> dict[str, Any]:
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    return await get_user_settings(user_id)


@router.put("/settings")
async def put_settings_route(
    request: Request,
    body: UserSettingsUpdate,
    user_id: str = Query("", alias="user_id"),
    tenant_id: int = Query(0, alias="tenant_id"),
) -> dict[str, Any]:
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    return await put_user_settings(
        user_id=user_id,
        tenant_id=tenant_id,
        provider=body.provider,
        cloud_api_key=body.cloud_api_key,
    )


@router.post("/settings/verify")
async def verify_settings_route(
    request: Request, user_id: str = Query("", alias="user_id")
) -> dict[str, Any]:
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    return await verify_user_settings(user_id)


@router.get("/admin/list")
async def admin_list_kb(request: Request) -> dict[str, Any]:
    role = request.headers.get("X-User-Role", "")
    if role not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="admin role required")
    return await admin_list_knowledge_bases()


@router.get("/{kb_id}")
async def get_kb(
    kb_id: str,
    request: Request,
    user_id: str = Query("", alias="user_id"),
    tenant_id: int = Query(0, alias="tenant_id"),
) -> dict[str, Any]:
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    return await get_knowledge_base(kb_id, user_id, tenant_id=tenant_id)


@router.put("/{kb_id}")
async def update_kb(
    kb_id: str,
    request: Request,
    body: KnowledgeBaseUpdate,
    user_id: str = Query("", alias="user_id"),
) -> dict[str, Any]:
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    return await update_knowledge_base(
        kb_id=kb_id,
        user_id=user_id,
        name=body.name,
        description=body.description,
        visibility=body.visibility,
    )


@router.delete("/{kb_id}")
async def delete_kb(
    kb_id: str,
    request: Request,
    user_id: str = Query("", alias="user_id"),
) -> dict[str, Any]:
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    is_admin = request.headers.get("X-User-Role", "") in ("admin", "owner")
    return await delete_knowledge_base(kb_id, user_id, is_admin=is_admin)


@router.post("/{kb_id}/documents")
async def upload_doc(
    kb_id: str,
    request: Request,
    body: DocumentUpload,
    user_id: str = Query("", alias="user_id"),
) -> dict[str, Any]:
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    payload: bytes | None = None
    if body.content:
        try:
            # validate=True：非法 base64 直接抛错，避免纯文本被静默解成垃圾字节
            payload = base64.b64decode(body.content, validate=True)
        except Exception:
            payload = body.content.encode("utf-8")
    return await upload_document(
        kb_id=kb_id,
        user_id=user_id,
        name=body.name,
        file_type=body.file_type,
        file_size_bytes=body.file_size_bytes,
        content=payload,
    )


@router.get("/{kb_id}/documents")
async def list_docs(
    kb_id: str,
    request: Request,
    user_id: str = Query("", alias="user_id"),
    tenant_id: int = Query(0, alias="tenant_id"),
) -> dict[str, Any]:
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    return await list_documents(kb_id=kb_id, user_id=user_id, tenant_id=tenant_id)


@router.delete("/{kb_id}/documents")
async def delete_doc(
    kb_id: str,
    request: Request,
    user_id: str = Query("", alias="user_id"),
    doc_id: str = Query("", alias="doc_id"),
) -> dict[str, Any]:
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    if not doc_id:
        raise HTTPException(status_code=400, detail="doc_id is required")
    return await delete_document(kb_id, user_id, doc_id)


@router.post("/{kb_id}/build")
async def build_kb(
    kb_id: str,
    request: Request,
    body: BuildBody | None = None,
    user_id: str = Query("", alias="user_id"),
    tenant_id: int = Query(0, alias="tenant_id"),
) -> dict[str, Any]:
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    return await build_knowledge_base(
        kb_id=kb_id,
        user_id=user_id,
        tenant_id=tenant_id,
        force=bool(body and body.force),
    )


@router.post("/{kb_id}/query")
async def query_kb(
    kb_id: str,
    request: Request,
    body: KBQuery,
    user_id: str = Query("", alias="user_id"),
    tenant_id: int = Query(0, alias="tenant_id"),
) -> dict[str, Any]:
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    return await query_knowledge_base(
        kb_id=kb_id,
        user_id=user_id,
        query=body.query,
        top_k=body.top_k,
        tenant_id=tenant_id,
    )


@router.get("/{kb_id}/documents/{doc_id}/preview")
async def preview_doc(
    kb_id: str,
    doc_id: str,
    request: Request,
    user_id: str = Query("", alias="user_id"),
    tenant_id: int = Query(0, alias="tenant_id"),
) -> dict[str, Any]:
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id required")
    return await preview_document(kb_id, doc_id, user_id, tenant_id=tenant_id)
