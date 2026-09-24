"""Tests for app.trace.writer module.

Verifies:
1. TraceWriter writes to Redis Stream (mocked)
2. record_span convenience function works
3. Graceful degradation when Redis is unavailable

历史说明：本文件中文注释曾在 UTF-8/GBK 混转中损坏（换行被吞 + 私用区码位），
Python 无法解析，长期被 conftest 的 collect_ignore 隔离。

修复要点（mock 方式）：`TraceWriter.get_instance()` 是**类级单例**，且**首次**调用时会
主动连真实 Redis 并覆盖 `cls._redis`（失败则置 None）。因此只 patch 类属性是**顺序依赖**的
——首个用例的 patch 会被 `get_instance()` 冲掉。这里统一用 fixture 预置单例与其 `_redis`。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def trace_writer():
    """预置 TraceWriter 单例 + Redis mock，返回 (writer, mock_redis, mock_pipeline)。

    同时复位模块级 `_trace_writer`，让 `record_span` 重新走 `get_instance()`
    拿到上面这个实例。
    """
    from app.trace import writer as writer_mod
    from app.trace.writer import TraceWriter

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock()
    mock_redis.xadd = AsyncMock(return_value="1234567890-0")

    mock_pipeline = AsyncMock()
    mock_pipeline.execute = AsyncMock()
    # redis-py 的 pipeline.xadd 是**同步入队**（execute() 才发出去），故用 MagicMock ——
    # 用 AsyncMock 会产生"coroutine was never awaited"警告。
    mock_pipeline.xadd = MagicMock()
    mock_redis.pipeline = MagicMock(return_value=mock_pipeline)

    saved = (TraceWriter._instance, TraceWriter._redis, writer_mod._trace_writer)
    writer = TraceWriter()
    writer._redis = mock_redis
    TraceWriter._instance = writer
    TraceWriter._redis = mock_redis
    writer_mod._trace_writer = None
    try:
        yield writer, mock_redis, mock_pipeline
    finally:
        TraceWriter._instance, TraceWriter._redis, writer_mod._trace_writer = saved


class TestTraceWriter:
    """TraceWriter 单元测试."""

    @pytest.mark.asyncio
    async def test_write_span_without_redis(self, trace_writer):
        """Redis 不可用时,write_span 应静默跳过 (不崩溃)."""
        writer, mock_redis, _ = trace_writer

        # Redis 未配置 → xadd 不会被调用
        writer._redis = None
        await writer.write_span(
            trace_id="test123",
            span_name="llm_call",
            duration_ms=1500,
            metadata={"model": "gpt-4"},
        )
        # No exception → success
        mock_redis.xadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_span_to_redis(self, trace_writer):
        """Redis 可用时,write_span 应写入 Stream."""
        writer, mock_redis, _ = trace_writer

        await writer.write_span(
            trace_id="abc123",
            span_name="tool:read_file",
            duration_ms=250,
            metadata={"file": "test.py"},
        )

        # Verify xadd was called with correct args
        mock_redis.xadd.assert_called_once()
        call_args = mock_redis.xadd.call_args
        # 多租户设计：无 tenant_id 时写入 anonymous stream
        assert call_args[0][0] == "chiron:traces:anonymous"  # stream name
        entry = call_args[0][1]
        assert entry["trace_id"] == "abc123"
        assert entry["span_name"] == "tool:read_file"
        assert entry["duration_ms"] == "250"
        assert 'metadata' in entry

    @pytest.mark.asyncio
    async def test_write_batch(self, trace_writer):
        """批量写入应使用 Redis pipeline."""
        writer, mock_redis, mock_pipeline = trace_writer

        spans = [
            {"trace_id": "t1", "span_name": "s1", "duration_ms": 100},
            {"trace_id": "t2", "span_name": "s2", "duration_ms": 200},
        ]

        await writer.write_batch(spans)

        # Verify pipeline usage
        mock_redis.pipeline.assert_called_once_with(transaction=False)
        assert mock_pipeline.xadd.call_count == 2
        mock_pipeline.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_instance_singleton(self):
        """TraceWriter 应为单例."""
        from app.trace.writer import TraceWriter

        w1 = await TraceWriter.get_instance()
        w2 = await TraceWriter.get_instance()
        assert w1 is w2

    @pytest.mark.asyncio
    async def test_record_span_convenience(self, trace_writer):
        """record_span 便捷函数应自动初始化 TraceWriter."""
        from app.trace import record_span

        _, mock_redis, _ = trace_writer

        await record_span(
            trace_id="conv_test",
            span_name="workflow_node",
            duration_ms=500,
            metadata={"node_id": "llm_1"},
        )

        mock_redis.xadd.assert_called_once()


class TestTraceEventFields:
    """AgentEvent 新增 trace 字段验证."""

    def test_agent_event_has_trace_fields(self):
        """AgentEvent dataclass 应包含 trace_id/span_name/duration_ms."""
        from app.agent.runtime import AgentEvent

        event = AgentEvent(
            type="trace_span",
            trace_id="test_trace",
            span_name="llm_call",
            duration_ms=1234,
        )

        assert event.trace_id == "test_trace"
        assert event.span_name == "llm_call"
        assert event.duration_ms == 1234

    def test_agent_event_backwards_compatible(self):
        """旧代码创建 AgentEvent 不应因缺少 trace 字段而失败"""
        from app.agent.runtime import AgentEvent

        # 旧写法 (不含 trace 字段)
        event = AgentEvent(type="text", content="hello")

        assert event.type == "text"
        assert event.content == "hello"
        assert event.trace_id == ""  # default
        assert event.span_name == ""  # default
        assert event.duration_ms == 0  # default
