"""Task 38: 单元测试 - 冲突管理。

测试 ConflictManager 和 ProfileCard 冲突处理逻辑：
- derived 二次出现自动写入
- user_confirmed 冲突挂起
- 三选项裁决生效
- 待确认 TTL
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.memory.conflict_manager import ConflictManager
from app.memory.layers import (
    ProfileItem,
    SlotType,
    SourceType,
)

# ── Mock Redis ───────────────────────────────────────────────────────


class _FakeRedis:
    """Mock Redis stub。"""

    def __init__(self):
        self._store: dict[str, str] = {}
        self._sets: dict[str, set[str]] = {}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, ex=None):
        self._store[key] = value
        return True

    async def setex(self, key, ttl, value):
        self._store[key] = value
        return True

    async def delete(self, *keys):
        for k in keys:
            self._store.pop(k, None)
        return len(keys)

    async def sadd(self, key, value):
        if key not in self._sets:
            self._sets[key] = set()
        self._sets[key].add(value)
        return True

    async def srem(self, key, value):
        if key in self._sets:
            self._sets[key].discard(value)
        return True

    async def smembers(self, key):
        return self._sets.get(key, set())

    async def expire(self, key, ttl):
        return True

    async def incr(self, key):
        count = int(self._store.get(key, "0"))
        count += 1
        self._store[key] = str(count)
        return count


# ── Mock ProfileItem ─────────────────────────────────────────────────


def _make_profile_item(
    slot: SlotType,
    item_key: str,
    item_value: Any,
    source: SourceType = SourceType.DERIVED,
    confirmed_at: float | None = None,
) -> ProfileItem:
    """创建 ProfileItem 测试实例。"""
    now = time.time()
    return ProfileItem(
        slot=slot,
        item_key=item_key,
        item_value=item_value,
        confidence=80,
        source=source,
        version=1,
        confirmed_at=confirmed_at,
        last_referenced_at=None,
        created_at=now - 3600,
        updated_at=now - 3600,
    )


# ── Tests ─────────────────────────────────────────────────────────────


@pytest.fixture
def redis():
    return _FakeRedis()


@pytest.fixture
def manager(redis):
    return ConflictManager(redis)


class TestConflictDetection:
    """冲突检测测试。"""

    @pytest.mark.asyncio
    async def test_user_confirmed_conflict_blocks_write(self, manager):
        """user_confirmed 条目冲突时，阻止写入。"""
        existing = _make_profile_item(
            slot=SlotType.PREFERENCE,
            item_key="lang",
            item_value="English",
            source=SourceType.USER_CONFIRMED,
            confirmed_at=time.time(),
        )

        should_block, conflict = await manager.detect_and_handle_conflict(
            tenant_id="t1",
            user_id="u1",
            slot=SlotType.PREFERENCE,
            item_key="lang",
            new_value="中文",
            new_source=SourceType.DERIVED,
            existing_item=existing,
        )

        assert should_block is True
        assert conflict is not None
        assert conflict.slot == SlotType.PREFERENCE
        assert conflict.item_key == "lang"
        assert conflict.old_value == "English"
        assert conflict.new_value == "中文"
        assert conflict.tenant_id == "t1"
        assert conflict.user_id == "u1"

    @pytest.mark.asyncio
    async def test_no_existing_item_allows_write(self, manager):
        """无现有条目时，允许写入。"""
        should_block, conflict = await manager.detect_and_handle_conflict(
            tenant_id="t1",
            user_id="u1",
            slot=SlotType.PREFERENCE,
            item_key="lang",
            new_value="English",
            new_source=SourceType.DERIVED,
            existing_item=None,
        )

        assert should_block is False
        assert conflict is None

    @pytest.mark.asyncio
    async def test_derived_existing_allows_write(self, manager):
        """现有条目是 derived 时，允许覆盖。"""
        existing = _make_profile_item(
            slot=SlotType.PREFERENCE,
            item_key="lang",
            item_value="English",
            source=SourceType.DERIVED,
        )

        should_block, conflict = await manager.detect_and_handle_conflict(
            tenant_id="t1",
            user_id="u1",
            slot=SlotType.PREFERENCE,
            item_key="lang",
            new_value="中文",
            new_source=SourceType.DERIVED,
            existing_item=existing,
        )

        assert should_block is False
        assert conflict is None

    @pytest.mark.asyncio
    async def test_tool_written_conflict_blocks_write(self, manager):
        """现有条目是 user_confirmed，新条目是 tool_written 时阻止。"""
        existing = _make_profile_item(
            slot=SlotType.PREFERENCE,
            item_key="lang",
            item_value="English",
            source=SourceType.USER_CONFIRMED,
            confirmed_at=time.time(),
        )

        should_block, conflict = await manager.detect_and_handle_conflict(
            tenant_id="t1",
            user_id="u1",
            slot=SlotType.PREFERENCE,
            item_key="lang",
            new_value="中文",
            new_source=SourceType.TOOL_WRITTEN,
            existing_item=existing,
        )

        assert should_block is True
        assert conflict is not None


class TestDerivedAutoWrite:
    """derived 二次出现自动写入测试。"""

    @pytest.mark.asyncio
    async def test_derived_second_occurrence_auto_writes(self, manager):
        """derived 二次出现时自动写入（不挂起）。"""
        existing = _make_profile_item(
            slot=SlotType.PREFERENCE,
            item_key="lang",
            item_value="English",
            source=SourceType.USER_CONFIRMED,
            confirmed_at=time.time(),
        )

        # 第一次出现：应该挂起
        should_block, conflict = await manager.detect_and_handle_conflict(
            tenant_id="t1",
            user_id="u1",
            slot=SlotType.PREFERENCE,
            item_key="lang",
            new_value="中文",
            new_source=SourceType.DERIVED,
            existing_item=existing,
        )
        assert should_block is True

        # 第二次出现：应该自动写入
        should_block, conflict = await manager.detect_and_handle_conflict(
            tenant_id="t1",
            user_id="u1",
            slot=SlotType.PREFERENCE,
            item_key="lang",
            new_value="中文",
            new_source=SourceType.DERIVED,
            existing_item=existing,
        )
        assert should_block is False  # 不再阻止
        assert conflict is None

    @pytest.mark.asyncio
    async def test_derived_third_occurrence_still_auto_writes(self, manager):
        """derived 第三次出现仍然自动写入。"""
        existing = _make_profile_item(
            slot=SlotType.PREFERENCE,
            item_key="lang",
            item_value="English",
            source=SourceType.USER_CONFIRMED,
            confirmed_at=time.time(),
        )

        # 触发两次，达到阈值
        for _ in range(2):
            await manager.detect_and_handle_conflict(
                tenant_id="t1",
                user_id="u1",
                slot=SlotType.PREFERENCE,
                item_key="lang",
                new_value="中文",
                new_source=SourceType.DERIVED,
                existing_item=existing,
            )

        # 第三次：仍然自动写入
        should_block, conflict = await manager.detect_and_handle_conflict(
            tenant_id="t1",
            user_id="u1",
            slot=SlotType.PREFERENCE,
            item_key="lang",
            new_value="中文",
            new_source=SourceType.DERIVED,
            existing_item=existing,
        )
        assert should_block is False

    @pytest.mark.asyncio
    async def test_different_items_have_independent_counters(self, manager):
        """不同条目的计数独立。"""
        existing_a = _make_profile_item(
            slot=SlotType.PREFERENCE,
            item_key="lang",
            item_value="English",
            source=SourceType.USER_CONFIRMED,
            confirmed_at=time.time(),
        )
        existing_b = _make_profile_item(
            slot=SlotType.PREFERENCE,
            item_key="timezone",
            item_value="UTC",
            source=SourceType.USER_CONFIRMED,
            confirmed_at=time.time(),
        )

        # 触发 A 两次
        await manager.detect_and_handle_conflict(
            tenant_id="t1", user_id="u1",
            slot=SlotType.PREFERENCE, item_key="lang",
            new_value="中文", new_source=SourceType.DERIVED,
            existing_item=existing_a,
        )

        # B 第一次应该仍然挂起
        should_block, conflict = await manager.detect_and_handle_conflict(
            tenant_id="t1", user_id="u1",
            slot=SlotType.PREFERENCE, item_key="timezone",
            new_value="Asia/Shanghai", new_source=SourceType.DERIVED,
            existing_item=existing_b,
        )
        assert should_block is True


class TestPendingConfirmation:
    """pending_confirmation 存储测试。"""

    @pytest.mark.asyncio
    async def test_conflict_stored_in_redis(self, manager, redis):
        """冲突应存储在 Redis 中。"""
        existing = _make_profile_item(
            slot=SlotType.PREFERENCE,
            item_key="lang",
            item_value="English",
            source=SourceType.USER_CONFIRMED,
            confirmed_at=time.time(),
        )

        should_block, conflict = await manager.detect_and_handle_conflict(
            tenant_id="t1",
            user_id="u1",
            slot=SlotType.PREFERENCE,
            item_key="lang",
            new_value="中文",
            new_source=SourceType.DERIVED,
            existing_item=existing,
        )

        assert conflict is not None
        key = f"memory:conflict:pending:{conflict.conflict_id}"
        assert redis._store.get(key) is not None

    @pytest.mark.asyncio
    async def test_list_conflicts_for_user(self, manager, redis):
        """能列出用户的所有待裁决冲突。"""
        existing = _make_profile_item(
            slot=SlotType.PREFERENCE,
            item_key="lang",
            item_value="English",
            source=SourceType.USER_CONFIRMED,
            confirmed_at=time.time(),
        )

        # 创建两个冲突
        for item_key in ["lang", "timezone"]:
            await manager.detect_and_handle_conflict(
                tenant_id="t1",
                user_id="u1",
                slot=SlotType.PREFERENCE,
                item_key=item_key,
                new_value="new_value",
                new_source=SourceType.DERIVED,
                existing_item=existing,
            )

        conflicts = await manager.get_pending_conflicts(
            tenant_id="t1", user_id="u1"
        )
        assert len(conflicts) == 2
        keys = {c.item_key for c in conflicts}
        assert "lang" in keys
        assert "timezone" in keys

    @pytest.mark.asyncio
    async def test_get_conflict_by_id(self, manager):
        """能通过 ID 获取单个冲突。"""
        existing = _make_profile_item(
            slot=SlotType.PREFERENCE,
            item_key="lang",
            item_value="English",
            source=SourceType.USER_CONFIRMED,
            confirmed_at=time.time(),
        )

        should_block, conflict = await manager.detect_and_handle_conflict(
            tenant_id="t1",
            user_id="u1",
            slot=SlotType.PREFERENCE,
            item_key="lang",
            new_value="中文",
            new_source=SourceType.DERIVED,
            existing_item=existing,
        )

        assert conflict is not None
        retrieved = await manager.get_conflict(conflict.conflict_id)
        assert retrieved is not None
        assert retrieved.item_key == "lang"
        assert retrieved.new_value == "中文"


class TestConflictResolution:
    """冲突裁决测试。"""

    @pytest.mark.asyncio
    async def test_resolve_keep_old(self, manager):
        """选择保留旧值。"""
        existing = _make_profile_item(
            slot=SlotType.PREFERENCE,
            item_key="lang",
            item_value="English",
            source=SourceType.USER_CONFIRMED,
            confirmed_at=time.time(),
        )

        should_block, conflict = await manager.detect_and_handle_conflict(
            tenant_id="t1",
            user_id="u1",
            slot=SlotType.PREFERENCE,
            item_key="lang",
            new_value="中文",
            new_source=SourceType.DERIVED,
            existing_item=existing,
        )

        assert conflict is not None
        success, result = await manager.resolve_conflict(
            conflict.conflict_id, "keep_old"
        )

        assert success is True
        assert result["final_value"] == "English"
        assert result["resolution"] == "keep_old"

        # 冲突应该被删除
        retrieved = await manager.get_conflict(conflict.conflict_id)
        assert retrieved is None

    @pytest.mark.asyncio
    async def test_resolve_use_new(self, manager):
        """选择采用新值。"""
        existing = _make_profile_item(
            slot=SlotType.PREFERENCE,
            item_key="lang",
            item_value="English",
            source=SourceType.USER_CONFIRMED,
            confirmed_at=time.time(),
        )

        should_block, conflict = await manager.detect_and_handle_conflict(
            tenant_id="t1",
            user_id="u1",
            slot=SlotType.PREFERENCE,
            item_key="lang",
            new_value="中文",
            new_source=SourceType.DERIVED,
            existing_item=existing,
        )

        assert conflict is not None
        success, result = await manager.resolve_conflict(
            conflict.conflict_id, "use_new"
        )

        assert success is True
        assert result["final_value"] == "中文"
        assert result["resolution"] == "use_new"

    @pytest.mark.asyncio
    async def test_resolve_manual(self, manager):
        """手动修改值。"""
        existing = _make_profile_item(
            slot=SlotType.PREFERENCE,
            item_key="lang",
            item_value="English",
            source=SourceType.USER_CONFIRMED,
            confirmed_at=time.time(),
        )

        should_block, conflict = await manager.detect_and_handle_conflict(
            tenant_id="t1",
            user_id="u1",
            slot=SlotType.PREFERENCE,
            item_key="lang",
            new_value="中文",
            new_source=SourceType.DERIVED,
            existing_item=existing,
        )

        assert conflict is not None
        success, result = await manager.resolve_conflict(
            conflict.conflict_id, "manual", manual_value="日本語"
        )

        assert success is True
        assert result["final_value"] == "日本語"
        assert result["resolution"] == "manual"

    @pytest.mark.asyncio
    async def test_resolve_manual_without_value_fails(self, manager):
        """手动修改但未提供值时失败。"""
        existing = _make_profile_item(
            slot=SlotType.PREFERENCE,
            item_key="lang",
            item_value="English",
            source=SourceType.USER_CONFIRMED,
            confirmed_at=time.time(),
        )

        should_block, conflict = await manager.detect_and_handle_conflict(
            tenant_id="t1",
            user_id="u1",
            slot=SlotType.PREFERENCE,
            item_key="lang",
            new_value="中文",
            new_source=SourceType.DERIVED,
            existing_item=existing,
        )

        assert conflict is not None
        success, result = await manager.resolve_conflict(
            conflict.conflict_id, "manual"
        )

        assert success is False

    @pytest.mark.asyncio
    async def test_resolve_unknown_fails(self, manager):
        """未知裁决方式失败。"""
        existing = _make_profile_item(
            slot=SlotType.PREFERENCE,
            item_key="lang",
            item_value="English",
            source=SourceType.USER_CONFIRMED,
            confirmed_at=time.time(),
        )

        should_block, conflict = await manager.detect_and_handle_conflict(
            tenant_id="t1",
            user_id="u1",
            slot=SlotType.PREFERENCE,
            item_key="lang",
            new_value="中文",
            new_source=SourceType.DERIVED,
            existing_item=existing,
        )

        assert conflict is not None
        success, result = await manager.resolve_conflict(
            conflict.conflict_id, "invalid_choice"
        )

        assert success is False

    @pytest.mark.asyncio
    async def test_resolve_nonexistent_conflict_fails(self, manager):
        """裁决不存在的冲突失败。"""
        success, result = await manager.resolve_conflict(
            "nonexistent_id", "keep_old"
        )

        assert success is False
        assert result is None


class TestConflictDeletion:
    """冲突删除测试。"""

    @pytest.mark.asyncio
    async def test_delete_conflict(self, manager):
        """删除冲突（用户否认）。"""
        existing = _make_profile_item(
            slot=SlotType.PREFERENCE,
            item_key="lang",
            item_value="English",
            source=SourceType.USER_CONFIRMED,
            confirmed_at=time.time(),
        )

        should_block, conflict = await manager.detect_and_handle_conflict(
            tenant_id="t1",
            user_id="u1",
            slot=SlotType.PREFERENCE,
            item_key="lang",
            new_value="中文",
            new_source=SourceType.DERIVED,
            existing_item=existing,
        )

        assert conflict is not None
        success = await manager.delete_conflict(conflict.conflict_id)
        assert success is True

        # 冲突应该被删除
        retrieved = await manager.get_conflict(conflict.conflict_id)
        assert retrieved is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_conflict(self, manager):
        """删除不存在的冲突返回 False。"""
        success = await manager.delete_conflict("nonexistent_id")
        assert success is False


class TestEdgeCases:
    """边界情况测试。"""

    @pytest.mark.asyncio
    async def test_multiple_tenants_isolated(self, manager):
        """不同租户的冲突隔离。"""
        existing = _make_profile_item(
            slot=SlotType.PREFERENCE,
            item_key="lang",
            item_value="English",
            source=SourceType.USER_CONFIRMED,
            confirmed_at=time.time(),
        )

        # t1 用户的冲突
        await manager.detect_and_handle_conflict(
            tenant_id="t1",
            user_id="u1",
            slot=SlotType.PREFERENCE,
            item_key="lang",
            new_value="中文",
            new_source=SourceType.DERIVED,
            existing_item=existing,
        )

        # t2 用户的冲突
        await manager.detect_and_handle_conflict(
            tenant_id="t2",
            user_id="u2",
            slot=SlotType.PREFERENCE,
            item_key="lang",
            new_value="日本語",
            new_source=SourceType.DERIVED,
            existing_item=existing,
        )

        # 列出 t1 的冲突，应只有 t1 的
        conflicts_t1 = await manager.get_pending_conflicts(
            tenant_id="t1", user_id="u1"
        )
        assert len(conflicts_t1) == 1
        assert conflicts_t1[0].new_value == "中文"

        # 列出 t2 的冲突，应只有 t2 的
        conflicts_t2 = await manager.get_pending_conflicts(
            tenant_id="t2", user_id="u2"
        )
        assert len(conflicts_t2) == 1
        assert conflicts_t2[0].new_value == "日本語"

    @pytest.mark.asyncio
    async def test_user_confirmed_over_conflict(self, manager):
        """新条目是 user_confirmed 时，允许覆盖现有 user_confirmed。"""
        existing = _make_profile_item(
            slot=SlotType.PREFERENCE,
            item_key="lang",
            item_value="English",
            source=SourceType.USER_CONFIRMED,
            confirmed_at=time.time(),
        )

        should_block, conflict = await manager.detect_and_handle_conflict(
            tenant_id="t1",
            user_id="u1",
            slot=SlotType.PREFERENCE,
            item_key="lang",
            new_value="中文",
            new_source=SourceType.USER_CONFIRMED,  # 用户再次确认
            existing_item=existing,
        )

        assert should_block is False
        assert conflict is None

    @pytest.mark.asyncio
    async def test_empty_conflict_list(self, manager):
        """用户无冲突时返回空列表。"""
        conflicts = await manager.get_pending_conflicts(
            tenant_id="unknown", user_id="user"
        )
        assert conflicts == []
