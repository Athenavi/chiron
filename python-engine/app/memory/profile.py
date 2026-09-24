"""L2 档案卡持久层 — user_memory_entries 表的 asyncpg CRUD。

- 槽位级 upsert（UNIQUE(tenant_id, user_id, slot, item_key)）
- embedding 以 JSONB 文本存取（asyncpg 默认 codec 下 JSONB 进出为 str，写 json.dumps / 读 json.loads）
- 所有方法显式携带 tenant_id/user_id，行级租户隔离
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.memory.layers import MemoryEntry

logger = logging.getLogger(__name__)

_COLUMNS = (
    "id, tenant_id, user_id, slot, item_key, item_value, confidence, source, "
    "embedding, access_count, last_accessed_at, status, created_at, updated_at"
)


def _row_to_entry(row: Any) -> MemoryEntry:
    """asyncpg Record → MemoryEntry（embedding 兼容 str/list 两种形态）。"""
    emb_raw = row["embedding"]
    embedding: list[float] | None = None
    if emb_raw is not None:
        if isinstance(emb_raw, str):
            try:
                emb_raw = json.loads(emb_raw)
            except (json.JSONDecodeError, TypeError):
                emb_raw = None
        if isinstance(emb_raw, (list, tuple)):
            embedding = [float(x) for x in emb_raw] or None
    return MemoryEntry(
        id=row["id"],
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        slot=row["slot"],
        item_key=row["item_key"],
        item_value=row["item_value"],
        confidence=int(row["confidence"]),
        source=row["source"],
        embedding=embedding,
        access_count=int(row["access_count"]),
        last_accessed_at=row["last_accessed_at"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ProfileStore:
    """用户长期记忆条目存储（依赖 asyncpg 连接池）。"""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # ── 读 ──────────────────────────────────────────────

    async def list(
        self,
        tenant_id: str,
        user_id: str,
        include_archived: bool = False,
        slot: str | None = None,
    ) -> list[MemoryEntry]:
        sql = f"SELECT {_COLUMNS} FROM user_memory_entries WHERE tenant_id=$1 AND user_id=$2"
        params: list[Any] = [tenant_id, user_id]
        if not include_archived:
            sql += " AND status='active'"
        if slot:
            sql += " AND slot=$3"
            params.append(slot)
        sql += " ORDER BY slot, updated_at DESC"
        rows = await self._pool.fetch(sql, *params)
        return [_row_to_entry(r) for r in rows]

    async def get_by_id(
        self, tenant_id: str, user_id: str, entry_id: str
    ) -> MemoryEntry | None:
        sql = f"SELECT {_COLUMNS} FROM user_memory_entries WHERE tenant_id=$1 AND user_id=$2 AND id=$3"
        row = await self._pool.fetchrow(sql, tenant_id, user_id, entry_id)
        return _row_to_entry(row) if row else None

    async def get_by_key(
        self, tenant_id: str, user_id: str, slot: str, item_key: str
    ) -> MemoryEntry | None:
        sql = (
            f"SELECT {_COLUMNS} FROM user_memory_entries "
            "WHERE tenant_id=$1 AND user_id=$2 AND slot=$3 AND item_key=$4"
        )
        row = await self._pool.fetchrow(sql, tenant_id, user_id, slot, item_key)
        return _row_to_entry(row) if row else None

    async def count(self, tenant_id: str, user_id: str) -> int:
        return int(
            await self._pool.fetchval(
                "SELECT COUNT(*) FROM user_memory_entries "
                "WHERE tenant_id=$1 AND user_id=$2 AND status='active'",
                tenant_id,
                user_id,
            )
        )

    # ── 写 ──────────────────────────────────────────────

    async def insert(self, entry: MemoryEntry) -> MemoryEntry:
        sql = (
            "INSERT INTO user_memory_entries "
            "(id, tenant_id, user_id, slot, item_key, item_value, confidence, source, embedding, status) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) "
            "ON CONFLICT (tenant_id, user_id, slot, item_key) DO UPDATE SET "
            "item_value=EXCLUDED.item_value, confidence=EXCLUDED.confidence, "
            "source=EXCLUDED.source, embedding=EXCLUDED.embedding, "
            "status='active', updated_at=NOW() "
            f"RETURNING {_COLUMNS}"
        )
        emb = json.dumps(entry.embedding) if entry.embedding else None
        row = await self._pool.fetchrow(
            sql,
            entry.id,
            entry.tenant_id,
            entry.user_id,
            entry.slot,
            entry.item_key,
            entry.item_value,
            entry.confidence,
            entry.source,
            emb,
            entry.status,
        )
        return _row_to_entry(row)

    async def update(
        self,
        tenant_id: str,
        user_id: str,
        entry_id: str,
        *,
        item_key: str | None = None,
        item_value: str | None = None,
        confidence: int | None = None,
        source: str | None = None,
        embedding: list[float] | None = None,
        embedding_set: bool = False,
    ) -> MemoryEntry | None:
        """按 id 局部更新（仅更新显式传入的字段；embedding_set 区分「清空向量」与「不改动」）。"""
        sets: list[str] = ["updated_at=NOW()"]
        params: list[Any] = [tenant_id, user_id, entry_id]
        idx = 4

        def _bind(sql_frag: str, value: Any) -> None:
            nonlocal idx
            params.append(value)
            sets.append(sql_frag.replace("?", f"${idx}"))
            idx += 1

        if item_key is not None:
            _bind("item_key=?", item_key)
        if item_value is not None:
            _bind("item_value=?", item_value)
        if confidence is not None:
            _bind("confidence=?", confidence)
        if source is not None:
            _bind("source=?", source)
        if embedding_set:
            emb = json.dumps(embedding) if embedding else None
            _bind("embedding=?", emb)

        sql = (
            "UPDATE user_memory_entries SET "
            + ", ".join(sets)
            + " WHERE tenant_id=$1 AND user_id=$2 AND id=$3 "
            f"RETURNING {_COLUMNS}"
        )
        row = await self._pool.fetchrow(sql, *params)
        return _row_to_entry(row) if row else None

    async def set_embedding(self, entry_id: str, embedding: list[float]) -> bool:
        row = await self._pool.fetchrow(
            "UPDATE user_memory_entries SET embedding=$2, updated_at=NOW() WHERE id=$1 RETURNING id",
            entry_id,
            json.dumps(embedding),
        )
        return row is not None

    async def delete(self, tenant_id: str, user_id: str, entry_id: str) -> bool:
        row = await self._pool.fetchrow(
            "DELETE FROM user_memory_entries WHERE tenant_id=$1 AND user_id=$2 AND id=$3 RETURNING id",
            tenant_id,
            user_id,
            entry_id,
        )
        return row is not None

    async def delete_by_key(
        self, tenant_id: str, user_id: str, item_key: str, slot: str | None = None
    ) -> int:
        """按 key 删除（可限定槽位）；返回删除条数。"""
        if slot:
            val = await self._pool.fetchval(
                "DELETE FROM user_memory_entries "
                "WHERE tenant_id=$1 AND user_id=$2 AND item_key=$3 AND slot=$4 RETURNING id",
                tenant_id,
                user_id,
                item_key,
                slot,
            )
            return 1 if val else 0
        rows = await self._pool.fetch(
            "DELETE FROM user_memory_entries "
            "WHERE tenant_id=$1 AND user_id=$2 AND item_key=$3 RETURNING id",
            tenant_id,
            user_id,
            item_key,
        )
        return len(rows)

    async def delete_all(self, tenant_id: str, user_id: str) -> int:
        rows = await self._pool.fetch(
            "DELETE FROM user_memory_entries WHERE tenant_id=$1 AND user_id=$2 RETURNING id",
            tenant_id,
            user_id,
        )
        return len(rows)

    async def archive(self, entry_id: str) -> bool:
        row = await self._pool.fetchrow(
            "UPDATE user_memory_entries SET status='archived', updated_at=NOW() "
            "WHERE id=$1 AND status='active' RETURNING id",
            entry_id,
        )
        return row is not None

    # ── 访问记账 ────────────────────────────────────────

    async def touch(self, entry_ids: list[str]) -> None:
        """命中即引用：access_count+1、last_accessed_at=now（搜索召回后调用）。"""
        if not entry_ids:
            return
        await self._pool.execute(
            "UPDATE user_memory_entries SET access_count=access_count+1, "
            "last_accessed_at=NOW() WHERE id = ANY($1)",
            entry_ids,
        )


def new_entry_id() -> str:
    return "mem_" + uuid.uuid4().hex[:24]

