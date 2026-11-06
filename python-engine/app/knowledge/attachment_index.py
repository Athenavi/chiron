"""附件结构化索引：把可索引的附件挂进 PageIndex，跨会话复用同一份索引。

**为什么复用知识库两张表**：``pageindex-worker`` 的 ``handle_kb_index`` 只认
``knowledge_bases`` / ``knowledge_documents``。再建一套"附件索引表"等于给索引执行者开
第二个入口，索引状态会分叉（一边 ready 一边 pending），排障时没有单一事实源。

**隔离边界 = 每个 tenant/user 一个库**（用户已定）：附件本身没有 ``kb_id``，但同一份
文件在多个会话里反复出现时重建索引是纯浪费（PageIndex 的 optimize=full 会走 LLM）。
库名固定为 ``__attachments__`` 且 ``visibility='private'``，列表接口按名字过滤掉，
不会混进用户的普通知识库。

**跨会话去重靠确定性 id**：``doc_id = uuid5(..., asset_id)``，同一 ``asset_id`` 在
``knowledge_documents`` 里天然只有一行（``ON CONFLICT DO NOTHING``）。

**不阻塞对话**：落 pending 行 + 投 ``kb_index`` 队列即返回；索引真跑完是后台的事，
检索式读取在文档未 ready 时自动回落到本地分片（见 ``app/tools/attachment_read.py``）。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.tools.context import to_tenant_int

logger = logging.getLogger(__name__)

#: 个人附件库的固定名。用保留前缀包住，避免与用户自建的库重名。
ATTACHMENT_KB_NAME = "__attachments__"
_NAMESPACE = uuid.UUID("6f1a3c92-0d4e-4b57-9c1e-2a7d8b5f4e10")


def attachment_kb_id(tenant_id: int, user_id: str) -> str:
    """个人附件库 id：确定性（同一租户+用户永远是同一个库）。"""
    return str(uuid.uuid5(_NAMESPACE, f"chiron:attachments:{tenant_id}:{user_id}"))


def attachment_doc_id(tenant_id: int, asset_id: str) -> str:
    """附件文档 id：同一 ``asset_id`` 永远映射同一行 —— 这就是去重。"""
    return str(uuid.uuid5(_NAMESPACE, f"chiron:attachment:{tenant_id}:{asset_id}"))


def _as_dict(value: Any) -> dict[str, Any]:
    """jsonb 列的取回形态随驱动配置而异（dict 或 str），与 service._as_dict 同口径。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


async def ensure_attachment_kb_for_user(*, tenant_id: int, user_id: str) -> str | None:
    """确保该用户的个人附件库存在；用户未配置 provider 时返回 None（不索引）。

    返回 None 是**正常路径**：附件索引是增强，未配置知识库的用户照常能发文件，
    只是走本地分片读取。
    """
    from app.db import get_pool
    from app.knowledge.settings_store import load

    if not user_id:
        return None
    user_settings = await load(user_id)
    if user_settings is None or not user_settings.provider:
        return None

    pool = get_pool()
    kb_id = attachment_kb_id(tenant_id, user_id)
    await pool.execute(
        """INSERT INTO knowledge_bases
               (id, tenant_id, user_id, name, description, visibility, provider, status,
                config, created_at, updated_at)
           VALUES ($1, $2, $3, $4, $5, 'private', $6, 'disabled', '{}'::jsonb, now(), now())
           ON CONFLICT (id) DO NOTHING""",
        kb_id,
        tenant_id,
        user_id,
        ATTACHMENT_KB_NAME,
        "自动维护：会话附件的结构化索引（跨会话复用）",
        user_settings.provider,
    )
    return kb_id


