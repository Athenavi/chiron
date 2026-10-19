"""知识库业务编排：把数据库行、原文对象存储与 provider 串起来。

这一层只做"编排"，不含任何 PageIndex 细节（那是 provider 的事），也不做 HTTP
（那是 ``app/api/knowledge.py`` 的事）。队列任务（``kb_index``）与 API 共用这里的函数，
所以两条路径对"文档状态怎么流转"必须只有一份实现。

状态流转（文档级，见 0023）：

    pending ──(队列取件)──> indexing ──> ready
                                 └────> error（error_message 记录原因）

库级状态由文档聚合而来：任一文档 error ⇒ 库 error；全部 ready ⇒ 库 ready。
**不做乐观置 ready**：索引是真跑完才回写（旧实现把"投递任务"当成功，检索时才暴露空库）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from typing import Any

from app.knowledge import blob, settings_store
from app.knowledge.errors import KnowledgeError, ProviderUnavailable
from app.knowledge.providers import registry
from app.knowledge.providers.base import (
    STATUS_ERROR,
    STATUS_READY,
    AnswerResult,
    ProviderContext,
)

logger = logging.getLogger(__name__)

STATUS_INDEXING = "indexing"


async def _pool() -> Any:
    from app.db import get_pool

    return get_pool()


def _as_dict(value: Any) -> dict[str, Any]:
    """jsonb 列的取回形态随驱动配置而异（dict 或 str）。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _context(kb: Any, doc_ids: tuple[str, ...] = ()) -> ProviderContext:
    return ProviderContext(
        kb_id=str(kb["id"]),
        tenant_id=str(kb["tenant_id"] or ""),
        user_id=str(kb["user_id"] or ""),
        index_ref=str(kb["index_ref"] or ""),
        config=_as_dict(kb["config"]),
        doc_ids=doc_ids,
    )


async def _set_kb_status(pool: Any, kb_id: str, status: str, error_message: str = "") -> None:
    await pool.execute(
        """UPDATE knowledge_bases
              SET status = $2, error_message = $3, updated_at = NOW()
            WHERE id = $1""",
        kb_id,
        status,
        error_message or None,
    )


async def _load_kb(pool: Any, kb_id: str) -> Any:
    kb = await pool.fetchrow(
        """SELECT id, tenant_id, user_id, provider, index_ref, config, status
             FROM knowledge_bases WHERE id = $1""",
        kb_id,
    )
    if kb is None:
        raise KnowledgeError(f"knowledge base not found: {kb_id}")
    return kb


