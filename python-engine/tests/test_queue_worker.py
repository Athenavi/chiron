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


def fields(task_type="rag_index", task_id="t1", payload="{}", retry_count=0):
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
        assert message["task_type"] == "rag_index"
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


class TestHandleRagIndex:
    """rag_index handler：读库内容 → build_document → 状态流转与扣费"""

    @staticmethod
    def _make_pool(doc_rows, kb_status="draft", kb_tenant="t-kb"):
        """构造 pool：首次 fetchrow 返回 KB 行（幂等检查 + 租户归属），随后返回各文档行

        kb_tenant=None 用来模拟 knowledge_bases.tenant_id 为空（列可空）的历史行。
        """
        class _Acq:
            """asyncpg Pool.acquire() 的 mock：async with pool.acquire() as conn"""

            def __init__(self, conn):
                self._conn = conn

            async def __aenter__(self):
                return self._conn

            async def __aexit__(self, *exc):
                return False

        pool = AsyncMock()
        pool.fetchrow = AsyncMock(
            side_effect=[{"status": kb_status, "tenant_id": kb_tenant}, *doc_rows]
        )
        pool.execute = AsyncMock()
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="UPDATE 1")  # 扣费防负检查要求返回 UPDATE n
        conn.transaction = MagicMock(return_value=AsyncMock())
        pool.acquire = MagicMock(return_value=_Acq(conn))
        return pool, conn

    async def test_kb_already_active_is_idempotent_skip(self):
        """任务级重试/重复投递时 KB 已激活 → 跳过（不重复构建与扣费）"""
        worker = make_worker()
        pool, _ = self._make_pool(doc_rows=[], kb_status="active")
        with patch("app.db.get_pool", return_value=pool), \
             patch("app.rag.builder.RAGBuilder") as mock_cls:
            await worker._handle_rag_index({
                "kb_id": "kb-1", "user_id": "u-1",
                "documents": [{"doc_id": "d-1", "file_type": "txt", "filename": "a.txt"}],
                "estimated_cost": 3.0,
            })
        mock_cls.assert_not_called()  # 不构建
        pool.execute.assert_not_awaited()  # 不扣费不激活

    async def test_partial_failure_marks_kb_error_without_charging(self):
        """有文档失败 → KB 置 error，不扣费不激活"""
        worker = make_worker()
        pool, conn = self._make_pool(
            [{"content": b"data", "file_type": "txt", "name": "a.txt"}]
        )
        fake_builder = MagicMock()

        async def gen():
            yield {"type": "error", "message": "parse failed"}

        fake_builder.build_document.return_value = gen()
        with patch("app.db.get_pool", return_value=pool), \
             patch("app.rag.builder.RAGBuilder", return_value=fake_builder):
            await worker._handle_rag_index({
                "kb_id": "kb-1", "user_id": "u-1",
                "documents": [{"doc_id": "d-1", "file_type": "txt", "filename": "a.txt"}],
                "estimated_cost": 3.0,
            })
        # 文档 error 已标记
        sqls = [c.args for c in pool.execute.await_args_list]
        assert any("status='error'" in s[0] for s in sqls)
        # 事务内置 KB error，且未扣费
        tx_sqls = [c.args[0] for c in conn.execute.await_args_list]
        assert any("status='error'" in s for s in tx_sqls)
        assert not any("credits" in s for s in tx_sqls)
        assert not any("status = 'active'" in s for s in tx_sqls)

    async def test_full_flow_updates_states(self):
        redis = AsyncMock()
        worker = make_worker(redis)

        pool, conn = self._make_pool(
            [{"content": b"hello world", "file_type": "txt", "name": "a.txt"}]
        )
        fake_builder = MagicMock()

        async def gen():
            yield {"type": "progress", "step": "parsing", "progress": 0.1}
            yield {"type": "complete", "chunk_count": 2, "char_count": 11, "page_count": 0}

        fake_builder.build_document.return_value = gen()

        with patch("app.db.get_pool", return_value=pool), \
             patch("app.rag.builder.RAGBuilder", return_value=fake_builder):
            await worker._handle_rag_index({
                "kb_id": "kb-1", "user_id": "u-1",
                "documents": [{"doc_id": "d-1", "file_type": "txt", "filename": "a.txt"}],
                "estimated_cost": 3.0,
            })

        # build_document 收到库中内容与正确参数
        fake_builder.build_document.assert_called_once()
        kw = fake_builder.build_document.call_args.kwargs
        assert kw["kb_id"] == "kb-1" and kw["doc_id"] == "d-1"
        assert kw["content"] == b"hello world"

        # 状态流转（文档级，pool.execute）+ 扣费与激活（事务内，conn.execute）
        sqls = [c.args[0] for c in pool.execute.await_args_list]
        assert any("processing" in s for s in sqls)
        assert any("completed" in s and "chunk_count" in s for s in sqls)
        tx_sqls = [c.args[0] for c in conn.execute.await_args_list]
        assert any("credits = credits -" in s and "credits >= " in s for s in tx_sqls)  # 防负扣费
        assert any("status = 'active'" in s for s in tx_sqls)

    async def test_document_without_content_marks_error(self):
        worker = make_worker()
        pool, conn = self._make_pool(doc_rows=[None])  # 文档不存在/无内容
        with patch("app.db.get_pool", return_value=pool), \
             patch("app.rag.builder.RAGBuilder") as mock_cls:
            await worker._handle_rag_index({
                "kb_id": "kb-1", "user_id": "u-1",
                "documents": [{"doc_id": "d-missing", "file_type": "txt", "filename": "x.txt"}],
                "estimated_cost": 1.0,
            })
        mock_cls.assert_called_once()  # builder 在进入循环前构造
        mock_cls.return_value.build_document.assert_not_called()  # 无内容不构建
        [c.args[0] for c in pool.execute.await_args_list]
        assert any("status='error'" in s and "no content" in c.args[1] for s, c in
                   [(c.args[0], c) for c in pool.execute.await_args_list])
        # 全部文档失败 → 事务置 KB error，不扣费
        tx_sqls = [c.args[0] for c in conn.execute.await_args_list]
        assert any("status='error'" in s for s in tx_sqls)
        assert not any("credits" in s for s in tx_sqls)

    async def test_build_error_marks_document_error(self):
        worker = make_worker()
        pool, conn = self._make_pool(
            [{"content": b"data", "file_type": "txt", "name": "a.txt"}]
        )
        fake_builder = MagicMock()

        async def gen():
            yield {"type": "error", "message": "parse failed"}

        fake_builder.build_document.return_value = gen()
        with patch("app.db.get_pool", return_value=pool), \
             patch("app.rag.builder.RAGBuilder", return_value=fake_builder):
            await worker._handle_rag_index({
                "kb_id": "kb-1", "user_id": "u-1",
                "documents": [{"doc_id": "d-1", "file_type": "txt", "filename": "a.txt"}],
                "estimated_cost": 1.0,
            })
        sqls = [c.args for c in pool.execute.await_args_list]
        assert any("status='error'" in s[0] and "parse failed" in s[1] for s in sqls)

    async def test_vectors_and_events_use_the_kb_tenant(self):
        """写入侧租户只取 KB 行，任务信封里的 tenant 不予采信

        检索侧按 KB 的 tenant_id 过滤向量；构建侧若用别的值打标（信封可被投递方伪造），
        两边对不上 → 「索引成功但一条也查不到」的假成功，或把向量写进别人的租户。
        """
        worker = make_worker()
        pool, _ = self._make_pool(
            [{"content": b"hello", "file_type": "txt", "name": "a.txt"}], kb_tenant="t-kb"
        )
        fake_builder = MagicMock()

        async def gen():
            yield {"type": "complete", "chunk_count": 1, "char_count": 5, "page_count": 0}

        fake_builder.build_document.return_value = gen()
        with patch("app.db.get_pool", return_value=pool), \
             patch("app.rag.builder.RAGBuilder", return_value=fake_builder), \
             patch("app.event_bus.emit_event", new=AsyncMock()) as emit:
            await worker._handle_rag_index({
                "kb_id": "kb-1", "user_id": "u-1", "tenant_id": "t-envelope",
                "documents": [{"doc_id": "d-1", "file_type": "txt", "filename": "a.txt"}],
                "estimated_cost": 1.0,
            })
        assert fake_builder.build_document.call_args.kwargs["tenant_id"] == "t-kb"
        assert emit.await_args.kwargs["tenant_id"] == "t-kb"

    async def test_blank_kb_tenant_normalizes_to_default(self):
        """KB 行 tenant_id 为 NULL（存量数据）→ 归一到默认租户，与检索侧同规则

        两边都用 `tenant_id or DEFAULT_TENANT_ID`：否则块打上默认租户、查询按空串降级，
        存量库永远检索不到自己的内容。
        """
        from app.config import DEFAULT_TENANT_ID

        worker = make_worker()
        pool, _ = self._make_pool(
            [{"content": b"hello", "file_type": "txt", "name": "a.txt"}], kb_tenant=None
        )
        fake_builder = MagicMock()

        async def gen():
            yield {"type": "complete", "chunk_count": 1, "char_count": 5, "page_count": 0}

        fake_builder.build_document.return_value = gen()
        with patch("app.db.get_pool", return_value=pool), \
             patch("app.rag.builder.RAGBuilder", return_value=fake_builder), \
             patch("app.event_bus.emit_event", new=AsyncMock()) as emit:
            await worker._handle_rag_index({
                "kb_id": "kb-1", "user_id": "u-1",
                "documents": [{"doc_id": "d-1", "file_type": "txt", "filename": "a.txt"}],
                "estimated_cost": 1.0,
            })
        assert fake_builder.build_document.call_args.kwargs["tenant_id"] == DEFAULT_TENANT_ID
        assert emit.await_args.kwargs["tenant_id"] == DEFAULT_TENANT_ID


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


class TestHandleEmbedBatch:
    """embed_batch handler"""

    async def test_computes_and_stores_vectors(self):
        worker = make_worker()
        fake_builder = MagicMock()
        fake_builder._vector_db_type = "milvus"
        fake_builder._compute_embeddings = AsyncMock(return_value=[[0.1], [0.2]])
        fake_builder._store_vectors = AsyncMock()
        with patch("app.rag.builder.RAGBuilder", return_value=fake_builder):
            await worker._handle_embed_batch({
                "texts": ["a", "b"], "kb_id": "kb-1", "doc_id": "d-1", "tenant_id": "t-1",
            })
        fake_builder._compute_embeddings.assert_awaited_once()
        fake_builder._store_vectors.assert_awaited_once()

    async def test_missing_texts_raises(self):
        worker = make_worker()
        with patch("app.rag.builder.RAGBuilder") as mock_cls:
            with pytest.raises(ValueError, match="texts"):
                await worker._handle_embed_batch({"kb_id": "kb-1"})
        mock_cls.assert_not_called()
