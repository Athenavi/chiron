"""Python AI 引擎测试"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import Settings
from app.agent.loop import build_messages, convert_tools, run_agent


class TestBuildMessages:
    """测试消息构建"""

    def test_basic_messages(self):
        messages = build_messages(
            system_prompt="You are helpful",
            history=[],
            content="Hello",
        )
        assert len(messages) == 2
        assert messages[0].role == "system"
        assert messages[0].content == "You are helpful"
        assert messages[1].role == "user"
        assert messages[1].content == "Hello"

    def test_with_history(self):
        history = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        messages = build_messages(
            system_prompt="System",
            history=history,
            content="How are you?",
        )
        assert len(messages) == 4
        assert messages[1].role == "user"
        assert messages[2].role == "assistant"

    def test_empty_system_prompt(self):
        messages = build_messages(
            system_prompt="",
            history=[],
            content="Hello",
        )
        assert len(messages) == 1
        assert messages[0].role == "user"


class TestConvertTools:
    """测试工具定义转换"""

    def test_basic_conversion(self):
        tools = [
            {
                "name": "read_file",
                "description": "Read a file",
                "parameters_json": '{"type": "object", "properties": {"path": {"type": "string"}}}',
            }
        ]
        converted = convert_tools(tools)
        assert len(converted) == 1
        assert converted[0]["type"] == "function"
        assert converted[0]["function"]["name"] == "read_file"

    def test_empty_tools(self):
        assert convert_tools([]) == []

    def test_invalid_json(self):
        tools = [
            {
                "name": "bad_tool",
                "description": "Bad",
                "parameters_json": "invalid json",
            }
        ]
        converted = convert_tools(tools)
        assert len(converted) == 1
        assert converted[0]["function"]["parameters"] == {}


class TestRunAgent:
    """测试 Agent 推理循环"""

    @pytest.mark.asyncio
    async def test_simple_response(self):
        """测试简单文本响应"""
        mock_gateway = MagicMock()

        async def fake_stream(*_a, **_kw):
            yield MagicMock(content="Hello!", tool_calls=None, finish_reason="stop")

        mock_gateway.chat_stream = fake_stream

        events = []
        async for event in run_agent(
            gateway=mock_gateway,
            system_prompt="You are helpful",
            history=[],
            content="Hi",
            tools=None,
            llm_config={"model": "test"},
            max_turns=1,
        ):
            events.append(event)

        assert any(e["type"] == "text" for e in events)
        assert any(e["type"] == "done" for e in events)

    @pytest.mark.asyncio
    async def test_error_handling(self):
        """测试错误处理"""
        mock_gateway = MagicMock()

        async def failing_stream(*_a, **_kw):
            raise Exception("LLM error")

        mock_gateway.chat_stream = failing_stream

        events = []
        async for event in run_agent(
            gateway=mock_gateway,
            system_prompt="test",
            history=[],
            content="test",
            max_turns=1,
        ):
            events.append(event)

        assert any(e["type"] == "error" for e in events)

    @pytest.mark.asyncio
    async def test_truncated_tool_call_is_not_dispatched(self):
        """坑2：finish_reason=length 时 arguments 可能是半截 JSON，不得转发执行。"""
        mock_gateway = MagicMock()

        async def truncated_stream(*_a, **_kw):
            yield MagicMock(
                content="",
                input_tokens=0,
                output_tokens=0,
                finish_reason="length",
                tool_calls=[
                    MagicMock(
                        id="call_1",
                        name="write_file",
                        arguments='{"path": "a.txt", "content": "hel',
                    )
                ],
            )

        mock_gateway.chat_stream = truncated_stream

        events = []
        async for event in run_agent(
            gateway=mock_gateway,
            system_prompt="test",
            history=[],
            content="write a file",
            tools=[{"name": "write_file", "parameters": {"type": "object"}}],
            llm_config={"model": "test"},
            max_turns=1,
        ):
            events.append(event)

        assert not any(
            e["type"] == "tool_call" for e in events
        ), "被 max_tokens 截断的 tool_call 不得转发给执行方"
        assert any(e["type"] == "error" for e in events)

    @pytest.mark.asyncio
    async def test_complete_tool_call_is_dispatched(self):
        """对照：finish_reason=stop 时 tool_call 正常转发（防回归）。"""
        mock_gateway = MagicMock()

        async def ok_stream(*_a, **_kw):
            yield MagicMock(
                content="",
                input_tokens=0,
                output_tokens=0,
                finish_reason="stop",
                tool_calls=[
                    MagicMock(
                        id="call_1", name="read_file", arguments='{"path": "a.txt"}'
                    )
                ],
            )

        mock_gateway.chat_stream = ok_stream

        events = []
        async for event in run_agent(
            gateway=mock_gateway,
            system_prompt="test",
            history=[],
            content="read a file",
            tools=[{"name": "read_file", "parameters": {"type": "object"}}],
            llm_config={"model": "test"},
            max_turns=1,
        ):
            events.append(event)

        assert any(e["type"] == "tool_call" for e in events)


class TestSettings:
    """测试配置"""

    def test_default_values(self):
        # _env_file=None：隔离项目根 .env（默认值测试只验证代码内默认）
        settings = Settings(_env_file=None)
        assert settings.http_port == 8000
        assert settings.max_turns == 10
        assert settings.default_model == "claude-sonnet-4-20250514"
        # RAG 配置默认值
        assert settings.vector_db_type == "milvus"
        assert settings.pgvector_table == "knowledge_chunk_vectors"
        assert settings.local_embedding_model == ""  # 默认关闭本地嵌入

    def test_custom_values(self, monkeypatch):
        monkeypatch.setenv("HTTP_PORT", "8080")
        monkeypatch.setenv("MAX_TURNS", "20")
        settings = Settings()
        assert settings.http_port == 8080
        assert settings.max_turns == 20


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