async def index_knowledge_base(*, kb_id: str, user_id: str) -> dict[str, Any]:
    """索引一个知识库下所有未就绪的文档（队列任务 ``kb_index`` 的任务体）。

    串行处理文档：local 建树是分钟级 CPU/网络密集型任务，并发跑只会把同一条 LLM
    配额打满并让 ``manifest.json`` 的写更密集。库级串行由队列的单消费者语义保证。
    """
    pool = await _pool()
    kb = await _load_kb(pool, kb_id)

    provider, user_settings, health = await registry.resolve_for_user(
        user_id, folder_id=str(kb["index_ref"] or "")
    )
    if provider is None or not health.ok:
        detail = health.detail or "provider 不可用"
        await _set_kb_status(pool, kb_id, STATUS_ERROR, detail)
        return {"status": STATUS_ERROR, "indexed": 0, "failed": 0, "detail": detail}

    await _set_kb_status(pool, kb_id, STATUS_INDEXING)

    # 让 provider 决定索引位置并回写（自托管=索引目录；Cloud=库 folder，其上层是以
    # user_id 命名的用户 folder）。放在"提交文档之前"：位置没准备好就不该开始索引。
    try:
        index_ref = await provider.prepare_index(_context(kb))
    except Exception as exc:  # noqa: BLE001 — 位置准备失败 = 整库失败，逐文档重试没有意义
        detail = f"索引位置准备失败: {exc}"
        await _set_kb_status(pool, kb_id, STATUS_ERROR, detail)
        return {"status": STATUS_ERROR, "indexed": 0, "failed": 0, "detail": detail}

    if index_ref and index_ref != str(kb["index_ref"] or ""):
        await pool.execute(
            "UPDATE knowledge_bases SET index_ref = $2, updated_at = NOW() WHERE id = $1",
            kb_id,
            index_ref,
        )
        # 后续文档级 ctx 要带上新位置（Cloud 的 folder_id 正是从这里进 provider 的）
        kb = dict(kb) | {"index_ref": index_ref}

    # Cloud 的用户级 folder 也落库：下次索引不必再列取一遍账号
    user_folder = str(getattr(provider, "user_folder_id", "") or "")
    if user_folder and user_folder != (user_settings.cloud_folder_id if user_settings else ""):
        await settings_store.set_cloud_folder(user_id=user_id, folder_id=user_folder)

    docs = await pool.fetch(
        """SELECT id, name, file_type, storage_key
             FROM knowledge_documents
            WHERE knowledge_base_id = $1 AND status <> 'ready'
            ORDER BY created_at""",
        kb_id,
    )

    store = blob.get_blob_store()
    indexed = 0
    errors: list[str] = []
    for doc in docs:
        doc_id = str(doc["id"])
        ctx = _context(kb)
        try:
            storage_key = str(doc["storage_key"] or "")
            if not storage_key:
                raise KnowledgeError("文档缺少原文对象（storage_key 为空）")
            data = await asyncio.to_thread(store.get, storage_key)
            await pool.execute(
                "UPDATE knowledge_documents SET status = $2, updated_at = NOW() WHERE id = $1",
                doc_id,
                STATUS_INDEXING,
            )
            result = await provider.submit_document(
                ctx,
                doc_id=doc_id,
                filename=str(doc["name"] or f"{doc_id}.pdf"),
                data=data,
                file_type=str(doc["file_type"] or "pdf"),
            )
            await pool.execute(
                """UPDATE knowledge_documents
                      SET status = $2, pageindex_doc_id = $3, page_count = $4,
                          error_message = NULL, updated_at = NOW()
                    WHERE id = $1""",
                doc_id,
                STATUS_READY,
                result.doc_id,
                result.page_count,
            )
            indexed += 1
        except Exception as exc:  # noqa: BLE001 — 单文档失败不该拖垮整库
            message = str(exc)[:500]
            errors.append(f"{doc['name']}: {message}")
            logger.warning("knowledge index failed kb=%s doc=%s: %s", kb_id, doc_id, message)
            await pool.execute(
                """UPDATE knowledge_documents
                      SET status = $2, error_message = $3, updated_at = NOW()
                    WHERE id = $1""",
                doc_id,
                STATUS_ERROR,
                message,
            )

    final = STATUS_ERROR if errors else STATUS_READY
    await _set_kb_status(pool, kb_id, final, "; ".join(errors))
    logger.info(
        "knowledge index done kb=%s indexed=%d failed=%d", kb_id, indexed, len(errors)
    )
    return {"status": final, "indexed": indexed, "failed": len(errors)}


async def answer_question(
    *, kb_id: str, user_id: str, question: str
) -> tuple[AnswerResult, str]:
    """在知识库内提问。返回 (答案, provider 名)。

    检索范围 = 该库中**已就绪**文档的 provider doc_id 集合：PageIndex 没有"整库"隐式范围，
    不传文档就等于让模型在空集上推理。
    """
    pool = await _pool()
    kb = await _load_kb(pool, kb_id)

    provider, _settings, health = await registry.resolve_for_user(
        user_id, folder_id=str(kb["index_ref"] or "")
    )
    if provider is None or not health.ok:
        raise ProviderUnavailable(health.detail or "provider 不可用")

    rows = await pool.fetch(
        """SELECT pageindex_doc_id FROM knowledge_documents
            WHERE knowledge_base_id = $1 AND status = 'ready'
              AND pageindex_doc_id IS NOT NULL""",
        kb_id,
    )
    doc_ids = tuple(str(row["pageindex_doc_id"]) for row in rows if row["pageindex_doc_id"])
    ctx = _context(kb, doc_ids)
    result = await provider.answer(ctx, question=question)
    return result, provider.name


def serialize_answer(result: AnswerResult) -> dict[str, Any]:
    """答案 → JSON 可序列化结构（API 与工具共用同一形状）。"""
    return {
        "answer": result.answer,
        "citations": [asdict(c) for c in result.citations],
    }
