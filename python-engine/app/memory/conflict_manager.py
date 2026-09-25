"""冲突管理器 - 管理记忆条目的冲突检测、挂起与裁决。

本模块实现记忆冲突的完整生命周期管理：
1. 冲突检测：当新条目与 user_confirmed 条目冲突时产出 MemoryConflict 事件
2. 冲突挂起：将冲突存储在 Redis 中，等待用户裁决
3. 自动写入：derived 二次出现时自动写入（不挂起）
4. 冲突裁决：用户选择保留旧值、采用新值或手动修改

Redis 存储结构：
- pending_confirmation:{conflict_id}: 冲突详情（JSON）
- TTL: 7 天（604800 秒）
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import redis.asyncio as aioredis

from app.memory.layers import MemoryConflict, ProfileItem, SlotType, SourceType
from app.redis_keys import rkey

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────

PENDING_CONFIRMATION_TTL = 604800  # 7 天（秒）
AUTO_WRITE_THRESHOLD = 2  # derived 二次出现自动写入阈值


class ConflictManager:
    """冲突管理器 - 管理记忆条目的冲突检测、挂起与裁决。

    主要职责：
    1. 检测并产出冲突事件
    2. 管理 pending_confirmation 存储
    3. 支持 derived 二次出现自动写入
    4. 提供冲突裁决接口
    """

    def __init__(self, redis: aioredis.Redis | None = None):
        """初始化冲突管理器。

        Args:
            redis: Redis 连接实例（可为 None，此时冲突管理功能禁用）。
        """
        self._redis = redis

    def _require_redis(self) -> aioredis.Redis:
        """返回**已确认可用**的 Redis 连接。

        ``__init__`` 允许 ``redis=None``（此时冲突管理整体禁用），公开入口
        （``detect_and_handle_conflict`` / ``handle_memory_entry_conflict``）会提前
        降级，不会走到内部方法。这里收敛类型：真出现 None 说明调用方漏了入口检查，
        明确报错好过一句 AttributeError。
        """
        redis = self._redis
        if redis is None:
            raise RuntimeError("ConflictManager: Redis unavailable")
        return redis

    # ── Redis 键生成 ──────────────────────────────────────────────────────

    @staticmethod
    def _pending_key(conflict_id: str) -> str:
        """生成 pending_confirmation 存储键。"""
        return rkey(f"memory:conflict:pending:{conflict_id}")

    @staticmethod
    def _pending_list_key(tenant_id: str, user_id: str) -> str:
        """生成 pending_confirmation 列表键（用户维度）。"""
        return rkey(f"memory:conflict:pending_list:{tenant_id}:{user_id}")

    @staticmethod
    def _derived_count_key(
        tenant_id: str, user_id: str, slot: str, item_key: str
    ) -> str:
        """生成 derived 出现次数计数键。"""
        return rkey(
            f"memory:conflict:derived_count:{tenant_id}:{user_id}:{slot}:{item_key}"
        )

    # ── 冲突检测与产出 ──────────────────────────────────────────────────

    async def register_conflict(self, conflict: MemoryConflict) -> bool:
        """登记一条待裁决冲突。

        ``detect_and_handle_conflict`` 面向「ProfileItem 形态的既有条目」，而 L2
        记忆条目走的是 ``MemoryEntry``；写入侧自己完成检测后调这里落地即可，
        不必为了复用检测逻辑去构造一个 ProfileItem。

        Redis 不可用时返回 False —— 调用方据此退回进程内存储。
        """
        if self._redis is None:
            return False
        await self._store_pending_confirmation(conflict)
        return True

    async def detect_and_handle_conflict(
        self,
        tenant_id: str,
        user_id: str,
        slot: SlotType,
        item_key: str,
        new_value: Any,
        new_source: SourceType,
        existing_item: ProfileItem | None,
    ) -> tuple[bool, MemoryConflict | None]:
        """检测并处理冲突。

        Args:
            tenant_id: 租户 ID。
            user_id: 用户 ID。
            slot: 槽位类型。
            item_key: 条目键。
            new_value: 新值。
            new_source: 新值来源。
            existing_item: 现有条目（如果存在）。

        Returns:
            (是否应该阻止写入, 冲突事件)
        """
        if not existing_item:
            return False, None  # 无现有条目，直接写入

        # 如果 Redis 不可用，跳过冲突管理，允许直接写入
        if self._redis is None:
            logger.debug(
                "Conflict manager disabled (Redis unavailable), allowing write for %s:%s",
                slot,
                item_key,
            )
            return False, None

        # user_confirmed 冲突：如果现有条目是 user_confirmed，新条目不是
        if (
            existing_item.source == SourceType.USER_CONFIRMED
            and new_source != SourceType.USER_CONFIRMED
        ):
            # 检查 derived 二次出现
            if new_source == SourceType.DERIVED:
                count = await self._increment_derived_count(
                    tenant_id, user_id, slot.value, item_key
                )
                if count >= AUTO_WRITE_THRESHOLD:
                    # derived 二次出现，允许自动写入
                    logger.info(
                        "Derived auto-write triggered for %s:%s "
                        "(count=%d, tenant=%s, user=%s)",
                        slot,
                        item_key,
                        count,
                        tenant_id,
                        user_id,
                    )
                    return False, None  # 允许写入

            # 产出冲突事件并挂起
            conflict = self._create_conflict_event(
                tenant_id=tenant_id,
                user_id=user_id,
                slot=slot,
                item_key=item_key,
                old_value=existing_item.item_value,
                new_value=new_value,
                old_source=existing_item.source,
                new_source=new_source,
                old_confirmed_at=existing_item.confirmed_at,
            )
            await self._store_pending_confirmation(conflict)
            logger.info(
                "Conflict detected and stored: %s (slot=%s, key=%s)",
                conflict.conflict_id,
                slot,
                item_key,
            )
            return True, conflict  # 阻止写入

        return False, None  # 允许写入

    def _create_conflict_event(
        self,
        tenant_id: str,
        user_id: str,
        slot: SlotType,
        item_key: str,
        old_value: Any,
        new_value: Any,
        old_source: SourceType,
        new_source: SourceType,
        old_confirmed_at: float | None,
    ) -> MemoryConflict:
        """创建冲突事件。"""
        now = time.time()
        conflict_id = f"conf_{int(now)}_{hash(f'{tenant_id}:{user_id}:{slot.value}:{item_key}') % 10000}"

        return MemoryConflict(
            conflict_id=conflict_id,
            slot=slot,
            item_key=item_key,
            old_value=old_value,
            new_value=new_value,
            source=new_source,
            tenant_id=tenant_id,
            user_id=user_id,
            created_at=now,
        )

    # ── Pending Confirmation 存储 ──────────────────────────────────────

    async def _store_pending_confirmation(self, conflict: MemoryConflict) -> None:
        """存储 pending_confirmation 到 Redis。

        Args:
            conflict: 冲突事件。
        """
        # 存储冲突详情
        conflict_data = {
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
        redis = self._require_redis()
        key = self._pending_key(conflict.conflict_id)
        await redis.setex(key, PENDING_CONFIRMATION_TTL, json.dumps(conflict_data))

        # 添加到用户的 pending 列表
        list_key = self._pending_list_key(conflict.tenant_id, conflict.user_id)
        await redis.sadd(list_key, conflict.conflict_id)
        await redis.expire(list_key, PENDING_CONFIRMATION_TTL)

    async def _increment_derived_count(
        self,
        tenant_id: str,
        user_id: str,
        slot: str,
        item_key: str,
    ) -> int:
        """递增 derived 出现次数计数。

        Args:
            tenant_id: 租户 ID。
            user_id: 用户 ID。
            slot: 槽位类型值。
            item_key: 条目键。

        Returns:
            当前计数值。
        """
        redis = self._require_redis()
        key = self._derived_count_key(tenant_id, user_id, slot, item_key)
        count = await redis.incr(key)
        await redis.expire(key, PENDING_CONFIRMATION_TTL)
        return count

    # ── 冲突裁决 ──────────────────────────────────────────────────────────

    async def get_pending_conflicts(
        self,
        tenant_id: str,
        user_id: str,
    ) -> list[MemoryConflict]:
        """获取用户的待裁决冲突列表。

        Args:
            tenant_id: 租户 ID。
            user_id: 用户 ID。

        Returns:
            冲突事件列表。
        """
        redis = self._require_redis()
        list_key = self._pending_list_key(tenant_id, user_id)
        conflict_ids = await redis.smembers(list_key)

        conflicts = []
        for cid in conflict_ids:
            if isinstance(cid, bytes):
                cid = cid.decode("utf-8")
            key = self._pending_key(cid)
            data = await redis.get(key)
            if data:
                try:
                    item = json.loads(data)
                    conflicts.append(
                        MemoryConflict(
                            conflict_id=item["conflict_id"],
                            slot=SlotType(item["slot"]),
                            item_key=item["item_key"],
                            old_value=item["old_value"],
                            new_value=item["new_value"],
                            source=SourceType(item["source"]),
                            tenant_id=item["tenant_id"],
                            user_id=item["user_id"],
                            created_at=item["created_at"],
                        )
                    )
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning("Failed to parse conflict %s: %s", cid, e)

        return conflicts

    async def resolve_conflict(
        self,
        conflict_id: str,
        resolution: str,
        manual_value: Any | None = None,
    ) -> tuple[bool, dict[str, Any] | None]:
        """裁决冲突。

        Args:
            conflict_id: 冲突 ID。
            resolution: 裁决方式 ("keep_old", "use_new", "manual")
            manual_value: 手动修改的值（仅当 resolution == "manual"）

        Returns:
            (是否成功, 裁决详情)
        """
        redis = self._require_redis()
        key = self._pending_key(conflict_id)
        data = await redis.get(key)
        if not data:
            logger.warning("Conflict %s not found", conflict_id)
            return False, None

        try:
            conflict = json.loads(data)
        except json.JSONDecodeError:
            return False, None

        # 根据裁决方式确定最终值
        if resolution == "keep_old":
            final_value = conflict["old_value"]
        elif resolution == "use_new":
            final_value = conflict["new_value"]
        elif resolution == "manual":
            if manual_value is None:
                return False, {
                    "error": "manual_value is required for manual resolution"
                }
            final_value = manual_value
        else:
            return False, {"error": f"Unknown resolution: {resolution}"}

        # 删除 pending 记录
        await redis.delete(key)
        list_key = self._pending_list_key(conflict["tenant_id"], conflict["user_id"])
        await redis.srem(list_key, conflict_id)

        logger.info(
            "Conflict %s resolved: %s → %s",
            conflict_id,
            resolution,
            final_value,
        )

        return True, {
            "conflict_id": conflict_id,
            "final_value": final_value,
            "resolution": resolution,
            "slot": conflict["slot"],
            "item_key": conflict["item_key"],
        }

    async def delete_conflict(self, conflict_id: str) -> bool:
        """删除冲突记录（用户否认时调用）。

        Args:
            conflict_id: 冲突 ID。

        Returns:
            是否成功删除。
        """
        redis = self._require_redis()
        key = self._pending_key(conflict_id)
        data = await redis.get(key)
        if not data:
            return False

        try:
            conflict = json.loads(data)
        except json.JSONDecodeError:
            return False

        await redis.delete(key)
        list_key = self._pending_list_key(conflict["tenant_id"], conflict["user_id"])
        await redis.srem(list_key, conflict_id)

        logger.info("Conflict %s deleted (user denied)", conflict_id)
        return True

    async def get_conflict(self, conflict_id: str) -> MemoryConflict | None:
        """获取单个冲突详情。

        Args:
            conflict_id: 冲突 ID。

        Returns:
            冲突事件或 None。
        """
        redis = self._require_redis()
        key = self._pending_key(conflict_id)
        data = await redis.get(key)
        if not data:
            return None

        try:
            item = json.loads(data)
            return MemoryConflict(
                conflict_id=item["conflict_id"],
                slot=SlotType(item["slot"]),
                item_key=item["item_key"],
                old_value=item["old_value"],
                new_value=item["new_value"],
                source=SourceType(item["source"]),
                tenant_id=item["tenant_id"],
                user_id=item["user_id"],
                created_at=item["created_at"],
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to parse conflict %s: %s", conflict_id, e)
            return None
