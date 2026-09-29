"""C4 · 冲突落 Redis：跨副本可见、TTL 自动清理、跨租户隔离。

这三个性质在进程内 ``self._conflicts`` 上都不成立（各存各的、无过期、靠代码过滤），
只有在真正的共享存储上才成立。这里用带 TTL 的 ``FakeRedis`` 驱动**真实的**
``ConflictManager``（而不是一个替身镜像），验的就是它发出去的命令语义。
"""

from __future__ import annotations

from app.memory.conflict_manager import PENDING_CONFIRMATION_TTL, ConflictManager
from app.memory.layers import MemoryConflict, SlotType, SourceType
from app.memory.service import MemoryService
from tests.fakes import InMemoryProfileStore
from tests.memory.fake_redis import FakeRedis


def _conflict(
    conflict_id: str,
    *,
    tenant: str = "t1",
    user: str = "u1",
    item_key: str = "city",
    old: str = "上海",
    new: str = "北京",
) -> MemoryConflict:
    """构造一条 user_confirmed 被 derived 覆盖的冲突事件。"""
    return MemoryConflict(
        conflict_id=conflict_id,
        slot=SlotType.FACT,
        item_key=item_key,
        old_value=old,
        new_value=new,
        source=SourceType.DERIVED,
        tenant_id=tenant,
        user_id=user,
        created_at=1.0,
    )


class TestConflictRedisCrossReplica:
    """① 两个独立实例共享一份存储：一个写的，另一个看得见。"""

    async def test_replica_b_sees_and_resolves_replica_a_conflict(self):
        redis = FakeRedis()
        replica_a = ConflictManager(redis)
        replica_b = ConflictManager(redis)

        await replica_a.register_conflict(_conflict("c1"))

        # 副本 B 从未 register 过，仍能在列表里看到，并取到详情
        seen = await replica_b.get_pending_conflicts("t1", "u1")
        assert [c.conflict_id for c in seen] == ["c1"]
        detail = await replica_b.get_conflict("c1")
        assert detail is not None and detail.old_value == "上海"

        # 在 B 上裁决 → A 的列表也随之清空（共享同一份活数据）
        ok, _ = await replica_b.resolve_conflict("c1", "use_new")
        assert ok is True
        assert await replica_a.get_pending_conflicts("t1", "u1") == []


class TestConflictRedisTTL:
    """② TTL 到点自动清理，不需要额外回收任务。"""

    async def test_pending_entry_expires_after_ttl(self):
        redis = FakeRedis()
        cm = ConflictManager(redis)
        await cm.register_conflict(_conflict("c1"))
        assert len(await cm.get_pending_conflicts("t1", "u1")) == 1

        redis.advance(PENDING_CONFIRMATION_TTL + 1)

        assert await cm.get_pending_conflicts("t1", "u1") == []
        assert await cm.get_conflict("c1") is None

    async def test_not_expired_before_ttl(self):
        redis = FakeRedis()
        cm = ConflictManager(redis)
        await cm.register_conflict(_conflict("c1"))

        redis.advance(PENDING_CONFIRMATION_TTL - 1)

        assert len(await cm.get_pending_conflicts("t1", "u1")) == 1


class TestConflictRedisTenantIsolation:
    """③ 跨租户不串：租户 A 的冲突不出现在租户 B 的列表。"""

    async def test_tenant_list_is_isolated(self):
        redis = FakeRedis()
        cm = ConflictManager(redis)
        await cm.register_conflict(_conflict("c-a", tenant="ta", user="u1"))

        assert len(await cm.get_pending_conflicts("ta", "u1")) == 1
        assert await cm.get_pending_conflicts("tb", "u1") == []

    async def test_pending_list_key_carries_tenant(self):
        redis = FakeRedis()
        cm = ConflictManager(redis)
        await cm.register_conflict(_conflict("c-a", tenant="ta", user="u1"))

        # 待办集合键必须带 tenant：否则两个租户会共用同一个待办列表，
        # 「列表过滤」就会退化成「写入时就串了」。
        set_keys = list(redis._sets.keys())
        assert set_keys and all(":ta:" in k for k in set_keys)


class TestConflictRedisViaService:
    """经由 MemoryService 的端到端：两个副本共享一份 Redis。"""

    @staticmethod
    def _two_replicas(redis: FakeRedis) -> tuple[MemoryService, MemoryService]:
        store = InMemoryProfileStore()
        return (
            MemoryService(store=store, conflict_manager=ConflictManager(redis)),
            MemoryService(store=store, conflict_manager=ConflictManager(redis)),
        )

    async def test_conflicts_from_both_replicas_visible_on_both(self):
        redis = FakeRedis()
        a, b = self._two_replicas(redis)

        # 副本 A 产生一条冲突（city）
        await a.upsert(
            "t1", "u1", "fact", "city", "上海", confidence=95, source="user_confirmed"
        )
        await a.upsert(
            "t1", "u1", "fact", "city", "北京", confidence=50, source="derived"
        )
        # 副本 B 也产生一条冲突（food）—— 旧实现下两者各存各的，谁也看不到对方
        await b.upsert(
            "t1", "u1", "fact", "food", "面", confidence=95, source="user_confirmed"
        )
        await b.upsert(
            "t1", "u1", "fact", "food", "饭", confidence=50, source="derived"
        )

        seen_a = {c["key"] for c in await a.list_conflicts_shared("t1", "u1")}
        seen_b = {c["key"] for c in await b.list_conflicts_shared("t1", "u1")}
        assert seen_a == {"city", "food"}
        assert seen_b == {"city", "food"}

    async def test_expired_conflict_disappears_from_list(self):
        redis = FakeRedis()
        a, b = self._two_replicas(redis)
        await a.upsert(
            "t1", "u1", "fact", "city", "上海", confidence=95, source="user_confirmed"
        )
        await a.upsert(
            "t1", "u1", "fact", "city", "北京", confidence=50, source="derived"
        )
        assert await b.list_conflicts_shared("t1", "u1") != []

        redis.advance(PENDING_CONFIRMATION_TTL + 1)

        # 已落 Redis 的那条不被当作「只在本副本」补回，过期后列表即空。
        assert await b.list_conflicts_shared("t1", "u1") == []
