# 队列消费者 — Redis Streams Consumer Group
# P1-2: 信号处理与优雅关闭 (生产安全检查 2026-08-17)
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import signal
import time

import redis.asyncio as aioredis

from app.observability.metrics import (QUEUE_DEPTH, QUEUE_DLQ_TOTAL,
                                       QUEUE_PROCESSING_DURATION,
                                       QUEUE_RETRY_TOTAL)
from app.redis_keys import rkey

logger = logging.getLogger(__name__)

# 流名与保留上限从 producer 统一取得。此前本文件自己又定义了一份流名，
# 且重投/放回用了比生产者更小的 maxlen（10000 vs 100000）—— 积压超过 1 万条时，
# 一次重投就会静默修剪掉最老的待处理任务。
from app.queue.producer import (  # noqa: E402
    DLQ_STREAM,
    DLQ_STREAM_MAXLEN,
    TASK_STREAM,
    TASK_STREAM_MAXLEN,
)
GROUP_NAME = "engine-workers"
CONSUMER_PREFIX = "worker"
MAX_RETRIES = 3

# ── 跨实例全局并发门控(N5,防多引擎实例后台 10×N 洪峰)──
# Redis 计数键 engine:worker:inflight;TTL 防进程崩溃泄漏;0=关闭。
GATE_KEY = rkey("engine:worker:inflight")
GATE_TTL = 300
GATE_WAIT_SECS = 3.0
GATE_RETRY_INTERVAL = 0.5