async def index_attachment(
    *,
    tenant_id: int,
    user_id: str,
    asset_id: str,
    name: str,
    data: bytes,
    file_type: str = "",
) -> str | None:
    """把附件原文字节登记为待索引文档并投递 ``kb_index``；返回 doc_id。

    不可索引（provider 不支持该类型 / 用户未配置知识库）时返回 None —— 调用方据此
    保持本地分片路径，不需要区分"失败"与"不支持"。
    """
    from app.knowledge import blob
    from app.knowledge.providers import registry

    if not asset_id or not data:
        return None
    provider, _settings, health = await registry.resolve_for_user(user_id)
    if provider is None or not health.ok or not provider.supports(name):
        return None

    from app.db import get_pool

    pool = get_pool()
    doc_id = attachment_doc_id(tenant_id, asset_id)
    # 早退：跨会话复用意味着同一附件会被反复"登记"，已有行就不要再写一遍对象存储、
    # 也不要重复投递（worker 虽幂等，但每次都唤醒一轮队列是纯噪声）。
    existing = await pool.fetchval("SELECT 1 FROM knowledge_documents WHERE id = $1", doc_id)
    if existing:
        return doc_id

    kb_id = await ensure_attachment_kb_for_user(tenant_id=tenant_id, user_id=user_id)
    if not kb_id:
        return None

    storage_key = blob.build_key(tenant_id, kb_id, doc_id, name or f"{asset_id}.pdf")
    store = blob.get_blob_store()
    import asyncio

    try:
        await asyncio.to_thread(store.put, storage_key, data, "")
    except Exception as exc:  # noqa: BLE001 — 存储故障不该让"看附件"失败
        logger.warning("attachment index: blob put failed asset=%s: %s", asset_id, exc)
        return None

    await pool.execute(
        """INSERT INTO knowledge_documents
               (id, knowledge_base_id, tenant_id, user_id, name, file_type, file_size_bytes,
                storage_key, status, created_at, updated_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending', now(), now())
           ON CONFLICT (id) DO NOTHING""",
        doc_id,
        kb_id,
        tenant_id,
        user_id,
        name,
        (file_type or name.rsplit(".", 1)[-1]).lower(),
        len(data),
        storage_key,
    )
    await _enqueue(kb_id=kb_id, user_id=user_id, tenant_id=tenant_id)
    return doc_id


async def _enqueue(*, kb_id: str, user_id: str, tenant_id: int) -> None:
    """投递索引任务。失败只告警：索引是增强，不能因为它把本轮对话带崩。"""
    from app.queue.producer import QueueProducer
    from app.redis_client import get_redis

    try:
        redis = await get_redis()
        try:
            await QueueProducer(redis).enqueue(
                "kb_index",
                tenant_id=to_tenant_int(tenant_id),
                payload={"kb_id": kb_id, "user_id": user_id, "force": False},
            )
        finally:
            await redis.aclose()
    except Exception as exc:  # noqa: BLE001
        logger.warning("attachment index: enqueue failed kb=%s: %s", kb_id, exc)


async def query_attachment(
    *, tenant_id: int, user_id: str, asset_id: str, question: str
) -> dict[str, Any] | None:
    """在**已就绪**的该附件索引上做检索式读取；不可用时返回 None。

    只收显式 doc_id：PageIndex 没有"整库隐式范围"，不传文档等于让模型在空集上推理，
    也会把同一用户其它附件的索引一并暴露给这次提问。
    """
    from app.db import get_pool
    from app.knowledge.providers import registry
    from app.knowledge.providers.base import ProviderContext

    if not question.strip():
        return None

    # 附件被删掉之后，索引里的内容仍然可被问答命中。读取前先确认资产还在
    # （Range 取 1 字节），否则"删除"只是从列表里消失，内容却还能被问出来。
    from app.media.attachments import _asset_still_exists

    if not await _asset_still_exists(asset_id, tenant_id, user_id):
        return None

    kb_id = attachment_kb_id(tenant_id, user_id)
    pool = get_pool()
    row = await pool.fetchrow(
        """SELECT k.id, k.tenant_id, k.user_id, k.index_ref, k.config, d.pageindex_doc_id
             FROM knowledge_bases k
             JOIN knowledge_documents d ON d.knowledge_base_id = k.id
            WHERE k.id = $1 AND d.id = $2 AND d.status = 'ready'
              AND d.pageindex_doc_id IS NOT NULL""",
        kb_id,
        attachment_doc_id(tenant_id, asset_id),
    )
    if row is None:
        return None

    provider, _settings, health = await registry.resolve_for_user(
        user_id, folder_id=str(row["index_ref"] or "")
    )
    if provider is None or not health.ok:
        return None

    ctx = ProviderContext(
        kb_id=str(row["id"]),
        tenant_id=to_tenant_int(row["tenant_id"]),
        user_id=str(row["user_id"] or ""),
        index_ref=str(row["index_ref"] or ""),
        config=_as_dict(row["config"]),
        doc_ids=(str(row["pageindex_doc_id"]),),
    )
    result = await provider.answer(ctx, question=question.strip())
    return {
        "answer": result.answer,
        "citations": [
            {"doc_name": c.doc_name, "page": c.page, "block_id": c.block_id, "snippet": c.snippet}
            for c in result.citations
        ],
    }
