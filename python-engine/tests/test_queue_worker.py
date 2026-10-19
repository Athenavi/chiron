# QueueWorker 测试 — 消费/重试/DLQ + 三个任务 handler
# mock Redis 与构建/记忆依赖，不依赖真实 Redis/PostgreSQL
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.layers import SlotType, SourceType
from app.queue import worker as worker_module
from app.queue.worker import DLQ_STREAM, MAX_RETRIES, TASK_STREAM, QueueWorker


def make_worker(redis=None):
    return QueueWorker(redis=redis or AsyncMock())


def _session_pool(rows, owner="t1"):
    """`_get_session_messages` 用的连接池替身。

    非空时只被问一次；空的时候代码会再问一次 `unified_sessions.tenant_id` —— 因为
    "这个会话没内容"和"这个会话不归你"是完全不同的两种故障，前者正常，后者要说出来。
    """
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[{"role": r, "content": c} for r, c in rows])
    pool.fetchval = AsyncMock(return_value=owner)
    return pool


def fields(task_type="kb_index", task_id="t1", payload="{}", retry_count=0):
    return {
        b"task_type": task_type.encode(),
        b"task_id": task_id.encode(),
        b"payload": payload.encode(),
        b"retry_count": str(retry_count).encode(),
    }


class TestProcessMessage:
    """消息处理：ACK / 重试 / DLQ"""

    async def test_success_acks(self):
        redis = AsyncMock()
        worker = make_worker(redis)
        worker._dispatch = AsyncMock()
        await worker._process_message("1-0", fields())
        worker._dispatch.assert_awaited_once()
        redis.xack.assert_awaited_once_with(TASK_STREAM, "engine-workers", "1-0")
        redis.xadd.assert_not_awaited()

    async def test_failure_requeues_with_increment(self):
        redis = AsyncMock()
        worker = make_worker(redis)
        worker._dispatch = AsyncMock(side_effect=RuntimeError("boom"))
        await worker._process_message("1-0", fields(retry_count=0))
        # 重投到任务流，retry_count 递增
        redis.xadd.assert_awaited_once()
        stream, message, *_ = redis.xadd.await_args.args
        assert stream == TASK_STREAM
        assert message["task_type"] == "kb_index"
        assert message["retry_count"] == "1"
        redis.xack.assert_awaited()

    async def test_dlq_after_max_retries(self):
        redis = AsyncMock()
        worker = make_worker(redis)
        worker._dispatch = AsyncMock(side_effect=RuntimeError("boom"))
        await worker._process_message("1-0", fields(retry_count=MAX_RETRIES))
        # 进 DLQ，不再重投
        redis.xadd.assert_awaited_once()
        assert redis.xadd.await_args.args[0] == DLQ_STREAM
        dlq_message = redis.xadd.await_args.args[1]
        assert dlq_message["task_id"] == "t1"
        assert "boom" in dlq_message["error"]
        redis.xack.assert_awaited()

    async def test_unknown_task_type_logs_only(self):
        redis = AsyncMock()
        worker = make_worker(redis)
        await worker._process_message("1-0", fields(task_type="bogus"))
        redis.xack.assert_awaited_once()  # 未知类型不算失败


class TestHandleMemorySave:
    """memory_save handler"""

    async def test_saves_to_memory_service(self):
        fake_memory = AsyncMock()
        worker = QueueWorker(redis=AsyncMock(), memory_service=fake_memory)
        await worker._handle_memory_save({"key": "k1", "value": "v1", "source": "derived"}, tenant_id="t1")
        fake_memory.update_profile.assert_awaited_once_with(
            tenant_id="t1",
            user_id="",
            slot=SlotType.FACT,
            item_key="k1",
            item_value="v1",
            confidence=50,
            source=SourceType.DERIVED,
        )

    async def test_missing_key_raises(self):
        fake_memory = AsyncMock()
        worker = QueueWorker(redis=AsyncMock(), memory_service=fake_memory)
        with pytest.raises(ValueError, match="key"):
            await worker._handle_memory_save({"value": "v1"})

    async def test_missing_memory_service_raises(self):
        worker = make_worker()
        with pytest.raises(RuntimeError, match="memory_service"):
            await worker._handle_memory_save({"key": "k1", "value": "v1"})


