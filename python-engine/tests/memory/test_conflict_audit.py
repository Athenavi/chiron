"""C4 · 冲突审计落 PG：登记 / 裁决 / 删除各留一条流水。

审计是**旁路**：一是它绝不能成为裁决列表的数据来源（Redis 才是活数据），二是它写
失败不能让用户的裁决动作失败。这两点在这里各有一条断言。
"""

from __future__ import annotations

from typing import Any

from app.memory.conflict_audit import (
    EVENT_DETECTED,
    EVENT_DISMISSED,
    EVENT_RESOLVED,
    PgConflictAuditor,
)
from app.memory.conflict_manager import PENDING_CONFIRMATION_TTL, ConflictManager
from app.memory.layers import MemoryConflict, ProfileItem, SlotType, SourceType
from app.memory.service import MemoryService
from tests.fakes import InMemoryProfileStore
from tests.memory.fake_redis import FakeRedis


class RecordingAuditor:
    """把每次 record 记下来，供断言事件序列与载荷。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def record(self, event: str, conflict: dict[str, Any]) -> None:
        self.events.append((event, conflict))

    @property
    def names(self) -> list[str]:
        return [e for e, _ in self.events]


class BrokenAuditor:
    """总是抛异常的审计器，用于验证审计旁路不会拖垮主流程。"""

    async def record(self, event: str, conflict: dict[str, Any]) -> None:
        raise RuntimeError("audit sink down")


def _conflict(conflict_id: str = "c1", *, tenant: str = "t1", user: str = "u1") -> MemoryConflict:
    return MemoryConflict(
        conflict_id=conflict_id,
        slot=SlotType.FACT,
        item_key="city",
        old_value="上海",
        new_value="北京",
        source=SourceType.DERIVED,
        tenant_id=tenant,
        user_id=user,
        created_at=1.0,
    )


def _user_confirmed_item() -> ProfileItem:
    """一条 user_confirmed 的既有档案卡条目，用于触发 detect_and_handle_conflict。"""
    return ProfileItem(
        slot=SlotType.FACT,
        item_key="city",
        item_value="上海",
        confidence=95,
        source=SourceType.USER_CONFIRMED,
        version=1,
        confirmed_at=1.0,
        last_referenced_at=None,
        created_at=1.0,
        updated_at=1.0,
    )


class TestConflictAuditEvents:
    async def test_register_resolve_delete_are_each_audited(self):
        redis = FakeRedis()
        auditor = RecordingAuditor()
        cm = ConflictManager(redis, auditor=auditor)

        await cm.register_conflict(_conflict("c1"))
        assert auditor.names == [EVENT_DETECTED]
        assert auditor.events[-1][1]["conflict_id"] == "c1"
        assert auditor.events[-1][1]["tenant_id"] == "t1"

        await cm.resolve_conflict("c1", "use_new")
        assert auditor.names == [EVENT_DETECTED, EVENT_RESOLVED]
        # 裁决流水带上最终值，事后可对账「当时选了哪个」
        assert auditor.events[-1][1]["final_value"] == "北京"

        await cm.register_conflict(_conflict("c2"))
        await cm.delete_conflict("c2")
        assert auditor.names == [
            EVENT_DETECTED,
            EVENT_RESOLVED,
            EVENT_DETECTED,
            EVENT_DISMISSED,
        ]

    async def test_detect_and_handle_conflict_also_audits(self):
        redis = FakeRedis()
        auditor = RecordingAuditor()
        cm = ConflictManager(redis, auditor=auditor)

        blocked, conflict = await cm.detect_and_handle_conflict(
            "t1",
            "u1",
            SlotType.FACT,
            "city",
            "北京",
            SourceType.DERIVED,
            _user_confirmed_item(),
        )
        assert blocked is True and conflict is not None
        assert auditor.names == [EVENT_DETECTED]

    async def test_broken_auditor_does_not_break_flow(self):
        redis = FakeRedis()
        cm = ConflictManager(redis, auditor=BrokenAuditor())

        # 审计器抛异常，登记/读取/裁决仍然照常成功
        assert await cm.register_conflict(_conflict("c1")) is True
        assert (await cm.get_pending_conflicts("t1", "u1"))[0].conflict_id == "c1"
        ok, _ = await cm.resolve_conflict("c1", "keep_old")
        assert ok is True

    async def test_default_auditor_is_noop(self):
        redis = FakeRedis()
        cm = ConflictManager(redis)  # 未注入审计器
        assert await cm.register_conflict(_conflict("c1")) is True
        assert len(await cm.get_pending_conflicts("t1", "u1")) == 1


class TestConflictAuditNotTheSource:
    """PG 审计只留痕，裁决列表一律以 Redis 为准。"""

    async def test_audit_record_survives_but_list_follows_redis(self):
        redis = FakeRedis()
        auditor = RecordingAuditor()
        svc = MemoryService(
            store=InMemoryProfileStore(),
            conflict_manager=ConflictManager(redis, auditor=auditor),
        )
        await svc.upsert(
            "t1", "u1", "fact", "city", "上海", confidence=95, source="user_confirmed"
        )
        await svc.upsert(
            "t1", "u1", "fact", "city", "北京", confidence=50, source="derived"
        )
        assert EVENT_DETECTED in auditor.names

        # Redis 里的活数据过期 → 列表清空；PG 审计流水仍在（只是不再是查询入口）
        redis.advance(PENDING_CONFIRMATION_TTL + 1)
        assert await svc.list_conflicts_shared("t1", "u1") == []
        assert EVENT_DETECTED in auditor.names


class TestPgConflictAuditor:
    class _RecordingPool:
        """只实现 execute，捕获 SQL 与参数。"""

        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        async def execute(self, query: str, *args: Any) -> str:
            self.calls.append((query, args))
            return "INSERT 0 1"

    async def test_writes_into_audit_logs(self):
        pool = self._RecordingPool()
        auditor = PgConflictAuditor(pool)
        await auditor.record(
            EVENT_DETECTED,
            {"conflict_id": "c1", "tenant_id": "t1", "user_id": "u1"},
        )

        assert len(pool.calls) == 1
        query, args = pool.calls[0]
        assert "INSERT INTO audit_logs" in query
        # 参数顺序：id, tenant_id, user_id, action, resource_type, resource_id, details, created_at
        assert args[1] == "t1"
        assert args[2] == "u1"
        assert args[3] == "memory.conflict.detected"
        assert args[5] == "c1"

    async def test_pg_failure_is_fail_soft(self):
        class _BrokenPool:
            async def execute(self, query: str, *args: Any) -> str:
                raise RuntimeError("db down")

        auditor = PgConflictAuditor(_BrokenPool())
        # 不抛异常：写审计失败被吞掉，调用方无感
        await auditor.record(EVENT_DETECTED, {"conflict_id": "c1"})
