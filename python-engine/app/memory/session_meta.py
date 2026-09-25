"""L1 SessionMeta Store - 会话元数据存储。

本模块实现 L1 层会话元数据的进程内存存储：
- create: 创建会话元数据
- get/update: 获取/更新会话元数据
- delete: 删除会话元数据
- 15 分钟空闲 TTL 自动清理
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.memory.layers import EntryChannel, SessionMeta

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────

IDLE_TTL = 15 * 60  # 15 分钟空闲 TTL


class SessionMetaStore:
    """L1 会话元数据存储（进程内存）。

    存储会话级别的簿记数据，与对话内容无关。
    支持 15 分钟空闲 TTL 自动清理。
    """

    def __init__(self) -> None:
        """初始化 SessionMetaStore。"""
        self._store: dict[str, SessionMeta] = {}
        self._cleanup_task: asyncio.Task[None] | None = None

    # ── CRUD 操作 ──────────────────────────────────────────────────────

    def create(
        self,
        session_id: str,
        tenant_id: str,
        user_id: str,
        entry_channel: EntryChannel = "web",
        mode: str = "agent",
    ) -> SessionMeta:
        """创建会话元数据。

        Args:
            session_id: 会话 ID。
            tenant_id: 租户 ID。
            user_id: 用户 ID。
            entry_channel: 入口渠道。
            mode: Agent 模式。

        Returns:
            新创建的 SessionMeta。

        Raises:
            ValueError: 如果 session_id 已存在。
        """
        if session_id in self._store:
            logger.warning("Session %s already exists, overwriting", session_id)

        now = time.time()
        meta = SessionMeta(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            entry_channel=entry_channel,
            mode=mode,
            started_at=now,
            last_active_at=now,
        )
        self._store[session_id] = meta
        logger.debug(
            "Created session meta for %s (user=%s, tenant=%s)",
            session_id,
            user_id,
            tenant_id,
        )
        return meta

    def get(self, session_id: str) -> SessionMeta | None:
        """获取会话元数据。

        Args:
            session_id: 会话 ID。

        Returns:
            SessionMeta 或 None（如果不存在或已过期）。
        """
        meta = self._store.get(session_id)
        if meta is None:
            return None

        # 检查是否过期
        if time.time() - meta.last_active_at > IDLE_TTL:
            logger.info(
                "Session %s expired (idle > %d min)", session_id, IDLE_TTL // 60
            )
            self.delete(session_id)
            return None

        return meta

    def update(self, session_id: str, **kwargs: Any) -> SessionMeta | None:
        """更新会话元数据字段。

        Args:
            session_id: 会话 ID。
            **kwargs: 要更新的字段和值。

        Returns:
            更新后的 SessionMeta 或 None（如果不存在）。
        """
        meta = self._store.get(session_id)
        if meta is None:
            logger.warning("Session %s not found for update", session_id)
            return None

        for key, value in kwargs.items():
            if hasattr(meta, key):
                setattr(meta, key, value)
            else:
                logger.warning("SessionMeta has no attribute '%s', skipping", key)

        meta.last_active_at = time.time()
        return meta

    def delete(self, session_id: str) -> bool:
        """删除会话元数据。

        Args:
            session_id: 会话 ID。

        Returns:
            是否成功删除。
        """
        if session_id in self._store:
            del self._store[session_id]
            logger.debug("Deleted session meta for %s", session_id)
            return True
        return False

    # ── 批量操作 ──────────────────────────────────────────────────────

    def get_all(self) -> list[SessionMeta]:
        """获取所有活跃会话的元数据。"""
        self._cleanup_expired()
        return list(self._store.values())

    def count(self) -> int:
        """获取活跃会话数量。"""
        self._cleanup_expired()
        return len(self._store)

    # ── TTL 清理 ──────────────────────────────────────────────────────

    def _cleanup_expired(self) -> int:
        """清理所有过期的会话。

        Returns:
            清理的会话数量。
        """
        now = time.time()
        expired = [
            sid
            for sid, meta in self._store.items()
            if now - meta.last_active_at > IDLE_TTL
        ]
        for sid in expired:
            logger.debug("Cleaning up expired session %s", sid)
            del self._store[sid]

        if expired:
            logger.info("Cleaned up %d expired sessions", len(expired))
        return len(expired)

    async def start_periodic_cleanup(self, interval: int = 60) -> None:
        """启动定期清理任务。

        Args:
            interval: 清理间隔（秒）。
        """
        if self._cleanup_task is not None and not self._cleanup_task.done():
            logger.warning("Periodic cleanup task already running")
            return

        async def _cleanup_loop() -> None:
            logger.info("Starting periodic cleanup (every %d seconds)", interval)
            try:
                while True:
                    await asyncio.sleep(interval)
                    self._cleanup_expired()
            except asyncio.CancelledError:
                logger.info("Periodic cleanup task cancelled")
            except Exception as e:
                logger.error("Periodic cleanup task failed: %s", e)

        self._cleanup_task = asyncio.create_task(_cleanup_loop())

    async def stop_periodic_cleanup(self) -> None:
        """停止定期清理任务。"""
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
            logger.info("Periodic cleanup task stopped")

    def clear(self) -> None:
        """清空所有会话（用于测试或关闭时）。"""
        self._store.clear()
        logger.info("Session meta store cleared")