class TestHandleMemoryConsolidate:
    """memory_consolidate handler — 巩固链路的最后一环。

    这一环此前是断的两次：`QueueWorker` 没被注入 `memory_service`（于是一进门就
    RuntimeError，烧完重试进 DLQ），而即使注入对了，handler 也会自己 `Consolidator(store=…)`
    再建一个没有 embedder/summariser 的实例。装配只该有 `app/main.py` 那一处。
    """

    async def test_consolidation_goes_through_the_injected_service(self):
        fake_memory = AsyncMock()
        fake_memory.save_summary.return_value = {
            "error": None,
            "summary": {"id": "sms_1"},
            "deduplicated": False,
            "skipped": False,
        }
        worker = QueueWorker(redis=AsyncMock(), memory_service=fake_memory)
        pool = _session_pool([("user", "聊聊 Go"), ("assistant", "好")])
        with patch("app.db.get_pool", return_value=pool):
            await worker._handle_memory_consolidate(
                {"session_id": "s1", "user_id": "u1", "turn_count": 2},
                tenant_id="t1",
            )

        fake_memory.save_summary.assert_awaited_once_with(
            "t1", "u1", "s1",
            [{"role": "user", "content": "聊聊 Go"}, {"role": "assistant", "content": "好"}],
            0,
            2,
        )

    async def test_worker_never_builds_its_own_consolidator(self):
        """结构守卫：worker 模块里不该再有 `Consolidator` 这个名字。

        口径（正文怎么生成、向量怎么算）必须由 MemoryService 那一个实例决定；就地再建一个
        就是"抽取式摘要 + 恒 NULL 向量"的第二套装配，而且是默认走到的那一套。
        """
        assert not hasattr(worker_module, "Consolidator")

    async def test_message_query_carries_the_task_tenant(self):
        """取会话消息必须带租户条件：巩固是写入动作，投错信封就等于给别人加记忆。"""
        fake_memory = AsyncMock()
        fake_memory.save_summary.return_value = {}
        worker = QueueWorker(redis=AsyncMock(), memory_service=fake_memory)
        pool = _session_pool([("user", "hi")])
        with patch("app.db.get_pool", return_value=pool):
            await worker._handle_memory_consolidate(
                {"session_id": "s1", "turn_count": 1}, tenant_id="t1"
            )

        sql, *args = pool.fetch.await_args.args
        assert args == ["s1", "t1"]
        assert "unified_sessions" in sql and "tenant_id" in sql

    async def test_cross_tenant_session_saves_nothing_and_warns(self, caplog):
        """查不到消息要分得清"会话没内容"和"这会话不是你的"，后者必须说出来。"""
        fake_memory = AsyncMock()
        worker = QueueWorker(redis=AsyncMock(), memory_service=fake_memory)
        pool = _session_pool([], owner="t2")
        with caplog.at_level("WARNING", logger="app.queue.worker"), patch(
            "app.db.get_pool", return_value=pool
        ):
            await worker._handle_memory_consolidate(
                {"session_id": "s1", "turn_count": 3}, tenant_id="t1"
            )

        fake_memory.save_summary.assert_not_awaited()
        assert "会话属于租户" in caplog.text

    async def test_unknown_session_is_named(self, caplog):
        fake_memory = AsyncMock()
        worker = QueueWorker(redis=AsyncMock(), memory_service=fake_memory)
        pool = _session_pool([], owner=None)
        with caplog.at_level("WARNING", logger="app.queue.worker"), patch(
            "app.db.get_pool", return_value=pool
        ):
            await worker._handle_memory_consolidate(
                {"session_id": "gone", "turn_count": 1}, tenant_id="t1"
            )

        assert "没有这个会话" in caplog.text

    async def test_error_outcome_raises_so_it_reaches_the_dlq(self):
        """写库失败要抛：静默 ACK 等于把"该写的记忆没写"变成永久丢失。"""
        fake_memory = AsyncMock()
        fake_memory.save_summary.return_value = {"error": "insert failed"}
        worker = QueueWorker(redis=AsyncMock(), memory_service=fake_memory)
        pool = _session_pool([("user", "hi")])
        with patch("app.db.get_pool", return_value=pool):
            with pytest.raises(RuntimeError, match="insert failed"):
                await worker._handle_memory_consolidate(
                    {"session_id": "s1", "turn_count": 1}, tenant_id="t1"
                )

    async def test_model_says_nothing_worth_remembering_is_not_a_failure(self):
        """`skipped` 是正常收口，不该烧重试预算 —— 否则每窗都得重试三轮进 DLQ。"""
        fake_memory = AsyncMock()
        fake_memory.save_summary.return_value = {"skipped": True}
        worker = QueueWorker(redis=AsyncMock(), memory_service=fake_memory)
        pool = _session_pool([("user", "hi")])
        with patch("app.db.get_pool", return_value=pool):
            await worker._handle_memory_consolidate(
                {"session_id": "s1", "turn_count": 1}, tenant_id="t1"
            )

    async def test_missing_memory_service_raises(self):
        worker = make_worker()
        with pytest.raises(RuntimeError, match="memory_service"):
            await worker._handle_memory_consolidate({"session_id": "s1"}, tenant_id="t1")

    async def test_missing_session_id_raises(self):
        worker = QueueWorker(redis=AsyncMock(), memory_service=AsyncMock())
        with pytest.raises(ValueError, match="session_id"):
            await worker._handle_memory_consolidate({"user_id": "u1"}, tenant_id="t1")


class TestHandleMemoryRollup:
    """memory_rollup handler"""

    async def test_rollup_records_the_full_message_range(self):
        fake_memory = AsyncMock()
        fake_memory.save_summary.return_value = {}
        worker = QueueWorker(redis=AsyncMock(), memory_service=fake_memory)
        pool = _session_pool([("user", "a"), ("assistant", "b"), ("user", "c")])
        with patch("app.db.get_pool", return_value=pool):
            await worker._handle_memory_rollup(
                {"session_id": "s1", "user_id": "u1"}, tenant_id="t1"
            )

        args = fake_memory.save_summary.await_args.args
        # 区间如实反映"取了整段会话"：过去写死 `turn_count-10` 会让 lineage 对不上内容
        assert (args[4], args[5]) == (0, 3)

    async def test_rollup_error_raises(self):
        fake_memory = AsyncMock()
        fake_memory.save_summary.return_value = {"error": "db down"}
        worker = QueueWorker(redis=AsyncMock(), memory_service=fake_memory)
        pool = _session_pool([("user", "a")])
        with patch("app.db.get_pool", return_value=pool):
            with pytest.raises(RuntimeError, match="db down"):
                await worker._handle_memory_rollup({"session_id": "s1"}, tenant_id="t1")
