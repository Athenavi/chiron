# QueueWorker 测试 — 消费/重试/DLQ + 三个任务 handler
# mock Redis 与构建/记忆依赖，不依赖真实 Redis/PostgreSQL
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.layers import SlotType, SourceType
from app.queue.worker import DLQ_STREAM, MAX_RETRIES, TASK_STREAM, QueueWorker


def make_worker(redis=None):
    return QueueWorker(redis=redis or AsyncMock())


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
    def _make_pool(doc_rows, kb_status="draft"):
        """构造 pool：首次 fetchrow 返回 KB 状态（幂等检查），随后返回各文档行"""
        class _Acq:
            """asyncpg Pool.acquire() 的 mock：async with pool.acquire() as conn"""

            def __init__(self, conn):
                self._conn = conn

            async def __aenter__(self):
                return self._conn

            async def __aexit__(self, *exc):
                return False

        pool = AsyncMock()
        pool.fetchrow = AsyncMock(side_effect=[{"status": kb_status}, *doc_rows])
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
