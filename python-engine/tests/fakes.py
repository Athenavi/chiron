"""测试共享假件（in-memory doubles）。

这些 fake 与真实持久层的**方法签名一一对应**，因此在测试里可以替换 asyncpg /
Milvus 实现而不改被测代码：``MemoryService`` 只依赖 ``ProfileStore`` 与
``SummaryStore`` 的方法契约，不依赖它们的具体实现。

此前 ``InMemoryProfileStore`` 在 ``test_memory_profile.py`` 与 ``test_memory_l3.py``
各写了一份（逐字相同），``InMemorySummaryStore`` 亦然。两份副本一旦漂移，
同一段被测逻辑会在两个文件里得到不同的结论 —— 所以集中到这里。

注意：``FakeEmbedder`` **不**放在这里 —— 各测试对「未映射文本」的期望不同
（有的要 fail-soft 存 NULL，有的要默认向量），强行合并会把测试意图藏进一个布尔开关。
"""

from __future__ import annotations

from datetime import UTC, datetime


class InMemorySummaryStore:
    """``app.memory.summaries.SummaryStore`` 的内存实现（方法签名一致）。"""

    def __init__(self) -> None:
        self.by_id: dict[str, object] = {}

    def _active(self, tenant_id, user_id):
        return [
            e for e in self.by_id.values()
            if e.tenant_id == tenant_id and e.user_id == user_id and e.status == "active"
        ]

    async def insert(self, entry, embedding=None):
        from app.memory.summaries import compute_hash

        ch = entry.content_hash or compute_hash(
            entry.tenant_id, entry.user_id, entry.content
        )
        entry.content_hash = ch
        # 精确去重：同一 (tenant, user, content) 只保留一条
        for existing in self.by_id.values():
            if (
                existing.tenant_id == entry.tenant_id
                and existing.user_id == entry.user_id
                and existing.content_hash == ch
            ):
                return existing
        entry.created_at = datetime.now(UTC)
        if embedding:
            entry.embedding = embedding
        self.by_id[entry.id] = entry
        return entry

    async def get_by_id(self, tenant_id, user_id, summary_id):
        e = self.by_id.get(summary_id)
        return e if e and e.tenant_id == tenant_id and e.user_id == user_id else None

    async def get_by_hash(self, tenant_id, user_id, content_hash):
        for e in self.by_id.values():
            if (
                e.tenant_id == tenant_id
                and e.user_id == user_id
                and e.content_hash == content_hash
            ):
                return e
        return None

    async def list_active(self, tenant_id, user_id, limit=50):
        items = self._active(tenant_id, user_id)
        items.sort(
            key=lambda e: e.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return items[:limit]

    async def touch(self, summary_id):
        e = self.by_id.get(summary_id)
        if e:
            e.access_count += 1
            e.last_accessed_at = datetime.now(UTC)

    async def archive(self, summary_id):
        e = self.by_id.get(summary_id)
        if e and e.status == "active":
            e.status = "archived"
            return True
        return False

    async def archive_expired(self, tenant_id, user_id, days):
        count = 0
        for e in self._active(tenant_id, user_id):
            ref = e.last_accessed_at or e.created_at
            if ref and (datetime.now(UTC) - ref).days > days:
                e.status = "archived"
                count += 1
        return count

    async def count_by_topic(self, tenant_id, user_id, topic):
        return len([e for e in self._active(tenant_id, user_id) if topic in e.topics])

    async def delete_all(self, tenant_id, user_id):
        ids = [
            k for k, v in self.by_id.items()
            if v.tenant_id == tenant_id and v.user_id == user_id
        ]
        for i in ids:
            del self.by_id[i]
        return len(ids)


class InMemoryProfileStore:
    """``app.memory.profile.ProfileStore`` 的内存实现（方法签名一致）。"""

    def __init__(self) -> None:
        self.by_key: dict[tuple, object] = {}
        self.by_id: dict[str, object] = {}

    def _key(self, tenant_id, user_id, slot, item_key):
        return (tenant_id, user_id, slot, item_key)

    async def list(self, tenant_id, user_id, include_archived=False, slot=None):
        out = [
            e for e in self.by_key.values()
            if e.tenant_id == tenant_id
            and e.user_id == user_id
            and (include_archived or e.status == "active")
            and (slot is None or e.slot == slot)
        ]
        out.sort(
            key=lambda e: (
                e.slot,
                e.updated_at or datetime.min.replace(tzinfo=UTC),
            )
        )
        return out

    async def get_by_id(self, tenant_id, user_id, entry_id):
        e = self.by_id.get(entry_id)
        return e if e and e.tenant_id == tenant_id and e.user_id == user_id else None

    async def get_by_key(self, tenant_id, user_id, slot, item_key):
        return self.by_key.get(self._key(tenant_id, user_id, slot, item_key))

    async def count(self, tenant_id, user_id):
        return len([
            e for e in self.by_key.values()
            if e.tenant_id == tenant_id
            and e.user_id == user_id
            and e.status == "active"
        ])

    async def insert(self, entry):
        existing = self.by_key.get(
            self._key(entry.tenant_id, entry.user_id, entry.slot, entry.item_key)
        )
        if existing is not None:
            # upsert 语义：保留原 id / 创建时间 / 访问计数
            entry.id = existing.id
            entry.access_count = existing.access_count
            entry.created_at = existing.created_at
            del self.by_id[existing.id]
        entry.updated_at = datetime.now(UTC)
        if entry.created_at is None:
            entry.created_at = entry.updated_at
        self.by_key[
            self._key(entry.tenant_id, entry.user_id, entry.slot, entry.item_key)
        ] = entry
        self.by_id[entry.id] = entry
        return entry

    async def update(
        self,
        tenant_id,
        user_id,
        entry_id,
        *,
        item_key=None,
        item_value=None,
        confidence=None,
        source=None,
        embedding=None,
        embedding_set=False,
    ):
        e = self.by_id.get(entry_id)
        if e is None or e.tenant_id != tenant_id or e.user_id != user_id:
            return None
        if item_key is not None:
            del self.by_key[self._key(e.tenant_id, e.user_id, e.slot, e.item_key)]
            e.item_key = item_key
            self.by_key[self._key(e.tenant_id, e.user_id, e.slot, e.item_key)] = e
        if item_value is not None:
            e.item_value = item_value
        if confidence is not None:
            e.confidence = confidence
        if source is not None:
            e.source = source
        if embedding_set:
            e.embedding = embedding
        e.updated_at = datetime.now(UTC)
        return e

    async def set_embedding(self, entry_id, embedding):
        e = self.by_id.get(entry_id)
        if e is None:
            return False
        e.embedding = embedding
        return True

    async def delete(self, tenant_id, user_id, entry_id):
        e = self.by_id.get(entry_id)
        if e is None or e.tenant_id != tenant_id or e.user_id != user_id:
            return False
        del self.by_id[entry_id]
        self.by_key.pop(
            self._key(e.tenant_id, e.user_id, e.slot, e.item_key), None
        )
        return True

    async def delete_by_key(self, tenant_id, user_id, item_key, slot=None):
        targets = [
            e for e in self.by_key.values()
            if e.tenant_id == tenant_id
            and e.user_id == user_id
            and e.item_key == item_key
            and (slot is None or e.slot == slot)
        ]
        for e in targets:
            await self.delete(tenant_id, user_id, e.id)
        return len(targets)

    async def delete_all(self, tenant_id, user_id):
        targets = [
            e for e in self.by_key.values()
            if e.tenant_id == tenant_id and e.user_id == user_id
        ]
        for e in targets:
            await self.delete(tenant_id, user_id, e.id)
        return len(targets)

    async def archive(self, entry_id):
        e = self.by_id.get(entry_id)
        if e is None or e.status != "active":
            return False
        e.status = "archived"
        return True

    async def touch(self, entry_ids):
        for eid in entry_ids:
            e = self.by_id.get(eid)
            if e:
                e.access_count += 1
                e.last_accessed_at = datetime.now(UTC)


class InMemoryConflictManager:
    """``ConflictManager`` 的内存替身（实现同一份共享存储契约）。

    核心用途是验证「一个副本登记的冲突，另一个副本能看到并裁决」—— 让两个
    ``MemoryService`` 共享同一个实例即可模拟 Redis 共享存储。
    """

    def __init__(self) -> None:
        self.by_id: dict[str, dict] = {}

    async def register_conflict(self, conflict) -> bool:
        self.by_id[conflict.conflict_id] = {
            "conflict_id": conflict.conflict_id,
            "slot": conflict.slot.value,
            "item_key": conflict.item_key,
            "old_value": conflict.old_value,
            "new_value": conflict.new_value,
            "source": conflict.source.value,
            "tenant_id": conflict.tenant_id,
            "user_id": conflict.user_id,
            "created_at": conflict.created_at,
        }
        return True

    def _as_conflict(self, d: dict):
        from app.memory.layers import MemoryConflict, SlotType, SourceType

        return MemoryConflict(
            conflict_id=d["conflict_id"],
            slot=SlotType(d["slot"]),
            item_key=d["item_key"],
            old_value=d["old_value"],
            new_value=d["new_value"],
            source=SourceType(d["source"]),
            tenant_id=d["tenant_id"],
            user_id=d["user_id"],
            created_at=d["created_at"],
        )

    async def get_pending_conflicts(self, tenant_id, user_id):
        return [
            self._as_conflict(d)
            for d in self.by_id.values()
            if d["tenant_id"] == tenant_id and d["user_id"] == user_id
        ]

    async def get_conflict(self, conflict_id):
        d = self.by_id.get(conflict_id)
        return self._as_conflict(d) if d else None

    async def resolve_conflict(self, conflict_id, resolution, manual_value=None):
        d = self.by_id.pop(conflict_id, None)
        if d is None:
            return False, None
        return True, {"conflict_id": conflict_id, "resolution": resolution}

    async def delete_conflict(self, conflict_id) -> bool:
        return self.by_id.pop(conflict_id, None) is not None
