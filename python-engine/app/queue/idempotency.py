"""任务幂等闸门（PG 唯一键）。

背景：Redis Streams 消费组是 at-least-once——worker 执行成功但 ACK 前崩溃/被杀，
消息会由本实例或其它实例的 reclaim 再次投递。缺少幂等约束时，有副作用的任务
（workflow_run / tool_job / rag_index 等）会被重复执行：重复扣费、重复写文件、
重复调用外部 API。

策略：
- 唯一键为 `idempotency_key`（消息字段 `idempotency_key`，缺省回退
  `{task_type}:{task_id}`）；
- 执行前 ``claim()``：首次插入 running；已存在且非 completed 时递增 attempt 并放行
  （覆盖崩溃恢复与显式重试）；已 completed 时返回 False，调用方直接 ACK 丢弃重复消息；
- 执行成功 ``complete()``，进入重试/进死信前 ``fail()``；
- PG 不可用/未初始化时 fail-open（放行执行）并告警——幂等是加固，不应因数据库
  抖动让任务无法执行。

表由权威迁移 migrations/versions/0001_authoritative_baseline.py 创建。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_CLAIM_SQL = """
INSERT INTO task_idempotency
    (idempotency_key, task_id, task_type, tenant_id, status, attempt, created_at, updated_at)
VALUES ($1, $2, $3, $4, 'running', 1, now(), now())
ON CONFLICT (idempotency_key) DO UPDATE
    SET attempt = task_idempotency.attempt + 1,
        status = 'running',
        updated_at = now()
    WHERE task_idempotency.status <> 'completed'
RETURNING idempotency_key
"""

_COMPLETE_SQL = """
UPDATE task_idempotency
SET status = 'completed', result_ref = $2, updated_at = now()
WHERE idempotency_key = $1
"""

_FAIL_SQL = """
UPDATE task_idempotency
SET status = 'failed', updated_at = now()
WHERE idempotency_key = $1
"""


def key_for(task_type: str, task_id: str, explicit: str = "") -> str:
    """幂等键：消息显式提供优先，否则回退 `{task_type}:{task_id}`。"""
    if explicit:
        return explicit
    return f"{task_type}:{task_id}"


def _pool() -> Any:
    """取 PG 池；不可用时抛异常（调用方 fail-open）。"""
    from app.db import get_pool

    return get_pool()


async def claim(key: str, task_id: str, task_type: str, tenant_id: str) -> bool:
    """抢占执行权。True=应执行；False=该键已 completed，调用方应跳过并 ACK。"""
    try:
        pool = _pool()
    except Exception as e:  # noqa: BLE001 - 池未初始化/已关闭
        logger.warning("idempotency disabled (db pool unavailable): %s", e)
        return True
    try:
        row = await pool.fetchrow(_CLAIM_SQL, key, task_id, task_type, tenant_id)
    except Exception as e:  # noqa: BLE001 - 表缺失/连接抖动：放行执行
        logger.warning("idempotency claim failed (fail-open) key=%s: %s", key, e)
        return True
    if row is None:
        logger.info("duplicate task skipped (already completed): key=%s", key)
        return False
    return True


async def complete(key: str, result_ref: str = "") -> None:
    """标记任务成功（幂等键终态）。"""
    await _update(_COMPLETE_SQL, key, result_ref)


async def fail(key: str) -> None:
    """标记任务失败（仍可重试：claim 只拒绝 completed）。"""
    await _update(_FAIL_SQL, key)


_PURGE_SQL = """
DELETE FROM task_idempotency
WHERE status <> 'running'
  AND updated_at < NOW() - make_interval(days => $1)
"""


async def purge_older_than(days: int) -> int:
    """删除保留期外已完结（completed/failed）的幂等记录，返回删除行数。

    running 不删：可能仍在执行或等待重试（claim 只拒绝 completed）。
    A7/C1：该表每任务一行，长期运行必须清理，否则无限膨胀。
    """
    if days <= 0:
        return 0
    try:
        pool = _pool()
    except Exception:  # noqa: BLE001 - 无池（PG 未启用）时跳过
        return 0
    try:
        res = await pool.execute(_PURGE_SQL, days)
    except Exception as e:  # noqa: BLE001 - 清理失败留待下一轮
        logger.warning("idempotency purge failed: %s", e)
        return 0
    # asyncpg / 统一客户端均返回 "DELETE <n>"
    try:
        return int(str(res).split()[-1])
    except (ValueError, IndexError):
        return 0


async def _update(sql: str, *args: Any) -> None:
    try:
        pool = _pool()
    except Exception:  # noqa: BLE001 - 无池时无需记录状态
        return
    try:
        await pool.execute(sql, *args)
    except Exception as e:  # noqa: BLE001 - 状态更新失败不改变任务结果
        logger.warning("idempotency update failed: %s", e)
