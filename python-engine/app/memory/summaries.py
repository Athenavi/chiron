"""L3 摘要持久层 — memory_summaries 表的 asyncpg CRUD + 嵌入向量。

- PG 存正文/元数据/运维字段，**也存向量**（`embedding float8[]`，迁移 0019）：向量是判重与
  语义召回的唯一数据来源，只活在外部库里就等于降级模式下没有这一层
- Milvus 只是**可选镜像**：`MEMORY_VECTOR_BACKEND=milvus` 时才写（默认 pg 时不写，
  因为没人会去查它）
- content_hash 精确去重（PG 唯一索引拦截）
- 租户行级隔离：所有查询携带 tenant_id/user_id

投影口径与 M12 一致：**向量只在真要算相似度的那条路上取**。`_COLUMNS` 不含 embedding，
`list_active(with_embedding=True)` 才走 `_COLUMNS_WITH_VECTOR` —— 记忆面板列 50 条摘要
不该为此反序列化 50×1536 个浮点数。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Any

from app.memory.layers import SummaryEntry
from app.memory.vector_backend import MILVUS, FailureLog, parse_vector, resolve

logger = logging.getLogger(__name__)

_COLUMNS = (
    "id, tenant_id, user_id, session_id, content, topics, entities, "
    "turn_start, turn_end, content_hash, access_count, last_accessed_at, "
    "status, created_at"
)

# 只有近重复扫描这一条路需要向量，所以它单独一个投影而不是给所有读路径都带上列。
_COLUMNS_WITH_VECTOR = f"{_COLUMNS}, embedding"


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
        # 未选这一列的投影返回 None（"这条只能按时间参与召回"），不是空向量。
        embedding=parse_vector(row.get("embedding")),
    )


class SummaryStore:
    """memory_summaries 表（含向量列）+ 可选 Milvus 镜像。"""

    def __init__(self, pool: Any, milvus_collection: Any = None) -> None:
        self._pool = pool
        self._milvus_collection = milvus_collection
        self._milvus_fail = FailureLog("Milvus 摘要写入")

    def _get_milvus(self) -> Any:
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
            self._milvus_fail.success()
        except Exception as e:
            # 不可达要看得见：静默返回 None 的话，症状只是"镜像里没有数据"。
            self._milvus_fail.failure(str(e))
            self._milvus_collection = None
        return self._milvus_collection

    async def insert(
        self, entry: SummaryEntry, embedding: list[float] | None = None
    ) -> SummaryEntry:
        """插入摘要。content_hash 冲突时返回既有条目。

        向量**总是**写进 PG（判重与语义召回的数据来源，迁移 0019）；Milvus 只在后端配成
        milvus 时镜像一份 —— 配成 pg 时没有人查它，写它纯属多一次外部调用。
        """
        pool = self._pool
        ch = entry.content_hash or compute_hash(
            entry.tenant_id, entry.user_id, entry.content
        )
        topics_json = json.dumps(entry.topics, ensure_ascii=False)
        entities_json = json.dumps(entry.entities, ensure_ascii=False)

        try:
            row = await pool.fetchrow(
                """INSERT INTO memory_summaries
                   (id, tenant_id, user_id, session_id, content, topics, entities,
                    turn_start, turn_end, content_hash, access_count, last_accessed_at,
                    status, created_at, embedding)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 0, NULL, 'active',
                           NOW(), $11)
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
                list(embedding) if embedding else None,
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
        # get_by_id 走的是不含向量的投影；向量已经在手上，不必再读一遍 6KB 数组。
        created.embedding = embedding

        if embedding and resolve() == MILVUS:
            await self._milvus_insert(created, embedding)

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
            self._milvus_fail.success()
        except Exception as e:
            # PG 已经有这条（含向量），所以镜像失败不致命 —— 但必须记，
            # 否则"后端配成 milvus 却一直在写空的 Milvus"查不出来。
            self._milvus_fail.failure(f"insert summary {entry.id}: {e}")

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
        self, tenant_id: str, user_id: str, limit: int = 50, *, with_embedding: bool = False
    ) -> list[SummaryEntry]:
        """列出活跃摘要。

        ``with_embedding=True`` 只有一处正当用途：近重复扫描/语义召回要算余弦。
        默认不取，因为记忆面板列 50 条摘要时反序列化 50×1536 个浮点数是纯浪费（同 M12 对
        容量淘汰的处置）。
        """
        columns = _COLUMNS_WITH_VECTOR if with_embedding else _COLUMNS
        rows = await self._pool.fetch(
            f"""SELECT {columns} FROM memory_summaries
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
