"""记忆冲突审计落库 —— 追加式流水，**不参与**裁决列表读取。

C4 的数据分层是「一活一史」：

- **Redis 是活数据**：待裁决冲突的登记 / 裁决 / 删除都以它为准，裁决列表
  （``MemoryService.list_conflicts_shared``）也只读 Redis。因此多副本天然一致，
  key 带 tenant 不串租户，带 TTL 过期即自动清理，不需要额外回收任务。
- **PostgreSQL 只落审计**：谁、在什么时候、对哪条冲突做了什么。它是历史账本，
  不是查询入口 —— 一旦让 PG 成为裁决列表的来源，就会把「活数据」和「历史记录」
  两套一致性要求绑死（Redis 里的待办过期清理了、PG 还留着，列表到底信谁？）。

所以本模块只负责「把事件 append 进 ``audit_logs``」，且写入一律 fail-soft：
审计是旁路，少一条流水不该让用户的裁决动作失败。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# 事件名：作为 audit_logs.action 的后缀（前缀固定 memory.conflict）。
EVENT_DETECTED = "detected"  # 冲突被登记（进入待裁决列表）
EVENT_RESOLVED = "resolved"  # 用户裁决（keep_old / use_new / manual）
EVENT_DISMISSED = "dismissed"  # 用户否认、不改变记忆内容

# audit_logs.resource_type 取值，便于按「冲突」这一资源类型检索流水。
RESOURCE_TYPE = "memory_conflict"


@runtime_checkable
class ConflictAuditor(Protocol):
    """冲突审计落库契约：append 一条事件流水。

    实现方只需保证「尽力写入」，无需返回值 —— 审计失败由实现方自行吞掉，
    不应向调用方抛出（``ConflictManager`` 也会再兜一层，防自定义实现失手）。
    """

    async def record(self, event: str, conflict: dict[str, Any]) -> None: ...


class NullConflictAuditor:
    """默认审计器：什么都不做。

    ``ConflictManager`` 未注入审计器时用它占位，省去调用点的 None 判断，
    也让「审计可缺省」成为显式的默认行为而非隐式的空分支。
    """

    async def record(self, event: str, conflict: dict[str, Any]) -> None:
        return None


class PgConflictAuditor:
    """把冲突事件写进 ``audit_logs`` 表（只追加，不复用 pending 记录）。

    ``pool`` 只需实现 ``execute(query, *args)`` —— asyncpg.Pool 与 ``app.db`` 的
    unified 包装器都满足。单位是「一条事件一行」：即便 Redis 里的待裁决项已经
    过期清理，历史审计仍在，供事后追责 / 对账。
    """

    def __init__(self, pool: Any, *, clock: Any = None):
        """初始化审计器。

        Args:
            pool: 具备 ``execute(query, *args)`` 的数据库连接池。
            clock: 可选时钟，返回 ``datetime``；缺省用当前 UTC（仅测试可复现用）。
        """
        self._pool = pool
        self._clock = clock or _utcnow_naive

    async def record(self, event: str, conflict: dict[str, Any]) -> None:
        """写一条冲突审计流水。

        ``audit_logs.created_at`` 是 ``timestamp without time zone``，故传 naive
        UTC；用 ``json.dumps(..., default=str)`` 兜住非原生可序列化的值（如
        ``SlotType``），避免一条审计把整次裁决动作带崩。
        """
        try:
            await self._pool.execute(
                """
                INSERT INTO audit_logs
                    (id, tenant_id, user_id, action, resource_type,
                     resource_id, details, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                str(uuid.uuid4()),
                conflict.get("tenant_id"),
                conflict.get("user_id"),
                f"memory.conflict.{event}",
                RESOURCE_TYPE,
                conflict.get("conflict_id"),
                json.dumps(conflict, ensure_ascii=False, default=str),
                self._clock(),
            )
        except Exception as e:  # noqa: BLE001
            # 审计是旁路：任何写库失败都只记 warning，绝不向调用方抛出 ——
            # 用户裁决动作的成功与否，不该取决于审计表是否可写。
            logger.warning(
                "Failed to audit memory conflict %s (%s): %s",
                conflict.get("conflict_id"),
                event,
                e,
            )


def _utcnow_naive() -> datetime:
    """当前 UTC 时间，去掉 tzinfo 以对齐 ``timestamp without time zone`` 列。"""
    return datetime.now(UTC).replace(tzinfo=None)
