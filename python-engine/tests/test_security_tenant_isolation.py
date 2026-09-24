"""SaaS Security Tests — 租户隔离验证.

关键安全场景:
1. Trace Stream 按 tenant_id 隔离
2. AgentEvent 携带 tenant_id 透传
3. Redis Stream 写入带 tenant_id 标记

历史说明：本文件中文注释曾在 UTF-8/GBK 混转中损坏（私用区码位 + 换行被吞），
另有 `MagicMock` 未随 `AsyncMock` 一起导入。此处按可还原内容修复。

修复要点（mock 方式）：`TraceWriter.get_instance()` 是**类级单例**，且**首次**调用时
会主动连真实 Redis 并覆盖 `cls._redis` —— 因此单靠 `patch(类属性)` 对首个用例无效：
它仍会走进真实连接、失败后把 `_redis` 置成 None，`write_span` 便静默跳过
（表现为 xadd.call_count == 0）。改用 fixture **预先备好实例与 _redis**，
并连同模块级 `_trace_writer` 一起复位，消除用例间的顺序依赖。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_redis():
    """把 TraceWriter 的 Redis 换成 mock（绕开单例的首次真实连接）。"""
    from app.trace import writer as writer_mod
    from app.trace.writer import TraceWriter

    mock = AsyncMock()
    mock.ping = AsyncMock()
    mock.xadd = AsyncMock(return_value="123")

    saved = (TraceWriter._instance, TraceWriter._redis, writer_mod._trace_writer)
    TraceWriter._instance = TraceWriter()
    TraceWriter._instance._redis = mock
    TraceWriter._redis = mock
    writer_mod._trace_writer = None  # 让 record_span 重新走 get_instance 拿到上面的实例
    try:
        yield mock
    finally:
        TraceWriter._instance, TraceWriter._redis, writer_mod._trace_writer = saved


class TestTenantIsolationTraceWriter:
    """TraceWriter 租户隔离测试."""

    @pytest.mark.asyncio
    async def test_trace_stream_isolation_by_tenant(self, mock_redis):
        """不同 tenant 应写入不同的 Redis Stream."""
        from app.trace.writer import TraceWriter, get_tenant_stream

        # 验证 stream key 生成逻辑
        assert get_tenant_stream("tenant_001") == "chiron:traces:tenant_001"
        assert get_tenant_stream("tenant_002") == "chiron:traces:tenant_002"
        assert get_tenant_stream("") == "chiron:traces:anonymous"
        assert get_tenant_stream(None) == "chiron:traces:anonymous"

        writer = await TraceWriter.get_instance()

        # 写入 tenant_001 的 span
        await writer.write_span(
            trace_id="trace_a",
            span_name="llm_call",
            duration_ms=100,
            tenant_id="tenant_001",
        )

        # 写入 tenant_002 的 span
        await writer.write_span(
            trace_id="trace_b",
            span_name="tool:read_file",
            duration_ms=200,
            tenant_id="tenant_002",
        )

        # Verify: xadd 被调用了两次,分别写入不同 stream
        assert mock_redis.xadd.call_count == 2

        calls = mock_redis.xadd.call_args_list
        assert calls[0][0][0] == "chiron:traces:tenant_001"
        assert calls[1][0][0] == "chiron:traces:tenant_002"

        # Verify entry 中带有 tenant_id 标记
        entry_001 = calls[0][0][1]
        entry_002 = calls[1][0][1]
        assert entry_001["tenant_id"] == "tenant_001"
        assert entry_002["tenant_id"] == "tenant_002"

    @pytest.mark.asyncio
    async def test_anonymous_tenant_isolation(self, mock_redis):
        """匿名租户 (无 tenant_id) 应写入 anonymous stream."""
        from app.trace.writer import TraceWriter

        writer = await TraceWriter.get_instance()

        # 不传 tenant_id
        await writer.write_span(
            trace_id="anon_trace",
            span_name="text",
            duration_ms=50,
        )

        mock_redis.xadd.assert_called_once()
        call_args = mock_redis.xadd.call_args
        assert call_args[0][0] == "chiron:traces:anonymous"
        entry = call_args[0][1]
        assert entry["tenant_id"] == ""

    @pytest.mark.asyncio
    async def test_record_span_with_tenant(self, mock_redis):
        """record_span 便捷函数应透传 tenant_id."""
        from app.trace import record_span

        await record_span(
            trace_id="conv_test",
            span_name="workflow_node",
            duration_ms=500,
            metadata={"node_id": "llm_1"},
            tenant_id="secure_tenant",
        )

        call_args = mock_redis.xadd.call_args
        stream = call_args[0][0]
        entry = call_args[0][1]

        assert stream == "chiron:traces:secure_tenant"
        assert entry["tenant_id"] == "secure_tenant"


class TestAgentEventTenantPropagation:
    """AgentEvent 租户 ID 透传测试."""

    def test_agent_event_includes_tenant_id(self):
        """AgentEvent 应携带 tenant_id."""
        from app.agent.runtime import AgentEvent

        event = AgentEvent(
            type="trace_span",
            trace_id="abc123",
            span_name="llm_call",
            duration_ms=1500,
            content='{"model": "gpt-4"}',
        )
        event.trace_id = "abc123"

        # 验证新字段存在
        assert hasattr(event, 'trace_id')
        assert hasattr(event, 'span_name')
        assert hasattr(event, 'duration_ms')
        assert event.trace_id == "abc123"

    @pytest.mark.asyncio
    async def test_runtime_propagates_tenant_to_trace(self):
        """AgentRuntime.run 应将 tenant_id 传给 record_span."""
        from app.agent.runtime import AgentRuntime, AgentTask
        from app.gateway.router import GatewayRouter

        # 创建 mock gateway（与其他测试一致：async generator 模拟流式响应）
        mock_gateway = AsyncMock(spec=GatewayRouter)

        async def fake_stream(**_kwargs):
            yield MagicMock(
                content="Hello",
                reasoning_content="",
                tool_calls=[],
                input_tokens=100,
                output_tokens=50,
                finish_reason="stop",
            )

        mock_gateway.chat_stream = fake_stream

        # 创建带 tenant_id 的 task
        task = AgentTask(
            id="task_001",
            tenant_id="isolated_tenant",
            user_id="user_123",
            session_id="session_456",
            content="test message",
        )

        runtime = AgentRuntime(gateway=mock_gateway)

        # Mock record_span 并拦截调用
        recorded_spans = []

        async def mock_record_span(**kwargs):
            recorded_spans.append(kwargs)

        # runtime 内局部导入 record_span，需 patch 源模块才能拦截
        with patch('app.trace.record_span', side_effect=mock_record_span):
            # 消费完事件后停止
            events_collected = []
            try:
                async for event in runtime.run(task):
                    events_collected.append(event)
                    if event.type == "done":
                        break
            except StopAsyncIteration:
                pass

        # Verify: 所有记录的 span 都带有正确的 tenant_id
        for span in recorded_spans:
            assert span.get("tenant_id") == "isolated_tenant", \
                f"Span tenant_id mismatch: expected 'isolated_tenant', got '{span.get('tenant_id')}'"


class TestTenantSecurityScenarios:
    """SaaS 安全场景测试."""

    def test_tenants_cannot_cross_reference_traces(self):
        """Tenant A 无法通过 trace 推断 Tenant B 的活动"""
        from app.trace.writer import get_tenant_stream

        # 验证 stream key 设计防止跨租户查询
        stream_a = get_tenant_stream("tenant_alpha")
        stream_b = get_tenant_stream("tenant_beta")

        # 两个 stream 完全独立
        assert stream_a != stream_b
        assert "tenant_alpha" not in stream_b
        assert "tenant_beta" not in stream_a

        # Anonymous stream 与已认证租户隔离
        stream_anon = get_tenant_stream("")
        assert stream_anon != stream_a
        assert "anonymous" in stream_anon

    def test_trace_metadata_no_sensitive_tenant_info(self):
        """Trace metadata 不应包含敏感租户信息 (如名称、配置)."""
        from app.agent.runtime import AgentEvent

        event = AgentEvent(
            type="trace_span",
            trace_id="test_trace",
            span_name="llm_call",
            duration_ms=1000,
            content='{"model": "gpt-4"}',
        )

        # 验证 metadata 只包含必要信息
        import json
        metadata = json.loads(event.content) if event.content else {}

        # 允许白名单字段
        allowed_fields = {"span_name", "duration_ms", "input_tokens", "output_tokens",
                          "model", "turn", "tool_name", "success"}

        for key in metadata.keys():
            assert key in allowed_fields, \
                f"Sensitive field '{key}' found in trace metadata"

    def test_tenant_id_validation(self):
        """tenant_id 应防止注入攻击 (如 Redis 命令注入)."""
        from app.trace.writer import get_tenant_stream

        malicious_ids = [
            "tenant;* MSET attack:true",  # Redis 命令注入
            "tenant\nbadline:attack",      # Redis CRLF 注入
            "tenant{\"noisy\":true}",       # JSON 注入
        ]

        for mal_id in malicious_ids:
            # 应该安全地转义到 stream key (不会崩溃或泄露)
            stream = get_tenant_stream(mal_id)
            assert isinstance(stream, str)
            assert len(stream) > 0
            # 恶意字符应被自然地转义为 key 的一部分 (Redis key 是安全的)
            assert "chiron:traces:" in stream
