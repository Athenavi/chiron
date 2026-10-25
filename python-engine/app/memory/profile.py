"""L2 档案卡持久层 — user_memory_entries 表的 asyncpg CRUD。

- 槽位级 upsert（UNIQUE(tenant_id, user_id, project, slot, item_key)）
- embedding 以 JSONB 文本存取（asyncpg 默认 codec 下 JSONB 进出为 str，写 json.dumps / 读 json.loads）
- 所有方法显式携带 tenant_id/user_id，行级租户隔离
"""

from __future__ import annotations

import builtins
import json
import logging
import uuid
from typing import Any, NamedTuple

from app.memory.layers import MemoryEntry

logger = logging.getLogger(__name__)

_COLUMNS = (
    "id, tenant_id, user_id, slot, item_key, item_value, confidence, source, "
    "embedding, access_count, last_accessed_at, status, created_at, updated_at, project"
)

#: 淘汰记账所需的最小列 —— **故意不含 embedding / item_value**（见 `list_evictable`）。
_EVICTION_COLUMNS = "id, confidence, source, last_accessed_at"


class EvictionRow(NamedTuple):
    """`list_evictable` 的行模型：容量淘汰判定要的四个字段，到此为止。

    与 ``MemoryEntry`` 分开是有意的 —— 它**没有向量**，绝不能喂给任何相似度逻辑。
    """

    id: str
    confidence: int
    source: Any
    last_accessed_at: Any


class UpsertResult(NamedTuple):
    """``upsert_protected`` 的结果三元组。

    * ``entry``：写入后的**最终行**（被守卫拦下时是原封不动的旧行）；
    * ``inserted``：本次是新建（True）还是覆盖已有行（False）；
    * ``protected``：True = 守卫拦下了写入（旧值是用户确认过的，本次来的是派生值）。
    """

    entry: MemoryEntry
    inserted: bool
    protected: bool


def _to_jsonb(value: Any) -> Any:
    """写入 ``jsonb`` 列：asyncpg 默认 codec 下必须传**合法 JSON 文本**。

    ``user_memory_entries.item_value`` 是 ``jsonb``。直接传裸文本（如
    "尽量使用最新更真实的数据"）会被 PostgreSQL 拒绝：``invalid input syntax for
    type json``（实测 ``POST /v1/memory/profile`` 因此返回 500）。``None`` 保持为 NULL。
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _from_jsonb(value: Any) -> Any:
    """读取 ``jsonb`` 列的对称处理（默认 codec 返回 JSON 文本 → 解析回原值）。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


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
        item_value=_from_jsonb(row["item_value"]),
        confidence=int(row["confidence"]),
        source=row["source"],
        embedding=embedding,
        access_count=int(row["access_count"]),
        last_accessed_at=row["last_accessed_at"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        project=str(row["project"] or ""),
    )


