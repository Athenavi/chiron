"""L2 ProfileCard Provider - 用户档案卡数据管理。

本模块实现 L2 层用户档案卡的核心 CRUD 操作：
- get_profile: 读取整卡（带 Redis 缓存）
- upsert_item: 槽位级 upsert（含冲突规则）
- delete_item: 硬删除条目
- archive_low_confidence: 归档低置信度条目
- evict_over_limit: 条目软限淘汰
- 集成 ConflictManager 进行冲突检测与管理
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC
from typing import Any

import redis.asyncio as aioredis

from app.db import get_pool
from app.memory.conflict_manager import ConflictManager
from app.memory.layers import ConflictRef, ProfileItem, ProfileUpdateResult, SlotType, SourceType
from app.redis_keys import rkey

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────

CACHE_TTL = 60  # Redis 缓存 TTL（秒）
ARCHIVE_THRESHOLD_DAYS = 180  # 归档阈值（天）
MAX_ITEMS_LIMIT = 200  # 软限条目数


class ProfileCard:
    """L2 用户档案卡 Provider。

    负责管理 PostgreSQL 中 user_memory_profile 表的 CRUD 操作，
    并通过 Redis 缓存优化读取性能。

    集成 ConflictManager 进行冲突检测与管理：
    - user_confirmed 冲突挂起
    - derived 二次出现自动写入
    """

    def __init__(
        self,
        redis: aioredis.Redis | None = None,
        conflict_manager: ConflictManager | None = None,
    ):
        """初始化 ProfileCard。

        Args:
            redis: Redis 连接实例（用于缓存，可为 None）。
            conflict_manager: 冲突管理器（可选，用于冲突检测）。
        """
        self._redis = redis
        self._pool = get_pool()
        self._conflict_manager = conflict_manager or ConflictManager(redis)

    # ── 缓存键生成 ──────────────────────────────────────────────────────

    @staticmethod
    def _cache_key(tenant_id: str, user_id: str) -> str:
        """生成档案卡缓存键。"""
        return rkey(f"memory:profile:{tenant_id}:{user_id}")

    # ── 1. get_profile ──────────────────────────────────────────────────

    async def get_profile(self, tenant_id: str, user_id: str) -> list[ProfileItem]:
        """获取用户的完整档案卡（带 Redis 缓存）。

        优先从 Redis 缓存读取，缓存 miss 时查询 PostgreSQL 并回填。

        Args:
            tenant_id: 租户 ID。
            user_id: 用户 ID。

        Returns:
            用户档案条目列表，按 slot + item_key 排序。
        """
        cache_key = self._cache_key(tenant_id, user_id)

        # 尝试从缓存读取（如果 Redis 可用）
        if self._redis is not None:
            try:
                cached = await self._redis.get(cache_key)
                if cached:
                    try:
                        data = json.loads(cached)
                        return [ProfileItem(**item) for item in data]
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.warning(
                            "Failed to parse cached profile for user %s: %s", user_id, e
                        )
                        await self._redis.delete(cache_key)
            except Exception as e:
                logger.warning("Redis cache read failed for user %s: %s", user_id, e)

        # 缓存 miss 或 Redis 不可用，查询数据库
        items = await self._fetch_from_db(tenant_id, user_id)

        # 回填缓存（如果 Redis 可用）
        if items and self._redis is not None:
            try:
                await self._redis.setex(
                    cache_key,
                    CACHE_TTL,
                    json.dumps([self._item_to_dict(item) for item in items]),
                )
            except Exception as e:
                logger.warning("Failed to cache profile for user %s: %s", user_id, e)

        return items

    async def _fetch_from_db(self, tenant_id: str, user_id: str) -> list[ProfileItem]:
        """从 PostgreSQL 查询档案卡。"""
        query = """
            SELECT slot, item_key, item_value, confidence, source,
                   version, confirmed_at, last_referenced_at,
                   created_at, updated_at
            FROM user_memory_profile
            WHERE tenant_id = $1 AND user_id = $2
            ORDER BY slot, item_key
        """
        rows = await self._pool.fetch(query, tenant_id, user_id)
        return [
            ProfileItem(
                slot=SlotType(row["slot"]),
                item_key=row["item_key"],
                item_value=row["item_value"],
                confidence=row["confidence"],
                source=SourceType(row["source"]),
                version=row["version"],
                confirmed_at=(
                    row["confirmed_at"].timestamp() if row["confirmed_at"] else None
                ),
                last_referenced_at=(
                    row["last_referenced_at"].timestamp()
                    if row["last_referenced_at"]
                    else None
                ),
                created_at=row["created_at"].timestamp(),
                updated_at=row["updated_at"].timestamp(),
            )
            for row in rows
        ]

    # ── 2. upsert_item ──────────────────────────────────────────────────

    async def upsert_item(
        self,
        tenant_id: str,
        user_id: str,
        slot: SlotType,
        item_key: str,
        item_value: Any,
        confidence: int = 50,
        source: SourceType = SourceType.DERIVED,
    ) -> ProfileUpdateResult:
        """槽位级 upsert 档案条目。

        核心逻辑：
        1. 检查现有条目
        2. 使用 ConflictManager 进行冲突检测
           - user_confirmed 冲突挂起（等待用户裁决）
           - derived 二次出现自动写入
        3. 如果现有条目是 derived，允许覆盖（version+1）
        4. 如果是新条目，直接插入

        Args:
            tenant_id: 租户 ID。
            user_id: 用户 ID。
            slot: 槽位类型。
            item_key: 条目键。
            item_value: 条目值。
            confidence: 置信度 (0-100)。
            source: 来源类型。

        Returns:
            ProfileUpdateResult 包含更新结果和可能的冲突。
        """
        now = time.time()

        # 查询现有条目
        existing = await self._get_item(tenant_id, user_id, slot, item_key)

        # 使用 ConflictManager 进行冲突检测
        should_block, conflict_event = (
            await self._conflict_manager.detect_and_handle_conflict(
                tenant_id=tenant_id,
                user_id=user_id,
                slot=slot,
                item_key=item_key,
                new_value=item_value,
                new_source=source,
                existing_item=existing,
            )
        )

        # 如果需要阻止写入（user_confirmed 冲突）
        if should_block and conflict_event:
            # 将 MemoryConflict 转换为 ConflictRef 返回
            conflict = ConflictRef(
                conflict_id=conflict_event.conflict_id,
                slot=conflict_event.slot,
                item_key=conflict_event.item_key,
                old_value=conflict_event.old_value,
                new_value=conflict_event.new_value,
                old_source=SourceType(
                    SourceType.USER_CONFIRMED.value
                ),  # 现有条目是 user_confirmed
                old_confirmed_at=existing.confirmed_at if existing else None,
            )
            return ProfileUpdateResult(
                success=False,
                item=existing,
                conflict=conflict,
            )

        # 执行 upsert
        if existing:
            # 更新现有条目（version+1）
            new_version = existing.version + 1
            await self._update_item(
                tenant_id,
                user_id,
                slot,
                item_key,
                item_value,
                confidence,
                source,
                new_version,
                now,
            )
        else:
            # 插入新条目
            await self._insert_item(
                tenant_id,
                user_id,
                slot,
                item_key,
                item_value,
                confidence,
                source,
                now,
            )

        # 失效缓存
        await self._invalidate_cache(tenant_id, user_id)

        # 返回最新条目
        updated = await self._get_item(tenant_id, user_id, slot, item_key)
        return ProfileUpdateResult(success=True, item=updated)

    # ── 3. delete_item ──────────────────────────────────────────────────

    async def delete_item(
        self,
        tenant_id: str,
        user_id: str,
        slot: SlotType,
        item_key: str,
    ) -> bool:
        """硬删除档案条目。

        Args:
            tenant_id: 租户 ID。
            user_id: 用户 ID。
            slot: 槽位类型。
            item_key: 条目键。

        Returns:
            是否成功删除。
        """
        query = """
            DELETE FROM user_memory_profile
            WHERE tenant_id = $1 AND user_id = $2 AND slot = $3 AND item_key = $4
        """
        result = await self._pool.execute(
            query, tenant_id, user_id, slot.value, item_key
        )

        # 失效缓存
        await self._invalidate_cache(tenant_id, user_id)
        return "DELETE 1" in result

    # ── 4. archive_low_confidence ────────────────────────────────────────

    async def archive_low_confidence(self) -> int:
        """归档低置信度条目（180天未引用且低置信度）。

        删除满足以下条件的条目：
        - last_referenced_at < 180 天前
        - confidence < 50

        Returns:
            删除的条目数量。
        """
        threshold_timestamp = time.time() - (ARCHIVE_THRESHOLD_DAYS * 24 * 3600)

        query = """
            DELETE FROM user_memory_profile
            WHERE (last_referenced_at IS NULL OR last_referenced_at < $1)
              AND confidence < 50
              AND source != 'user_confirmed'
        """
        result = await self._pool.execute(query, threshold_timestamp)
        # 解析删除数量
        deleted_count = 0
        if "DELETE " in result:
            try:
                deleted_count = int(result.split(" ")[1])
            except (IndexError, ValueError):
                pass

        logger.info("Archived %d low-confidence profile entries", deleted_count)
        return deleted_count

    # ── 5. evict_over_limit ──────────────────────────────────────────────

    async def evict_over_limit(self, tenant_id: str | None = None) -> int:
        """软限淘汰（200 条目上限）。

        对超出 200 条目的用户，按 confidence × recency 排序淘汰最低条目。
        保留 user_confirmed 条目。

        Args:
            tenant_id: 可选，仅处理指定租户。

        Returns:
            淘汰的条目数量。
        """
        # 找出超出限制的用户
        overloaded_query = """
            SELECT tenant_id, user_id, COUNT(*) as cnt
            FROM user_memory_profile
            WHERE ($1::text IS NULL OR tenant_id = $1)
            AND source != 'user_confirmed'
            GROUP BY tenant_id, user_id
            HAVING COUNT(*) > $2
        """
        overloaded_users = await self._pool.fetch(
            overloaded_query, tenant_id, MAX_ITEMS_LIMIT
        )

        total_evicted = 0

        for user in overloaded_users:
            tid = user["tenant_id"]
            uid = user["user_id"]
            excess = user["cnt"] - MAX_ITEMS_LIMIT

            # 找出要淘汰的条目（按 confidence * recency 升序）
            evict_query = """
                DELETE FROM user_memory_profile
                WHERE tenant_id = $1 AND user_id = $2
                  AND source != 'user_confirmed'
                  AND ctid IN (
                    SELECT ctid FROM user_memory_profile
                    WHERE tenant_id = $1 AND user_id = $2
                      AND source != 'user_confirmed'
                    ORDER BY confidence *
                      CASE
                        WHEN last_referenced_at IS NULL THEN 0
                        ELSE EXTRACT(EPOCH FROM last_referenced_at)
                      END ASC
                    LIMIT $3
                  )
                RETURNING item_key
            """
            try:
                await self._pool.fetch(evict_query, tid, uid, excess)
                total_evicted += excess
                await self._invalidate_cache(tid, uid)
            except Exception as e:
                logger.error("Failed to evict entries for user %s: %s", uid, e)

        logger.info("Evicted %d profile entries over limit", total_evicted)
        return total_evicted

    # ── 辅助方法 ──────────────────────────────────────────────────────────

    async def _get_item(
        self,
        tenant_id: str,
        user_id: str,
        slot: SlotType,
        item_key: str,
    ) -> ProfileItem | None:
        """获取单个条目。"""
        slot_value = slot.value if isinstance(slot, SlotType) else slot
        query = """
            SELECT slot, item_key, item_value, confidence, source,
                   version, confirmed_at, last_referenced_at,
                   created_at, updated_at
            FROM user_memory_profile
            WHERE tenant_id = $1 AND user_id = $2
              AND slot = $3 AND item_key = $4
        """
        row = await self._pool.fetchrow(query, tenant_id, user_id, slot_value, item_key)
        if not row:
            return None
        return ProfileItem(
            slot=SlotType(row["slot"]),
            item_key=row["item_key"],
            item_value=row["item_value"],
            confidence=row["confidence"],
            source=SourceType(row["source"]),
            version=row["version"],
            confirmed_at=(
                row["confirmed_at"].timestamp() if row["confirmed_at"] else None
            ),
            last_referenced_at=(
                row["last_referenced_at"].timestamp()
                if row["last_referenced_at"]
                else None
            ),
            created_at=row["created_at"].timestamp(),
            updated_at=row["updated_at"].timestamp(),
        )

    async def _insert_item(
        self,
        tenant_id: str,
        user_id: str,
        slot: SlotType,
        item_key: str,
        item_value: Any,
        confidence: int,
        source: SourceType,
        now: float,
    ) -> None:
        """插入新条目。"""
        from datetime import datetime

        now_ts = datetime.fromtimestamp(now, tz=UTC)

        query = """
            INSERT INTO user_memory_profile
                (tenant_id, user_id, slot, item_key, item_value,
                 confidence, source, version, confirmed_at,
                 last_referenced_at, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 1,
                    CASE WHEN $7 = 'user_confirmed' THEN $8 ELSE NULL END,
                    NULL, $8, $8)
        """
        await self._pool.execute(
            query,
            tenant_id,
            user_id,
            slot.value if isinstance(slot, SlotType) else slot,
            item_key,
            (
                json.dumps(item_value)
                if not isinstance(item_value, (str, int, float, bool))
                else item_value
            ),
            confidence,
            source.value if isinstance(source, SourceType) else source,
            now_ts,
        )

    async def _update_item(
        self,
        tenant_id: str,
        user_id: str,
        slot: SlotType,
        item_key: str,
        item_value: Any,
        confidence: int,
        source: SourceType,
        new_version: int,
        now: float,
    ) -> None:
        """更新现有条目。"""
        from datetime import datetime

        now_ts = datetime.fromtimestamp(now, tz=UTC)

        query = """
            UPDATE user_memory_profile
            SET item_value = $5,
                confidence = $6,
                source = $7,
                version = $8,
                confirmed_at = CASE
                    WHEN $7 = 'user_confirmed' THEN $9
                    ELSE confirmed_at
                END,
                updated_at = $9
            WHERE tenant_id = $1 AND user_id = $2
              AND slot = $3 AND item_key = $4
        """
        await self._pool.execute(
            query,
            tenant_id,
            user_id,
            slot.value if isinstance(slot, SlotType) else slot,
            item_key,
            (
                json.dumps(item_value)
                if not isinstance(item_value, (str, int, float, bool))
                else item_value
            ),
            confidence,
            source.value if isinstance(source, SourceType) else source,
            new_version,
            now_ts,
        )

    async def _invalidate_cache(self, tenant_id: str, user_id: str) -> None:
        """失效缓存。"""
        if self._redis is not None:
            cache_key = self._cache_key(tenant_id, user_id)
            await self._redis.delete(cache_key)

    @staticmethod
    def _item_to_dict(item: ProfileItem) -> dict[str, Any]:
        """将 ProfileItem 转换为字典（用于缓存）。"""
        slot_val = item.slot.value if isinstance(item.slot, SlotType) else item.slot
        source_val = (
            item.source.value if isinstance(item.source, SourceType) else item.source
        )
        return {
            "slot": slot_val,
            "item_key": item.item_key,
            "item_value": item.item_value,
            "confidence": item.confidence,
            "source": source_val,
            "version": item.version,
            "confirmed_at": item.confirmed_at,
            "last_referenced_at": item.last_referenced_at,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
