"""L3 摘要持久层 — memory_summaries 表的 asyncpg CRUD + Milvus 向量双写。

- PG 存正文/元数据/运维字段；Milvus memory_store collection 存嵌入向量
- content_hash 精确去重（PG 唯一索引拦截）
- 租户行级隔离：所有查询携带 tenant_id/user_id
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from app.memory.layers import SummaryEntry

logger = logging.getLogger(__name__)

_COLUMNS = (
    "id, tenant_id, user_id, session_id, content, topics, entities, "
    "turn_start, turn_end, content_hash, access_count, last_accessed_at, "
    "status, created_at"
)


def new_summary_id() -> str:
    return f"sms_{uuid.uuid4().hex[:20]}"


def compute_hash(tenant_id: str, user_id: str, content: str) -> str:
    raw = f"{tenant_id}:{user_id}:{content}"
    return f"sha256:{hashlib.sha256(raw.encode()).hexdigest()[:40]}"


def _row_to_entry(row: Any) -> SummaryEntry:
    topics_raw = row["topics"]
    topics = (
        json.loads(topics_raw) if isinstance(topics_raw, str) else (topics_raw or [])
    )
    entities_raw = row["entities"]
    entities = (
        json.loads(entities_raw)
        if isinstance(entities_raw, str)
        else (entities_raw or {})
    )
    return SummaryEntry(
        id=row["id"],
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        session_id=row["session_id"],
        content=row["content"],
        topics=topics,
        entities=entities,
        turn_start=row["turn_start"],
        turn_end=row["turn_end"],
        content_hash=row["content_hash"],
        access_count=row["access_count"],
        last_accessed_at=row["last_accessed_at"],
        status=row["status"],
        created_at=row["created_at"],
    )


class SummaryStore:
    """memory_summaries 表 + Milvus memory_store 双写。"""

    def __init__(self, pool, milvus_collection=None) -> None:
        self._pool = pool
        self._milvus_collection = milvus_collection

    def _get_milvus(self):
        """延迟获取 Milvus collection（与 MemoryManager 同模式）。"""
        if self._milvus_collection is not None:
            return self._milvus_collection
        try:
            from pymilvus import Collection, connections

            from app.config import settings

            host = settings.milvus_address.split(":")[0]
            port = (
                int(settings.milvus_address.split(":")[1])
                if ":" in settings.milvus_address
                else 19530
            )
            connections.connect(alias="memory", host=host, port=port)
            self._milvus_collection = Collection("memory_store", using="memory")
            self._milvus_collection.load(using="memory")
        except Exception as e:
            logger.warning("Milvus not available for summaries: %s", e)
            self._milvus_collection = None
        return self._milvus_collection

    async def insert(
        self, entry: SummaryEntry, embedding: list[float] | None = None
    ) -> SummaryEntry:
        """插入摘要（PG + Milvus 双写）。content_hash 冲突时返回既有条目。"""
        pool = self._pool
        ch = entry.content_hash or compute_hash(
            entry.tenant_id, entry.user_id, entry.content
        )
        topics_json = json.dumps(entry.topics, ensure_ascii=False)
        entities_json = json.dumps(entry.entities, ensure_ascii=False)
        datetime.now(UTC)

        try:
            row = await pool.fetchrow(
                """INSERT INTO memory_summaries
                   (id, tenant_id, user_id, session_id, content, topics, entities,
                    turn_start, turn_end, content_hash, access_count, last_accessed_at,
                    status, created_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 0, NULL, 'active', NOW())
                   ON CONFLICT (tenant_id, user_id, content_hash) DO NOTHING
                   RETURNING id""",
                entry.id,
                entry.tenant_id,
                entry.user_id,
                entry.session_id,
                entry.content,
                topics_json,
                entities_json,
                entry.turn_start,
                entry.turn_end,
                ch,
            )
        except Exception as e:
            logger.error("SummaryStore insert PG failed: %s", e)
            raise

        if row is None:
            existing = await self.get_by_hash(entry.tenant_id, entry.user_id, ch)
            if existing is not None:
                return existing

        created = await self.get_by_id(entry.tenant_id, entry.user_id, entry.id)
        if created is None:
            raise RuntimeError(
                f"summary insert succeeded but row not found: {entry.id}"
            )

        # Milvus 双写（fail-soft：失败只警告，PG 已有数据）
        if embedding:
            await self._milvus_insert(created, embedding)
            created.embedding = embedding

        return created

    async def _milvus_insert(self, entry: SummaryEntry, embedding: list[float]) -> None:
        coll = self._get_milvus()
        if coll is None:
            return
        try:
            metadata = json.dumps(
                {
                    "session_id": entry.session_id,
                    "turn_range": [entry.turn_start, entry.turn_end],
                    "topics": entry.topics,
                    "entities": entry.entities,
                    "content_hash": entry.content_hash,
                    "access_count": 0,
                    "last_accessed_at": int(time.time()),
                    "status": "active",
                },
                ensure_ascii=False,
            )
            coll.insert(
                [
                    [entry.id],
                    [entry.tenant_id],
                    [entry.user_id],
                    [entry.session_id],
                    [entry.content],
                    ["summary"],
                    [embedding],
                    [str(int(time.time()))],
                    [metadata],
                ]
            )
            coll.flush(using="memory")
        except Exception as e:
            logger.warning("Milvus insert summary failed (PG has data): %s", e)

    async def get_by_id(
        self, tenant_id: str, user_id: str, summary_id: str
    ) -> SummaryEntry | None:
        row = await self._pool.fetchrow(
            f"SELECT {_COLUMNS} FROM memory_summaries WHERE id=$1 AND tenant_id=$2 AND user_id=$3",
            summary_id,
            tenant_id,
            user_id,
        )
        return _row_to_entry(row) if row else None

    async def get_by_hash(
        self, tenant_id: str, user_id: str, content_hash: str
    ) -> SummaryEntry | None:
        row = await self._pool.fetchrow(
            f"SELECT {_COLUMNS} FROM memory_summaries WHERE content_hash=$1 AND tenant_id=$2 AND user_id=$3",
            content_hash,
            tenant_id,
            user_id,
        )
        return _row_to_entry(row) if row else None

    async def list_active(
        self, tenant_id: str, user_id: str, limit: int = 50
    ) -> list[SummaryEntry]:
        rows = await self._pool.fetch(
            f"""SELECT {_COLUMNS} FROM memory_summaries
                WHERE tenant_id=$1 AND user_id=$2 AND status='active'
                ORDER BY created_at DESC LIMIT $3""",
            tenant_id,
            user_id,
            limit,
        )
        return [_row_to_entry(r) for r in rows]

    async def touch(self, summary_id: str) -> None:
        """命中时更新 access_count 和 last_accessed_at。"""
        await self._pool.execute(
            """UPDATE memory_summaries
               SET access_count = access_count + 1, last_accessed_at = NOW()
               WHERE id = $1""",
            summary_id,
        )

    async def archive(self, summary_id: str) -> bool:
        result = await self._pool.execute(
            "UPDATE memory_summaries SET status='archived' WHERE id=$1 AND status='active'",
            summary_id,
        )
        return "UPDATE 1" in (result or "")

    async def archive_expired(self, tenant_id: str, user_id: str, days: int) -> int:
        """归档超期未命中的摘要。"""
        result = await self._pool.execute(
            """UPDATE memory_summaries SET status='archived'
               WHERE tenant_id=$1 AND user_id=$2 AND status='active'
                 AND (last_accessed_at IS NULL AND created_at < NOW() - ($3 || ' days')::INTERVAL
                   OR last_accessed_at < NOW() - ($3 || ' days')::INTERVAL)""",
            tenant_id,
            user_id,
            str(days),
        )
        return (
            int((result or "").replace("UPDATE ", ""))
            if "UPDATE" in (result or "")
            else 0
        )

    async def count_by_topic(self, tenant_id: str, user_id: str, topic: str) -> int:
        row = await self._pool.fetchrow(
            """SELECT COUNT(*) as cnt FROM memory_summaries
               WHERE tenant_id=$1 AND user_id=$2 AND status='active'
                 AND topics @> $3::jsonb""",
            tenant_id,
            user_id,
            json.dumps([topic]),
        )
        return row["cnt"] if row else 0

    async def delete_all(self, tenant_id: str, user_id: str) -> int:
        result = await self._pool.execute(
            "DELETE FROM memory_summaries WHERE tenant_id=$1 AND user_id=$2",
            tenant_id,
            user_id,
        )
        return (
            int((result or "").replace("DELETE ", ""))
            if "DELETE" in (result or "")
            else 0
        )