# ── PEL 崩溃恢复（XCLAIM 认领）─────────────────────────────────────────────
# worker 崩溃后其未 ACK 消息停留在消费组 PEL；本实例周期认领 idle 超阈值的
# pending 消息重投（workflow_run 依 checkpoint 幂等续跑；tool_job 重执行）。
# 处理中的消息由 _heartbeat_lease 周期刷新 idle（lease），所以阈值不必再大于
# 单任务处理上限——只有真正失联（崩溃/被杀/网络分区）的消息才会 idle 超时。
# 原值 (3600+180)s 使崩溃任务最长滞留约 1 小时才被恢复；现默认 600s。
CLAIM_MIN_IDLE_SECS = int(os.getenv("QUEUE_CLAIM_MIN_IDLE_SECS", "600"))
CLAIM_MIN_IDLE_MS = CLAIM_MIN_IDLE_SECS * 1000
# lease 心跳周期：阈值 1/3（下限 30s），保证阈值窗口内至少刷新两次。
HEARTBEAT_SECS = max(30, CLAIM_MIN_IDLE_SECS // 3)
CLAIM_LOOP_SECS = 60
CLAIM_BATCH = 50

_GATE_ACQUIRE_LUA = """
local v = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[1])
if v > tonumber(ARGV[2]) then
  redis.call('DECR', KEYS[1])
  return 0
end
return 1
"""

_GATE_RELEASE_LUA = """
local v = redis.call('GET', KEYS[1])
if v and tonumber(v) > 0 then
  return redis.call('DECR', KEYS[1])
end
return 0
"""


# ── 延迟重试（P2-3）─────────────────────────────────────────────────────────
#
# 为什么需要：`agent_followup` 最常见的失败不是"任务本身有问题"，而是**父会话此刻正忙**
# （会话运行锁 409）。原来的重试是"立刻放回队尾"——3 次重试预算在毫秒级耗尽，随后进 DLQ，
# 而需要等的条件通常要几十秒甚至几分钟才释放。结果就是：子 Agent 的结论明明已经落库，
# 却因为一次锁竞争再也回不到主对话。
# 这里给"等一会儿再试"一条独立通路：延迟集合（ZSET，score=可执行时间），
# 到点由 `_promote_delayed` 搬回主队列，**不消耗重试预算**（它没有失败），
# 由 deadline 与 delay 次数上限兜底，超限则进 DLQ（可见、可人工重投）。
DELAYED_ZSET = rkey("engine:tasks:delayed")
DELAY_LOOP_SECS = float(os.getenv("QUEUE_DELAY_LOOP_SECS", "15"))
DELAY_BASE_SECONDS = float(os.getenv("QUEUE_DELAY_BASE_SECONDS", "20"))
DELAY_MAX_SECONDS = float(os.getenv("QUEUE_DELAY_MAX_SECONDS", "300"))
DELAY_MAX_ATTEMPTS = int(os.getenv("QUEUE_DELAY_MAX_ATTEMPTS", "12"))
DELAY_BATCH = 50


class RetryLaterError(RuntimeError):
    """"现在不行，等一会儿再来"——**不是失败**。

    抛出它的 handler 表示：条件（会话锁、并发配额、上游暂时不可达）会自行好转，
    应当延迟重投而不是消耗重试预算、更不该进 DLQ。
    """

    def __init__(self, message: str, delay_seconds: float = 0.0) -> None:
        super().__init__(message)
        #: 退避基值（秒）。0 表示用全局默认基值；上游给了 Retry-After 时用它。
        self.delay_seconds = float(delay_seconds or 0)


def _as_int(value, default: int = 0) -> int:
    """把消息字段（bytes / str / int / None）解析成 int，解析不了回落默认值。

    队列消息来自 Redis，字段可能是 bytes、str 或干脆缺失；直接 ``int()`` 会抛异常，
    而那等于把"一条字段脏掉的消息"变成"延迟重试逻辑本身崩溃"。
    """
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode()
        except Exception:  # noqa: BLE001 - 解不出来就按缺失处理
            return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _retry_after(resp) -> float:
    """读上游的 ``Retry-After``（秒）。解析不了就返回 0（用默认退避基值）。

    网关目前不发这个头，但读它是为了让"上游明确告诉我们等多久"时不必猜。
    """
    raw = ""
    try:
        raw = str(resp.headers.get("retry-after") or "").strip()
    except Exception:  # noqa: BLE001 - 头不可读时按默认退避处理
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


class QueueWorker:
    """
    Redis Streams 消费者

    生命周期:
      - 启动: XREADGROUP BLOCK 5s
      - 处理: ACK 成功 / NACK 失败
      - 重试: NACK 后超时 30s 可被 XCLAIM
      - 死信: retry_count >= 3 → XADD 到 DLQ
      - 关闭: 停止消费 → 等待 in-flight → 退出
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        concurrency: int = 10,
        gateway=None,
        memory_service=None,
        global_concurrency: int = 10,
    ):
        self._redis = redis
        self._concurrency = concurrency
        self._gate_limit = global_concurrency
        self._gateway = gateway  # GatewayRouter（用于 RAG 构建/嵌入），可为 None
        self._memory_service = (
            memory_service  # MemoryService（用于记忆存储），可为 None
        )
        self._running = False
        self._semaphore = asyncio.Semaphore(concurrency)
        self._in_flight: set[asyncio.Task] = set()
        self._consumer_name = f"{CONSUMER_PREFIX}-{id(self):x}"
        self._reclaim_task: asyncio.Task | None = None
        self._delay_task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动消费者"""
        self._running = True

        # P1-2: 注册信号处理器（支持 SIGTERM/Kill -15）
        await self._register_signal_handlers()

        # 确保 Consumer Group 存在
        try:
            await self._redis.xgroup_create(
                TASK_STREAM, GROUP_NAME, id="0", mkstream=True
            )
            logger.info("Consumer group '%s' created", GROUP_NAME)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
            logger.debug("Consumer group '%s' already exists", GROUP_NAME)

        logger.info(
            "Queue worker started: consumer=%s concurrency=%d",
            self._consumer_name,
            self._concurrency,
        )

        # PEL 崩溃恢复：后台认领循环（worker 崩溃后由其它实例接管其 pending 任务）
        self._reclaim_task = asyncio.create_task(self._reclaim_loop())
        logger.info("Queue worker reclaim loop started (idle=%ds)", CLAIM_MIN_IDLE_MS // 1000)

        # 延迟重试：把到期的"等一会儿再来"任务搬回主队列
        self._delay_task = asyncio.create_task(self._delay_loop())
        logger.info("Queue worker delayed-retry loop started (every %.0fs)", DELAY_LOOP_SECS)

        while self._running:
            try:
                await self._consume_batch()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Queue worker error: %s", e)
                await asyncio.sleep(1)

    async def stop(self) -> None:
        """P1-2: 优雅停止（带超时保护）"""
        self._running = False
        if self._reclaim_task is not None:
            self._reclaim_task.cancel()
            self._reclaim_task = None
        if self._delay_task is not None:
            self._delay_task.cancel()
            self._delay_task = None
        logger.info(
            "Queue worker stopping, waiting for %d in-flight tasks...",
            len(self._in_flight),
        )
        if self._in_flight:
            # P1-2: 设置超时，防止 task 永久挂起
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._in_flight, return_exceptions=True),
                    timeout=30.0,  # 最多等待 30 秒
                )
            except asyncio.TimeoutError:
                logger.error(
                    "Queue worker shutdown timed out, cancelling remaining tasks"
                )
                for task in self._in_flight:
                    task.cancel()
        logger.info("Queue worker stopped")

    async def _register_signal_handlers(self) -> None:
        """P1-2: 注册 UNIX 信号处理器（Kubernetes Docker Pod 兼容）"""
        loop = asyncio.get_event_loop()

        def _signal_handler():
            """信号回调：触发异步停止"""
            asyncio.create_task(self.stop())

        try:
            loop.add_signal_handler(signal.SIGTERM, _signal_handler)
            loop.add_signal_handler(signal.SIGINT, _signal_handler)
            logger.info("Registered signal handlers for SIGTERM/SIGINT")
        except NotImplementedError:
            # Windows 不支持 add_signal_handler
            logger.debug("Signal handling not supported on this platform")

    async def _consume_batch(self) -> None:
        """批量消费一批消息"""
        try:
            results = await self._redis.xreadgroup(
                GROUP_NAME,
                self._consumer_name,
                {TASK_STREAM: ">"},
                count=self._concurrency,
                block=5000,  # 5 秒超时
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("XREADGROUP error: %s", e)
            return

        if not results:
            return

        for stream, messages in results:
            for stream_id, fields in messages:
                await self._spawn_message(stream_id, fields)

    async def _spawn_message(self, stream_id: str, fields: dict) -> None:
        """把一条消息投入本地处理：本地信号量 + 跨实例全局门控 + 超时保护。"""
        # 等待本地信号量（每实例）
        await self._semaphore.acquire()
        # 跨实例全局门控：超限等待上限后把消息放回队尾并 ACK（避免认领窗口重复处理）
        if not await self._try_acquire_gate(stream_id, fields):
            self._semaphore.release()
            return
        task = asyncio.create_task(self._process_message(stream_id, fields))
        # 设置超时保护，防止 task 永久挂起
        timeout_task = asyncio.create_task(asyncio.wait_for(task, timeout=3600))
        self._in_flight.add(timeout_task)
        timeout_task.add_done_callback(self._task_done)
        # lease 心跳：处理期间周期刷新该消息的 PEL idle，使其它实例的 reclaim
        # 不会把仍在执行的长任务当作崩溃残留抢走（阈值因此可下调到分钟级）。
        lease_task = asyncio.create_task(self._heartbeat_lease(stream_id, timeout_task))
        timeout_task.add_done_callback(lambda _t: lease_task.cancel())

    async def _heartbeat_lease(self, stream_id: str, owner: asyncio.Task) -> None:
        """处理期间刷新 PEL idle（XCLAIM ... JUSTID），充当任务 lease 心跳。"""
        while not owner.done():
            try:
                await asyncio.sleep(HEARTBEAT_SECS)
            except asyncio.CancelledError:
                return
            if owner.done():
                return
            try:
                await self._redis.xclaim(
                    TASK_STREAM,
                    GROUP_NAME,
                    self._consumer_name,
                    0,
                    [stream_id],
                    justid=True,
                )
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001 - 心跳失败不应打断任务本身
                logger.debug("worker lease heartbeat failed for %s: %s", stream_id, e)

    async def _reclaim_loop(self) -> None:
        """PEL 崩溃恢复认领循环：周期接管 idle 超阈值的 pending 消息。"""
        while self._running:
            try:
                await asyncio.sleep(CLAIM_LOOP_SECS)
                await self._reclaim_pending()
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("worker reclaim loop error: %s", e)

    async def _reclaim_pending(self) -> None:
        """认领本组内 idle 超过 CLAIM_MIN_IDLE_MS 的 pending 消息并重新处理。

        覆盖场景：worker 崩溃/被杀后其未 ACK 消息滞留 PEL；其它实例接管后
        workflow_run 依 DB checkpoint 幂等续跑、tool_job 重新执行（retry_count 递增，
        超过 MAX_RETRIES 进 DLQ）。正在其它实例执行的长任务（idle < 阈值）不会被抢。
        """
        try:
            pend = await self._redis.xpending_range(
                TASK_STREAM,
                GROUP_NAME,
                min="-",
                max="+",
                count=CLAIM_BATCH,
                idle=CLAIM_MIN_IDLE_MS,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("worker pending scan failed: %s", e)
            return
        if not pend:
            return
        ids = []
        for p in pend:
            mid = p.get("message_id") if isinstance(p, dict) else p.message_id
            if mid:
                ids.append(mid)
        if not ids:
            return
        try:
            claimed = await self._redis.xclaim(
                TASK_STREAM, GROUP_NAME, self._consumer_name, CLAIM_MIN_IDLE_MS, ids
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("worker xclaim failed: %s", e)
            return
        for item in claimed:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                await self._spawn_claimed(item[0], item[1])

    async def _spawn_claimed(self, msg_id: str, fields: dict) -> None:
        """认领到的消息：retry_count 递增后重新进入处理；超限进 DLQ。"""
        raw = fields.get(b"retry_count", fields.get("retry_count", 0))
        try:
            retry = int(raw) + 1
        except (TypeError, ValueError):
            retry = 1
        fields[b"retry_count"] = str(retry).encode()
        if retry > MAX_RETRIES:
            raw_type = fields.get(b"task_type", fields.get("task_type", ""))
            task_type = raw_type.decode() if isinstance(raw_type, bytes) else str(raw_type or "")
            try:
                await self._redis.xadd(DLQ_STREAM, fields, maxlen=DLQ_STREAM_MAXLEN)
                await self._redis.xack(TASK_STREAM, GROUP_NAME, msg_id)
                # 必须带 task_type 标签：QUEUE_DLQ_TOTAL 定义了该标签，无标签调用会抛
                # ValueError，被下面的 except 吞掉 —— 结果是"进 DLQ"这件事在指标里
                # 完全不可见（这正是"要么可见地失败"要消灭的形态）。
                QUEUE_DLQ_TOTAL.labels(task_type=task_type).inc()
                logger.warning("reclaimed message exceeded retries, moved to DLQ: %s", msg_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("reclaimed DLQ move failed: %s", e)
            return
        await self._spawn_message(msg_id, fields)

    async def _try_acquire_gate(self, stream_id: str, fields: dict) -> bool:
        """全局并发门控：抢到槽位返回 True；等待 GATE_WAIT_SECS 仍满则放回队尾返回 False。
        Redis 故障/未配置：fail-open（返回 True，交由后续调用报错/本地并发约束）。"""
        if not self._gate_limit or self._redis is None:
            return True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + GATE_WAIT_SECS
        while True:
            try:
                ok = await self._redis.eval(
                    _GATE_ACQUIRE_LUA, 1, GATE_KEY, GATE_TTL, self._gate_limit
                )
            except Exception:
                return True  # fail-open
            if ok:
                return True
            if loop.time() >= deadline:
                # 放回队尾（保持顺序）并 ACK 原消息
                requeue = {
                    (k.decode() if isinstance(k, bytes) else k): (
                        v.decode() if isinstance(v, bytes) else v
                    )
                    for k, v in fields.items()
                }
                try:
                    await self._redis.xadd(TASK_STREAM, requeue, maxlen=TASK_STREAM_MAXLEN)
                    await self._redis.xack(TASK_STREAM, GROUP_NAME, stream_id)
                except Exception as exc:
                    logger.warning("worker gate requeue failed: %s", exc)
                    return True  # 放回失败：宁可处理也不丢
                task_id = requeue.get("task_id", "")
                logger.info(
                    "worker global concurrency full; message requeued id=%s",
                    task_id,
                )
                return False
            await asyncio.sleep(GATE_RETRY_INTERVAL)

    async def _release_gate(self) -> None:
        if not self._gate_limit or self._redis is None:
            return
        try:
            await self._redis.eval(_GATE_RELEASE_LUA, 1, GATE_KEY)
        except Exception:
            pass  # 计数键由 TTL 兜底

    def _task_done(self, task: asyncio.Task) -> None:
        self._in_flight.discard(task)
        self._semaphore.release()
        if self._gate_limit and self._redis:
            asyncio.create_task(self._release_gate())
        if task.exception():
            logger.error("Task exception: %s", task.exception())

    async def _process_message(self, stream_id: str, fields: dict) -> None:
        """处理单条消息"""
        task_type = (
            fields.get(b"task_type", b"").decode()
            if isinstance(fields.get(b"task_type"), bytes)
            else fields.get("task_type", "")
        )
        task_id = (
            fields.get(b"task_id", b"").decode()
            if isinstance(fields.get(b"task_id"), bytes)
            else fields.get("task_id", "")
        )
        payload_raw = (
            fields.get(b"payload", b"{}").decode()
            if isinstance(fields.get(b"payload"), bytes)
            else fields.get("payload", "{}")
        )
        retry_count = int(
            fields.get(b"retry_count", 0)
            if isinstance(fields.get(b"retry_count"), bytes)
            else fields.get("retry_count", 0)
        )
        tenant_id = (
            fields.get(b"tenant_id", b"").decode()
            if isinstance(fields.get(b"tenant_id"), bytes)
            else fields.get("tenant_id", "")
        )
        # 幂等键：消息显式提供优先，否则回退 {task_type}:{task_id}
        from app.queue import idempotency

        idem_raw = fields.get(b"idempotency_key", fields.get("idempotency_key", ""))
        idem_key = idempotency.key_for(
            task_type,
            task_id,
            idem_raw.decode() if isinstance(idem_raw, bytes) else (idem_raw or ""),
        )

        # 过期任务直接 ACK 丢弃:deadline 由投递方写入(RFC3339 UTC,见 producer/jobs/workflows)。
        # 缺省/解析失败视为无期限,保持既有行为。
        deadline_raw = (
            fields.get(b"deadline", b"").decode()
            if isinstance(fields.get(b"deadline"), bytes)
            else fields.get("deadline", "")
        )
        if deadline_raw:
            try:
                dl = datetime.datetime.strptime(
                    deadline_raw, "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=datetime.timezone.utc)
            except ValueError:
                dl = None
            if dl is not None and dl < datetime.datetime.now(datetime.timezone.utc):
                # 过期任务不再执行,但也不能静默丢弃(A3):转 DLQ 保留可见性与人工重投能力。
                logger.warning(
                    "Task expired (deadline=%s), moved to DLQ: id=%s type=%s",
                    deadline_raw,
                    task_id,
                    task_type,
                )
                try:
                    await self._redis.xadd(
                        DLQ_STREAM,
                        {
                            "task_id": task_id,
                            "task_type": task_type,
                            "payload": payload_raw,
                            "error": f"deadline exceeded: {deadline_raw}",
                            "retry_count": str(retry_count),
                        },
                        maxlen=DLQ_STREAM_MAXLEN,
                    )
                    QUEUE_DLQ_TOTAL.labels(task_type=task_type).inc()
                except Exception as exc:  # noqa: BLE001 - DLQ 写入失败也必须 ACK，避免无限重投
                    logger.warning("expired DLQ move failed: %s", exc)
                await self._redis.xack(TASK_STREAM, GROUP_NAME, stream_id)
                return

        start = time.monotonic()
        try:
            payload = json.loads(payload_raw)
            # 幂等闸门：该键已 completed 说明是重复投递（执行后 ACK 前崩溃被 reclaim），
            # 直接 ACK 丢弃，避免 workflow_run/tool_job 等副作用任务重复执行。
            if not await idempotency.claim(idem_key, task_id, task_type, tenant_id):
                await self._redis.xack(TASK_STREAM, GROUP_NAME, stream_id)
                return
            await self._dispatch(task_type, payload, tenant_id)
            await idempotency.complete(idem_key)

            # 成功 → ACK
            await self._redis.xack(TASK_STREAM, GROUP_NAME, stream_id)
            elapsed = time.monotonic() - start
            QUEUE_PROCESSING_DURATION.labels(task_type=task_type).observe(elapsed)
            logger.info(
                "Task completed: id=%s type=%s (%.2fs)", task_id, task_type, elapsed
            )

        except RetryLaterError as e:
            # "等一会儿再试"：条件（会话锁 / 并发配额 / 上游暂时不可达）会自行好转。
            # 既不是失败，就不能走下面的"消耗重试预算"路径 —— 立即重投的预算会在
            # 毫秒级耗尽并进 DLQ，而这里要等的条件通常要几十秒才释放。
            logger.warning(
                "Task deferred: id=%s type=%s reason=%s", task_id, task_type, str(e)[:200]
            )
            await idempotency.fail(idem_key)
            await self._defer_message(stream_id, fields, task_id, task_type, payload_raw, e)

        except Exception as e:
            logger.error("Task failed: id=%s type=%s error=%s", task_id, task_type, e)
            # 标记失败：幂等键仍可重试（claim 只拒绝 completed）
            await idempotency.fail(idem_key)

            if retry_count >= MAX_RETRIES:
                # 移入死信队列
                await self._move_to_dlq(task_id, task_type, payload_raw, str(e), retry_count)
                await self._redis.xack(TASK_STREAM, GROUP_NAME, stream_id)
                logger.warning(
                    "Task moved to DLQ: id=%s (retries=%d)", task_id, retry_count
                )
            else:
                # 重试：重新投递消息并递增 retry_count（保留租户标识以保持观测链路）
                retry_count += 1
                retry_msg = {
                    "task_type": task_type,
                    "task_id": task_id,
                    "payload": payload_raw,
                    "retry_count": str(retry_count),
                }
                if tenant_id:
                    retry_msg["tenant_id"] = tenant_id
                await self._redis.xadd(TASK_STREAM, retry_msg, maxlen=TASK_STREAM_MAXLEN)
                await self._redis.xack(TASK_STREAM, GROUP_NAME, stream_id)
                QUEUE_RETRY_TOTAL.labels(task_type=task_type).inc()
                logger.info(
                    "Task re-queued for retry: id=%s (retry=%d/%d)",
                    task_id,
                    retry_count,
                    MAX_RETRIES,
                )

    # ── 延迟重试与死信 ──

    async def _move_to_dlq(
        self, task_id: str, task_type: str, payload_raw: str, error: str, retry_count: int | str
    ) -> None:
        """把任务移进死信队列（可见、可人工重投；绝不静默丢弃）。"""
        await self._redis.xadd(
            DLQ_STREAM,
            {
                "task_id": task_id,
                "task_type": task_type,
                "payload": payload_raw,
                "error": error,
                "retry_count": str(retry_count),
            },
        )
        QUEUE_DLQ_TOTAL.labels(task_type=task_type).inc()

    @staticmethod
    def _defer_delay(attempts: int, exc: RetryLaterError) -> float:
        """退避时长：指数增长，上限 ``DELAY_MAX_SECONDS``。

        上游给了明确等待时长（如 Retry-After）时以它作基值，否则用
        ``DELAY_BASE_SECONDS``。会话锁的典型持有时长是"一个 turn"（数十秒到数分钟），
        所以基值不能太小，否则会白跑很多轮。
        """
        base = exc.delay_seconds or DELAY_BASE_SECONDS
        return float(min(base * (2 ** max(0, attempts - 1)), DELAY_MAX_SECONDS))

    async def _defer_message(
        self,
        stream_id: str,
        fields: dict,
        task_id: str,
        task_type: str,
        payload_raw: str,
        exc: RetryLaterError,
    ) -> None:
        """把"等一会儿再试"的任务放进延迟集合，稍后由 :meth:`_promote_delayed` 搬回主队列。

        刻意**不递增** ``retry_count``：延迟重试不是失败，消耗失败预算会让一个
        "本来只是要等"的任务在几次之后就进 DLQ（那正是修复前的行为）。
        边界改由两处兜底：消息自带的 ``deadline``（到期进 DLQ）与 ``DELAY_MAX_ATTEMPTS``。
        """
        message = {
            (k.decode() if isinstance(k, bytes) else k): (
                v.decode() if isinstance(v, bytes) else v
            )
            for k, v in fields.items()
        }
        try:
            attempts = _as_int(message.get("delay_count")) + 1
        except (TypeError, ValueError):
            attempts = 1

        if attempts > DELAY_MAX_ATTEMPTS:
            logger.error(
                "Task deferred %d times, giving up: id=%s type=%s reason=%s — "
                "任务不会自动重试了（deadline/上限已到），需要人工重投",
                attempts - 1, task_id, task_type, str(exc)[:200],
            )
            await self._move_to_dlq(
                task_id, task_type, payload_raw,
                f"deferred retry limit ({DELAY_MAX_ATTEMPTS}) reached: {exc}",
                message.get("retry_count", "0"),
            )
            await self._redis.xack(TASK_STREAM, GROUP_NAME, stream_id)
            return

        message["delay_count"] = str(attempts)
        delay = self._defer_delay(attempts, exc)
        try:
            await self._redis.zadd(
                DELAYED_ZSET, {json.dumps(message, ensure_ascii=False): time.time() + delay}
            )
            await self._redis.xack(TASK_STREAM, GROUP_NAME, stream_id)
        except Exception as e:  # noqa: BLE001 - 延迟集合不可用：退回立即重投（至少不丢）
            logger.warning(
                "Task defer failed (%s), falling back to immediate requeue: id=%s",
                str(e)[:160], task_id,
            )
            retry_count = _as_int(message.get("retry_count"))
            if retry_count >= MAX_RETRIES:
                await self._move_to_dlq(task_id, task_type, payload_raw,
                                        f"defer failed and retries exhausted: {exc}",
                                        retry_count)
            else:
                requeue = dict(message)
                requeue["retry_count"] = str(retry_count + 1)
                requeue.pop("delay_count", None)
                await self._redis.xadd(TASK_STREAM, requeue, maxlen=TASK_STREAM_MAXLEN)
                QUEUE_RETRY_TOTAL.labels(task_type=task_type).inc()
            await self._redis.xack(TASK_STREAM, GROUP_NAME, stream_id)
            return
        logger.warning(
            "Task deferred %.0fs (attempt %d/%d): id=%s type=%s reason=%s",
            delay, attempts, DELAY_MAX_ATTEMPTS, task_id, task_type, str(exc)[:160],
        )

    async def _delay_loop(self) -> None:
        """周期性把到期的延迟任务搬回主队列。"""
        while self._running:
            try:
                await asyncio.sleep(DELAY_LOOP_SECS)
                await self._promote_delayed()
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("worker delayed-retry loop error: %s", e)

    async def _promote_delayed(self, now: float | None = None) -> int:
        """把到期的延迟任务搬回主队列，返回到期数量。

        多实例并发是安全的：用 ``ZREM`` 的返回值做**原子抢占**，只有一个实例会重投。
        抢到之后如果 ``XADD`` 失败，则把它放回延迟集合（下次再试）—— 绝不丢。
        """
        ts = time.time() if now is None else now
        try:
            members = await self._redis.zrangebyscore(
                DELAYED_ZSET, "-inf", ts, start=0, num=DELAY_BATCH
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("worker delayed scan failed: %s", e)
            return 0
        promoted = 0
        for raw in members or []:
            try:
                claimed = await self._redis.zrem(DELAYED_ZSET, raw)
            except Exception as e:  # noqa: BLE001
                logger.warning("worker delayed claim failed: %s", e)
                continue
            if not claimed:
                continue  # 其它实例已抢走
            text = raw.decode() if isinstance(raw, bytes) else raw
            try:
                message = json.loads(text)
                await self._redis.xadd(TASK_STREAM, message, maxlen=TASK_STREAM_MAXLEN)
                promoted += 1
            except Exception as e:  # noqa: BLE001 - 放回延迟集合，不丢
                logger.warning("worker delayed requeue failed (%s), re-scheduling: %s",
                               str(e)[:160], text[:160])
                try:
                    await self._redis.zadd(DELAYED_ZSET, {text: time.time() + DELAY_BASE_SECONDS})
                except Exception as e2:  # noqa: BLE001
                    logger.error("worker delayed re-schedule failed, message lost: %s", e2)
        return promoted

    async def _dispatch(
        self, task_type: str, payload: dict, tenant_id: str = ""
    ) -> None:
        """分发任务到具体处理器"""
        if task_type == "rag_index":
            await self._handle_rag_index(payload)
        elif task_type == "memory_save":
            await self._handle_memory_save(payload, tenant_id)
        elif task_type == "memory_consolidate":
            await self._handle_memory_consolidate(payload, tenant_id)
        elif task_type == "memory_rollup":
            await self._handle_memory_rollup(payload, tenant_id)
        elif task_type == "embed_batch":
            await self._handle_embed_batch(payload)
        elif task_type == "tool_job":
            await self._handle_tool_job(payload)
        elif task_type == "workflow_run":
            await self._handle_workflow_run(payload)
        elif task_type == "agent_followup":
            await self._handle_agent_followup(payload)
        else:
            # 未知任务类型：显式失败（回队重投）而非 ACK 丢弃。
            # 滚动升级期间旧版 worker 收到新版 task_type（tool_job/workflow_run 等）时，
            # 抛错使消息 retry++ 回队尾，由新版 worker 重取；超 MAX_RETRIES 进 DLQ，
            # 避免新类型任务在升级窗口被静默确认丢弃。
            raise ValueError(f"unknown task type: {task_type}")

    async def _handle_agent_followup(self, payload: dict) -> None:
        """子 Agent 完成 → 请网关在父会话上开新一轮（真正的执行在 Go）。

        payload: {run_id, session_id, tenant_id, user_id, status, profile, depth, summary}

        为什么不在引擎侧开新一轮：``messages`` 落库、SSE 推送、turn 状态、计费与会话锁
        全在 Go；引擎侧自己跑一遍，这一轮在对话里是"看不见"的（见
        ``app/subagent/followup.py`` 与 ``internal/api/agent_followup.go``）。
        """
        run_id = str(payload.get("run_id") or "")
        session_id = str(payload.get("session_id") or "")
        if not run_id or not session_id:
            logger.warning("agent_followup payload 不完整: %s", payload)
            return

        from app.config import settings

        if not settings.internal_token:
            raise RuntimeError("agent_followup 需要 internal_token 才能调用网关")

        import httpx

        url = f"{settings.gateway_internal_url.rstrip('/')}/v1/internal/agent-followup"
        headers = {"X-Internal-Token": settings.internal_token}
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except Exception as exc:  # noqa: BLE001 - 网关暂时不可达（重启/滚动升级）
            raise RetryLaterError(f"agent_followup transport error: {exc}") from exc

        if resp.status_code == 409:
            # 该会话正在跑别的 turn：**要等**，不是失败。走延迟重试，不消耗重试预算。
            raise RetryLaterError(
                "agent_followup: session busy (409)", delay_seconds=_retry_after(resp)
            )
        if resp.status_code == 429:
            # 实例/租户并发已满：同样只是要等
            raise RetryLaterError(
                "agent_followup: too many requests (429)", delay_seconds=_retry_after(resp)
            )
        if 400 <= resp.status_code < 500:
            # 载荷问题：重试也没用（重试只会把同一条坏消息反复送到 DLQ）。
            # 明确记录并 ACK —— 可见地失败，而不是排队空转。
            logger.error(
                "agent_followup rejected by gateway (no retry): status=%s run=%s session=%s body=%s",
                resp.status_code, run_id, session_id, resp.text[:200],
            )
            return
        if not resp.is_success:
            raise RuntimeError(
                f"agent_followup rejected: {resp.status_code} {resp.text[:200]}"
            )
        logger.info("agent_followup accepted: run=%s session=%s", run_id, session_id)

    async def _handle_workflow_run(self, payload: dict) -> None:
        """执行（或续跑）workflow：读 DB checkpoint 跳过已完成节点，终态写回。

        payload: {instance_id, user_id, graph_json, initial_state}
        """
        from app.workflow.executor import execute_with_checkpoint

        instance_id = payload.get("instance_id") or ""
        if not instance_id:
            logger.warning("workflow_run payload 缺失: %s", payload)
            return
        await execute_with_checkpoint(
            instance_id,
            payload.get("graph_json") or {},
            payload.get("initial_state") or {},
            payload.get("user_id", ""),
            self._gateway,
        )
        logger.info("workflow_run done: instance=%s", instance_id)

    async def _handle_tool_job(self, payload: dict) -> None:
        """处理后台命令任务（run_in_background 队列化）：独立子进程执行 + 结果写 Redis。

        payload: {job_id, command, shell_key}（shell_key 仅保留信息，执行与持久 shell 解耦）
        """
        from app.tools.job_runner import execute_tool_job

        job_id = payload.get("job_id") or ""
        command = payload.get("command") or ""
        if not job_id or not command:
            logger.warning("tool_job payload 缺失: %s", payload)
            return
        res = await execute_tool_job(self._redis, job_id, command)
        logger.info(
            "tool_job done: job=%s status=%s exit_code=%s",
            job_id, res.get("status"), res.get("exit_code"),
        )

    async def _handle_rag_index(self, payload: dict) -> None:
        """处理 RAG 文档索引任务：读库取内容 → RAGBuilder 构建 → 更新文档/KB 状态与扣费

        payload: {kb_id, user_id, documents: [{doc_id, file_type, filename}], estimated_cost}
        """
        from app.db import get_pool
        from app.rag.builder import RAGBuilder

        kb_id = payload.get("kb_id")
        user_id = payload.get("user_id")
        documents = payload.get("documents") or []
        estimated_cost = payload.get("estimated_cost", 0)
        if not kb_id or not documents:
            raise ValueError(f"rag_index payload 缺失: {payload}")

        pool = get_pool()
        # 幂等：任务级重试/重复投递时 KB 已激活则跳过（避免重复构建与扣费）
        kb = await pool.fetchrow(
            "SELECT status FROM knowledge_bases WHERE id = $1", kb_id
        )
        if kb is None:
            # KB 已被删除（文档应随 CASCADE 清除）——直接失败进 DLQ，不误扣费
            raise ValueError(f"知识库不存在: {kb_id}")
        if kb["status"] == "active":
            logger.info("rag_index 跳过（KB 已激活）: kb_id=%s", kb_id)
            return

        builder = RAGBuilder(llm_gateway=self._gateway)
        errors = []

        for doc in documents:
            doc_id = doc.get("doc_id")
            row = await pool.fetchrow(
                "SELECT content, file_type, name FROM knowledge_documents WHERE id = $1",
                doc_id,
            )
            if row is None or row["content"] is None:
                errors.append(f"{doc_id}: 无内容")
                await pool.execute(
                    "UPDATE knowledge_documents SET status='error', error_message=$1 WHERE id=$2",
                    "no content",
                    doc_id,
                )
                continue

            await pool.execute(
                "UPDATE knowledge_documents SET status='processing' WHERE id=$1",
                doc_id,
            )
            chunk_count = 0
            error_msg = None
            try:
                async for event in builder.build_document(
                    kb_id=kb_id,
                    doc_id=doc_id,
                    content=bytes(row["content"]),
                    file_type=doc.get("file_type") or row["file_type"] or "txt",
                    filename=doc.get("filename") or row["name"] or doc_id,
                    tenant_id=user_id or "",
                ):
                    if event.get("type") == "complete":
                        chunk_count = event.get("chunk_count", 0)
                    elif event.get("type") == "error":
                        error_msg = event.get("message")
            except Exception as e:
                error_msg = str(e)

            if error_msg:
                errors.append(f"{doc_id}: {error_msg}")
                await pool.execute(
                    "UPDATE knowledge_documents SET status='error', error_message=$1 WHERE id=$2",
                    error_msg,
                    doc_id,
                )
            else:
                await pool.execute(
                    "UPDATE knowledge_documents SET status='completed', chunk_count=$1 WHERE id=$2",
                    chunk_count,
                    doc_id,
                )

        # 全部文档成功才扣费并激活；有失败则置 error（与 wiki 分支「成功才激活」语义一致）
        async with pool.acquire() as conn:
            async with conn.transaction():
                if errors:
                    await conn.execute(
                        "UPDATE knowledge_bases SET status='error', updated_at=NOW() WHERE id=$1",
                        kb_id,
                    )
                else:
                    # 防负扣费：余额不足时事务回滚并报错（任务进 DLQ，不重复扣费）
                    res = await conn.execute(
                        "UPDATE users SET credits = credits - $1 WHERE id = $2 AND credits >= $1",
                        estimated_cost,
                        user_id,
                    )
                    if not res or res.startswith("UPDATE 0"):
                        raise RuntimeError(
                            f"用户 {user_id} 积分不足，无法扣费 {estimated_cost}"
                        )
                    await conn.execute(
                        """UPDATE knowledge_bases
                           SET status = 'active', credits_consumed = credits_consumed + $1, updated_at = NOW()
                           WHERE id = $2""",
                        estimated_cost,
                        kb_id,
                    )
        if errors:
            logger.warning("rag_index 部分文档失败，KB 置 error: %s", errors)

        # Webhook 事件（knowledge.ingest/ingest_error）：跨实例统一出口（队列 worker）
        try:
            from app.event_bus import emit_event

            kb_payload = {
                "kb_id": kb_id,
                "user_id": user_id,
                "documents": [d.get("doc_id") for d in documents],
                "doc_count": len(documents),
            }
            if errors:
                kb_payload["errors"] = errors[:20]
                await emit_event(
                    "knowledge.ingest_error", kb_payload, tenant_id=user_id or "default"
                )
            else:
                await emit_event(
                    "knowledge.ingest", kb_payload, tenant_id=user_id or "default"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("knowledge webhook emit failed: %s", exc)

    async def _handle_memory_save(self, payload: dict, tenant_id: str = "") -> None:
        """处理记忆持久化任务

        payload: {key, value, source, user_id?, slot?, confidence?}
        """
        key = payload.get("key")
        if not key:
            raise ValueError("memory_save payload 缺少 key")
        if not self._memory_service:
            raise RuntimeError("memory_save 需要 memory_service 支持")

        from app.memory.layers import SlotType, SourceType

        slot = SlotType(payload.get("slot", "fact"))
        source = SourceType(payload.get("source", "derived"))
        confidence = int(payload.get("confidence", 50))
        user_id = payload.get("user_id", "")

        await self._memory_service.update_profile(
            tenant_id=tenant_id,
            user_id=user_id,
            slot=slot,
            item_key=key,
            item_value=str(payload.get("value", "")),
            confidence=confidence,
            source=source,
        )
        logger.info("memory_save 完成: key=%s tenant=%s", key, tenant_id)

    async def _handle_memory_consolidate(self, payload: dict, tenant_id: str) -> None:
        """处理记忆巩固任务：将对话消息巩固为 L3 摘要。

        payload: {session_id, user_id, turn_count, trigger}
        """
        if not self._memory_service:
            raise RuntimeError("memory_consolidate 需要 memory_service 支持")

        session_id = payload.get("session_id")
        user_id = payload.get("user_id", "")
        if not session_id:
            raise ValueError("memory_consolidate payload 缺少 session_id")

        # 从会话历史获取消息（简化：后续集成 ContextManager）
        messages = await self._get_session_messages(tenant_id, user_id, session_id)
        if not messages:
            logger.warning("No messages to consolidate: session=%s", session_id)
            return

        # 调用 Consolidator 进行巩固
        from app.memory.consolidator import Consolidator

        if self._memory_service._summary_store is None:
            logger.debug("SummaryStore not available, skip consolidate")
            return

        consolidator = Consolidator(store=self._memory_service._summary_store)
        turn_count = payload.get("turn_count", 0)
        result = await consolidator.consolidate(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            messages=messages,
            turn_start=max(0, turn_count - 10),
            turn_end=turn_count,
        )

        if result.error:
            logger.error("Consolidate failed: %s", result.error)
        elif result.deduplicated:
            logger.debug("Consolidate deduplicated: session=%s", session_id)
        else:
            logger.info(
                "Consolidate completed: session=%s, summary_id=%s",
                session_id,
                result.summary.id if result.summary else None,
            )

    async def _handle_memory_rollup(self, payload: dict, tenant_id: str) -> None:
        """处理记忆 rollup 任务：会话结束时的总结归档。

        payload: {session_id, user_id, trigger}
        """
        if not self._memory_service:
            raise RuntimeError("memory_rollup 需要 memory_service 支持")

        session_id = payload.get("session_id")
        user_id = payload.get("user_id", "")
        if not session_id:
            raise ValueError("memory_rollup payload 缺少 session_id")

        # 获取会话全部消息
        messages = await self._get_session_messages(tenant_id, user_id, session_id)
        if not messages:
            logger.debug("No messages to rollup: session=%s", session_id)
            return

        # 调用 Consolidator 进行完整 rollup
        from app.memory.consolidator import Consolidator

        if self._memory_service._summary_store is None:
            logger.debug("SummaryStore not available, skip rollup")
            return

        consolidator = Consolidator(store=self._memory_service._summary_store)
        result = await consolidator.consolidate(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            messages=messages,
            turn_start=0,
            turn_end=len(messages),
        )

        if result.error:
            logger.error("Rollup failed: %s", result.error)
        else:
            logger.info(
                "Rollup completed: session=%s, messages=%d, summary_id=%s",
                session_id,
                len(messages),
                result.summary.id if result.summary else None,
            )

    async def _get_session_messages(
        self, tenant_id: str, user_id: str, session_id: str
    ) -> list[dict]:
        """获取会话消息（从数据库）。"""
        from app.db import get_pool

        try:
            pool = get_pool()
            rows = await pool.fetch(
                """SELECT role, content, created_at
                   FROM unified_messages
                   WHERE session_id = $1
                   ORDER BY created_at ASC""",
                session_id,
            )
            return [
                {
                    "role": row["role"],
                    "content": row["content"],
                }
                for row in rows
                if row["content"] and isinstance(row["content"], str)
            ]
        except Exception as e:
            logger.warning("Failed to get session messages: %s", e)
            return []

    async def _handle_embed_batch(self, payload: dict) -> None:
        """处理批量嵌入任务：批量计算嵌入并存储向量

        payload: {texts, kb_id, doc_id, tenant_id}
        """
        from app.rag.builder import RAGBuilder

        texts = payload.get("texts") or []
        if not texts:
            raise ValueError("embed_batch payload 缺少 texts")
        builder = RAGBuilder(llm_gateway=self._gateway)
        chunks = [{"index": i, "content": t} for i, t in enumerate(texts)]
        embeddings = await builder._compute_embeddings(chunks)
        await builder._store_vectors(
            payload.get("kb_id", ""),
            payload.get("doc_id", ""),
            payload.get("tenant_id", ""),
            chunks,
            embeddings,
            builder._vector_db_type,
        )
        logger.info("embed_batch 完成: count=%d", len(texts))

    async def update_queue_depth(self) -> None:
        """更新队列深度指标"""
        try:
            info = await self._redis.xinfo_stream(TASK_STREAM)
            QUEUE_DEPTH.labels(stream="engine:tasks").set(info.get("length", 0))
        except Exception:
            logger.warning("Failed to update queue depth metric")