class ProfileStore:
    """用户长期记忆条目存储（依赖 asyncpg 连接池）。"""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # ── 读 ──────────────────────────────────────────────

    async def list(
        self,
        tenant_id: int,
        user_id: str,
        include_archived: bool = False,
        slot: str | None = None,
        project: str = "",
    ) -> list[MemoryEntry]:
        """按 (tenant, user, project) 列出条目。

        C3：`project` 空串 = **未分组**；不同项目互不可见（过滤而非排序）。
        """
        sql = (
            f"SELECT {_COLUMNS} FROM user_memory_entries "
            "WHERE tenant_id=$1 AND user_id=$2 AND project=$3"
        )
        params: list[Any] = [tenant_id, user_id, project]
        if not include_archived:
            sql += " AND status='active'"
        if slot:
            sql += " AND slot=$4"
            params.append(slot)
        sql += " ORDER BY slot, updated_at DESC"
        rows = await self._pool.fetch(sql, *params)
        return [_row_to_entry(r) for r in rows]

    async def list_evictable(
        self, tenant_id: int, user_id: str, project: str = ""
    ) -> builtins.list[EvictionRow]:
        """列出**未分组/某项目**内参与容量淘汰的行的最小必要列。

        为什么不复用 `list()`：淘汰只看置信度与新近度，而 `list()` 会把每条的
        1536 维 ``jsonb`` 向量 ``json.loads`` + 逐元素 ``float()`` 一遍。上限是 200 条，
        而这条路径**每次写记忆都要走一次** —— 等于每次写入白解析 20 万个浮点。

        排序与 `list()` 保持一致（``slot, updated_at DESC``）：淘汰打分并列时靠它保证
        确定性，换成别的顺序会让"哪条被挤掉"变得不可复现。
        """
        rows = await self._pool.fetch(
            f"SELECT {_EVICTION_COLUMNS} FROM user_memory_entries "
            "WHERE tenant_id=$1 AND user_id=$2 AND project=$3 AND status='active' "
            "ORDER BY slot, updated_at DESC",
            tenant_id,
            user_id,
            project,
        )
        return [
            EvictionRow(
                id=row["id"],
                confidence=int(row["confidence"]),
                source=row["source"],
                last_accessed_at=row["last_accessed_at"],
            )
            for row in rows
        ]

    async def get_by_id(
        self, tenant_id: int, user_id: str, entry_id: str, project: str = ""
    ) -> MemoryEntry | None:
        sql = (
            f"SELECT {_COLUMNS} FROM user_memory_entries "
            "WHERE tenant_id=$1 AND user_id=$2 AND id=$3 AND project=$4"
        )
        row = await self._pool.fetchrow(sql, tenant_id, user_id, entry_id, project)
        return _row_to_entry(row) if row else None

    async def get_by_key(
        self, tenant_id: int, user_id: str, slot: str, item_key: str, project: str = ""
    ) -> MemoryEntry | None:
        """按 (tenant, user, project, slot, key) 取一条 —— 与唯一约束同一组键（C3）。"""
        sql = (
            f"SELECT {_COLUMNS} FROM user_memory_entries "
            "WHERE tenant_id=$1 AND user_id=$2 AND slot=$3 AND item_key=$4 AND project=$5"
        )
        row = await self._pool.fetchrow(sql, tenant_id, user_id, slot, item_key, project)
        return _row_to_entry(row) if row else None

    async def count(self, tenant_id: int, user_id: str, project: str = "") -> int:
        return int(
            await self._pool.fetchval(
                "SELECT COUNT(*) FROM user_memory_entries "
                "WHERE tenant_id=$1 AND user_id=$2 AND project=$3 AND status='active'",
                tenant_id,
                user_id,
                project,
            )
        )

    # ── 写 ──────────────────────────────────────────────

    async def insert(self, entry: MemoryEntry) -> MemoryEntry:
        sql = (
            "INSERT INTO user_memory_entries "
            "(id, tenant_id, user_id, slot, item_key, item_value, confidence, source, "
            "embedding, status, project) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) "
            # C3：冲突目标必须与唯一约束一致（tenant, user, **project**, slot, key）——
            # 少了 project 会让"两个项目的同名键"互相覆盖，隔离就是假的。
            "ON CONFLICT (tenant_id, user_id, project, slot, item_key) DO UPDATE SET "
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
            _to_jsonb(entry.item_value),
            entry.confidence,
            entry.source,
            emb,
            entry.status,
            entry.project,
        )
        return _row_to_entry(row)

    async def upsert_protected(self, entry: MemoryEntry) -> UpsertResult:
        """一条语句完成"读—判—写"：**派生值不得覆盖用户确认过的值**。

        与 `insert` 的差别只在 `ON CONFLICT … DO UPDATE … WHERE` 的守卫：守卫不成立时
        UPDATE 影响 0 行、`RETURNING` 无输出 —— 于是回读原行并按"未写入"上报，
        调用方据此登记待裁决冲突。

        为什么把判定放进 SQL：此前 `upsert` 是 `get_by_key` → 据此决定是否登记冲突 → `insert`。
        两个并发写会各自读到"没有旧行"，既不登冲突又把用户确认的值悄悄盖掉 ——
        与 `MemoryService.upsert` 自己承诺的"不直接覆盖，登记待裁决"正好相反。
        `created` 同理改由 `(xmax = 0)` 判定（PostgreSQL 区分 ON CONFLICT 走了
        INSERT 还是 UPDATE 分支的标准手法），不再依赖那次有竞态的预读。
        """
        emb = json.dumps(entry.embedding) if entry.embedding else None
        row = await self._pool.fetchrow(
            "INSERT INTO user_memory_entries "
            "(id, tenant_id, user_id, slot, item_key, item_value, confidence, source, "
            "embedding, status, project) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) "
            "ON CONFLICT (tenant_id, user_id, project, slot, item_key) DO UPDATE SET "
            "item_value=EXCLUDED.item_value, confidence=EXCLUDED.confidence, "
            "source=EXCLUDED.source, embedding=EXCLUDED.embedding, "
            "status='active', updated_at=NOW() "
            # 守卫：旧值是用户确认的、而这次来的是派生值 ⇒ 不写。
            # 反过来（派生 → 用户确认、用户确认 → 用户确认）照写。
            "WHERE user_memory_entries.source <> 'user_confirmed' "
            "   OR EXCLUDED.source = 'user_confirmed' "
            f"RETURNING {_COLUMNS}, (xmax = 0) AS inserted",
            entry.id,
            entry.tenant_id,
            entry.user_id,
            entry.slot,
            entry.item_key,
            _to_jsonb(entry.item_value),
            entry.confidence,
            entry.source,
            emb,
            entry.status,
            entry.project,
        )
        if row is not None:
            return UpsertResult(
                entry=_row_to_entry(row), inserted=bool(row["inserted"]), protected=False
            )

        existing = await self.get_by_key(
            entry.tenant_id, entry.user_id, entry.slot, entry.item_key, project=entry.project
        )
        if existing is not None:
            return UpsertResult(entry=existing, inserted=False, protected=True)

        # 守卫拦下、回读却已经没有这一行 ⇒ 极小概率的并发删除。此时按普通写入落库即可
        # （没有需要保护的用户确认值了）。
        created = await self.insert(entry)
        return UpsertResult(entry=created, inserted=True, protected=False)

    async def update(
        self,
        tenant_id: int,
        user_id: str,
        entry_id: str,
        *,
        project: str = "",
        item_key: str | None = None,
        item_value: str | None = None,
        confidence: int | None = None,
        source: str | None = None,
        embedding: builtins.list[float] | None = None,
        embedding_set: bool = False,
    ) -> MemoryEntry | None:
        """按 id 局部更新（仅更新显式传入的字段；embedding_set 区分「清空向量」与「不改动」）。

        C3：`project` 参与 WHERE 过滤 —— 拿别的项目的 id 进来改不动任何行。
        """
        sets: list[str] = ["updated_at=NOW()"]
        params: list[Any] = [tenant_id, user_id, entry_id, project]
        idx = 5

        def _bind(sql_frag: str, value: Any) -> None:
            nonlocal idx
            params.append(value)
            sets.append(sql_frag.replace("?", f"${idx}"))
            idx += 1

        if item_key is not None:
            _bind("item_key=?", item_key)
        if item_value is not None:
            _bind("item_value=?", _to_jsonb(item_value))
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
            + " WHERE tenant_id=$1 AND user_id=$2 AND id=$3 AND project=$4 "
            f"RETURNING {_COLUMNS}"
        )
        row = await self._pool.fetchrow(sql, *params)
        return _row_to_entry(row) if row else None

    # 注意：本类定义了一个名为 ``list`` 的方法（见上），会遮蔽内置 ``list`` ——
    # 因此类内签名里的列表注解必须写 ``builtins.list``。
    async def set_embedding(
        self, entry_id: str, embedding: builtins.list[float]
    ) -> bool:
        row = await self._pool.fetchrow(
            "UPDATE user_memory_entries SET embedding=$2, updated_at=NOW() WHERE id=$1 RETURNING id",
            entry_id,
            json.dumps(embedding),
        )
        return row is not None

    async def delete(
        self, tenant_id: int, user_id: str, entry_id: str, project: str = ""
    ) -> bool:
        row = await self._pool.fetchrow(
            "DELETE FROM user_memory_entries "
            "WHERE tenant_id=$1 AND user_id=$2 AND id=$3 AND project=$4 RETURNING id",
            tenant_id,
            user_id,
            entry_id,
            project,
        )
        return row is not None

    async def delete_by_key(
        self,
        tenant_id: int,
        user_id: str,
        item_key: str,
        slot: str | None = None,
        project: str = "",
    ) -> int:
        """按 key 删除（可限定槽位）；返回删除条数。C3：project 参与过滤。"""
        if slot:
            val = await self._pool.fetchval(
                "DELETE FROM user_memory_entries "
                "WHERE tenant_id=$1 AND user_id=$2 AND item_key=$3 AND slot=$4 "
                "AND project=$5 RETURNING id",
                tenant_id,
                user_id,
                item_key,
                slot,
                project,
            )
            return 1 if val else 0
        rows = await self._pool.fetch(
            "DELETE FROM user_memory_entries "
            "WHERE tenant_id=$1 AND user_id=$2 AND item_key=$3 AND project=$4 RETURNING id",
            tenant_id,
            user_id,
            item_key,
            project,
        )
        return len(rows)

    async def delete_all(self, tenant_id: int, user_id: str, project: str = "") -> int:
        """清空某用户**某个项目**的条目（C3：project 空了只清"未分组"）。"""
        rows = await self._pool.fetch(
            "DELETE FROM user_memory_entries "
            "WHERE tenant_id=$1 AND user_id=$2 AND project=$3 RETURNING id",
            tenant_id,
            user_id,
            project,
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

    async def touch(self, entry_ids: builtins.list[str]) -> None:
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

