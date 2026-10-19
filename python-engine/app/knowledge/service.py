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

import json
import logging
from dataclasses import asdict
from typing import Any

from app.knowledge.errors import KnowledgeError, ProviderUnavailableError
from app.knowledge.providers import registry
from app.knowledge.providers.base import (
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
    """**索引已移出引擎**（S4）：由 ``pageindex-worker`` 消费 ``engine:tasks:kb`` 执行。

    引擎不装 PageIndex SDK（``pageindex → litellm → openai<3`` 与引擎的 ``openai 3.x``
    无法共存）。这里保留一个**显式的失败点**而不是静默降级：有人调到它，说明还在走旧路径，
    应当改为投递队列任务（见 ``app/api/knowledge.py::build_knowledge_base``）。
    """
    raise KnowledgeError(
        "索引由 pageindex-worker 执行（引擎不装 PageIndex SDK）；请通过 "
        "POST /v1/kb/{id}/build 投递任务"
    )


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
        raise ProviderUnavailableError(health.detail or "provider 不可用")

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
