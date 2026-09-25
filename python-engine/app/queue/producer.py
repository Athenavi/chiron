# 队列生产者 — 发布任务到 Redis Streams
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import redis.asyncio as aioredis

from app.observability.logging import trace_id_var
from app.redis_keys import rkey

logger = logging.getLogger(__name__)

# Stream 名称（统一键前缀：多环境共用 Redis 时隔离键空间，与 Go 网关 RedisKey 语义一致）
TASK_STREAM = rkey("engine:tasks")
DLQ_STREAM = rkey("engine:tasks:dlq")

# 流的保留上限。**生产者与重投/放回路径必须引用同一个常量。**
# Redis 的 `XADD ... MAXLEN` 是流级修剪、删掉的是最旧的条目，它不区分
# "刚写进去的"和"已经积压的"。重投路径若用了更小的值，一旦积压超过那个值，
# 一次重投就会连带删掉最老的待处理任务 —— 任务静默消失，且没有任何日志。
TASK_STREAM_MAXLEN = 100000
DLQ_STREAM_MAXLEN = 10000

# 按任务类型的默认截止时间(秒):既避免"堆积后执行早已无意义的旧任务",
# 也不能误杀长任务(知识库建索引可能数小时)。
DEFAULT_DEADLINE_SECONDS = {
    "rag_index": 6 * 3600,
    "embed_batch": 3600,
    "memory_save": 900,
    "tool_job": 3600,
    "workflow_run": 7200,
    # 子 Agent 完成后的「唤起父会话新一轮」信号：30 分钟内没送出去就没有意义了
    # （会话锁可能被上一个 turn 占着，靠重试退避）
    "agent_followup": 1800,
}
DEFAULT_DEADLINE_SECONDS_FALLBACK = 3600


class QueueProducer:
    """Redis Streams 任务发布者"""

    def __init__(self, redis: aioredis.Redis):
        self._redis = redis

    async def enqueue(
        self,
        task_type: str,
        tenant_id: str,
        payload: dict[str, Any],
        priority: int = 0,
        deadline_seconds: int | None = None,
        idempotency_key: str = "",
    ) -> str:
        """
        发布任务到 Redis Streams

        Args:
            task_type: "rag_index" | "memory_save" | "embed_batch"
            tenant_id: 租户 ID
            payload: 任务数据
            priority: 优先级（0=普通，1=高）
            deadline_seconds: 截止时间（秒）；None 时按 task_type 取默认值
            idempotency_key: 业务幂等键。**留空表示"只对同一条消息的重复投递幂等"**——
                本方法每次生成新 task_id，回退键 `{task_type}:{task_id}` 因此每次不同，
                覆盖的是重投/认领场景（消息字段原样重投）。只有"同一业务动作必须只执行
                一次"时才传稳定键（如 tool_job 用 job_id、workflow_run 用 instance_id）。
                注意 worker 的 claim 只拒绝 `completed`：传稳定键会让**用户再次主动发起
                的同一操作**被跳过，因此不可用于可重复发起的动作（如"重建索引"——
                该场景由 `_handle_rag_index` 的自身幂等负责）。

        Returns:
            task_id
        """
        task_id = uuid.uuid4().hex[:16]
        trace_id = trace_id_var.get("")

        # redis-py 的 xadd stub 把字段映射声明为 Dict[EncodableT, EncodableT]（EncodableT 是
        # bytes/str/int/float 的联合别名）。dict[str, str] 会因参数不变性被判不兼容 ——
        # 这里的值全是字符串，用 Any 标注即可（写入内容不变）。
        message: dict[Any, Any] = {
            "task_id": task_id,
            "task_type": task_type,
            "tenant_id": tenant_id,
            "payload": json.dumps(payload, ensure_ascii=False),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "retry_count": "0",
            "trace_id": trace_id,
            "priority": str(priority),
            # 幂等键(000.md 第 15 条):worker 执行前 claim,已完成的任务重投被直接丢弃。
            # 语义边界见方法 docstring(A6):传了业务键就要对"可重复性"负责。
            "idempotency_key": idempotency_key or f"{task_type}:{task_id}",
            # 截止时间:超时后 worker 不再执行(进 DLQ,不静默丢弃,见 worker.py)。
            # 未显式指定时按 task_type 取默认值——长任务与短任务不能同一档(A2)。
            "deadline": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(
                    time.time()
                    + (
                        deadline_seconds
                        if deadline_seconds is not None
                        else DEFAULT_DEADLINE_SECONDS.get(
                            task_type, DEFAULT_DEADLINE_SECONDS_FALLBACK
                        )
                    )
                ),
            ),
        }

        stream_id = await self._redis.xadd(TASK_STREAM, message, maxlen=TASK_STREAM_MAXLEN)
        logger.info(
            "Task enqueued: id=%s type=%s stream_id=%s", task_id, task_type, stream_id
        )
        return task_id

    async def get_depth(self) -> int:
        """查询队列深度"""
        info = await self._redis.xinfo_stream(TASK_STREAM)
        depth: int = info.get("length", 0)
        return depth
