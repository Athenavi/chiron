"""Multi-Agent System 测试"""
import asyncio
from unittest.mock import MagicMock

import pytest

from app.agent.multi_agent import (
    BUILTIN_AGENTS,
    AgentDispatcher,
    SubAgent,
    create_dispatcher_with_builtins,
)
from app.gateway.provider import ChatResponse

# ── Helpers ───────────────────────────────────────────────


def _make_mock_gateway(response_text: str = "Done!", input_tokens: int = 100, output_tokens: int = 50):
    """创建一个返回固定文本的 mock GatewayRouter"""
    mock = MagicMock()

    async def fake_stream(*_a, **_kw):
        yield ChatResponse(
            content=response_text,
            finish_reason="stop",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    mock.chat_stream = fake_stream
    return mock


def _make_error_gateway(error_msg: str = "LLM error"):
    """创建一个会抛出异常的 mock GatewayRouter"""
    mock = MagicMock()

    async def failing_stream(*_a, **_kw):
        raise Exception(error_msg)
        yield  # pragma: no cover

    mock.chat_stream = failing_stream
    return mock


# ── SubAgent Tests ────────────────────────────────────────


class TestSubAgent:
    """测试 SubAgent 核心功能"""

    @pytest.mark.asyncio
    async def test_run_simple_task(self):
        """SubAgent 能完成简单任务"""
        gateway = _make_mock_gateway("Hello, I'm the code agent!")
        agent = SubAgent(
            name="code",
            description="Test agent",
            system_prompt="You are a code assistant.",
            gateway=gateway,
        )

        result = await agent.run("Write a hello world function")

        assert result.success is True
        assert result.output == "Hello, I'm the code agent!"
        assert result.token_usage["input_tokens"] == 100
        assert result.token_usage["output_tokens"] == 50
        assert result.duration > 0
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_run_no_gateway(self):
        """没有 gateway 时返回失败"""
        agent = SubAgent(
            name="broken",
            description="Broken agent",
            system_prompt="test",
            gateway=None,
        )
        result = await agent.run("some task")

        assert result.success is False
        assert "No gateway" in result.error

    @pytest.mark.asyncio
    async def test_run_with_context(self):
        """上下文被注入到 system prompt"""
        gateway = _make_mock_gateway("contextual response")
        agent = SubAgent(
            name="review",
            description="Review agent",
            system_prompt="You are a reviewer.",
            gateway=gateway,
        )

        result = await agent.run(
            "Review this code",
            context={"file": "main.py", "language": "python"},
        )

        assert result.success is True
        assert result.output == "contextual response"

    @pytest.mark.asyncio
    async def test_run_with_error(self):
        """Gateway 异常时返回失败结果"""
        gateway = _make_error_gateway("Connection refused")
        agent = SubAgent(
            name="code",
            description="Test agent",
            system_prompt="test",
            gateway=gateway,
        )

        result = await agent.run("some task")

        assert result.success is False
        assert "Connection refused" in result.error

    @pytest.mark.asyncio
    async def test_run_with_tools(self):
        """SubAgent 可以接收工具定义"""
        gateway = _make_mock_gateway("Used a tool!")
        tools = [
            {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ]
        agent = SubAgent(
            name="code",
            description="Test agent",
            system_prompt="test",
            tools=tools,
            gateway=gateway,
        )

        result = await agent.run("Read main.py")
        assert result.success is True


# ── AgentDispatcher Tests ─────────────────────────────────


class TestAgentDispatcher:
    """测试 AgentDispatcher 调度器"""

    @pytest.mark.asyncio
    async def test_register_and_dispatch(self):
        """注册 agent 并调度任务，验证结果"""
        gateway = _make_mock_gateway("42")
        dispatcher = AgentDispatcher(gateway=gateway)

        dispatcher.register_agent(
            name="calculator",
            description="Calculator agent",
            system_prompt="You are a calculator.",
        )

        result = await dispatcher.dispatch("calculator", "What is 6*7?")

        assert result.success is True
        assert result.output == "42"

    @pytest.mark.asyncio
    async def test_register_and_dispatch_async(self):
        """使用 sync wrapper 验证注册 + 调度"""
        gateway = _make_mock_gateway("42")
        dispatcher = AgentDispatcher(gateway=gateway)

        dispatcher.register_agent(
            name="calculator",
            description="Calculator agent",
            system_prompt="You are a calculator.",
        )

        result = await dispatcher.dispatch("calculator", "What is 6*7?")
        assert result.success is True
        assert result.output == "42"

    @pytest.mark.asyncio
    async def test_dispatch_async_returns_task_id(self):
        """异步调度返回有效的 task_id"""
        gateway = _make_mock_gateway("async result")
        dispatcher = AgentDispatcher(gateway=gateway)

        dispatcher.register_agent(
            name="worker",
            description="Worker agent",
            system_prompt="You work.",
        )

        task_id = await dispatcher.dispatch_async("worker", "Do something")
        assert task_id is not None
        assert isinstance(task_id, str)
        assert len(task_id) > 0

    @pytest.mark.asyncio
    async def test_dispatch_async_result_available(self):
        """异步调度完成后可获取结果"""
        gateway = _make_mock_gateway("async done!")
        dispatcher = AgentDispatcher(gateway=gateway)

        dispatcher.register_agent(
            name="worker",
            description="Worker agent",
            system_prompt="Work.",
        )

        task_id = await dispatcher.dispatch_async("worker", "Do it")

        # 等待结果（给异步任务完成的时间）
        await asyncio.sleep(0.1)
        result = await dispatcher.get_result(task_id)

        assert result is not None
        assert result.success is True
        assert result.output == "async done!"

    @pytest.mark.asyncio
    async def test_dispatch_unknown_agent(self):
        """调度不存在的 agent 返回失败"""
        gateway = _make_mock_gateway("irrelevant")
        dispatcher = AgentDispatcher(gateway=gateway)

        result = await dispatcher.dispatch("nonexistent", "task")

        assert result.success is False
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_dispatch_async_unknown_agent(self):
        """异步调度不存在的 agent 也存储失败结果"""
        gateway = _make_mock_gateway("irrelevant")
        dispatcher = AgentDispatcher(gateway=gateway)

        task_id = await dispatcher.dispatch_async("ghost", "task")
        result = await dispatcher.get_result(task_id)

        assert result is not None
        assert result.success is False
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_get_result_nonexistent_task(self):
        """获取不存在的 task_id 返回 None"""
        dispatcher = AgentDispatcher()
        result = await dispatcher.get_result("no-such-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_agents(self):
        """验证 agent listing"""
        gateway = _make_mock_gateway()
        dispatcher = AgentDispatcher(gateway=gateway)

        dispatcher.register_agent("a", "Agent A", "prompt A")
        dispatcher.register_agent("b", "Agent B", "prompt B", tools=[{"name": "t1", "description": "tool1", "parameters": {}}])

        agents = dispatcher.list_agents()

        assert len(agents) == 2
        names = {a["name"] for a in agents}
        assert names == {"a", "b"}

        # 检查工具计数
        agent_b = next(a for a in agents if a["name"] == "b")
        assert agent_b["tools_count"] == 1

    @pytest.mark.asyncio
    async def test_get_agent(self):
        """get_agent 返回正确的 SubAgent 实例"""
        gateway = _make_mock_gateway()
        dispatcher = AgentDispatcher(gateway=gateway)
        dispatcher.register_agent("myagent", "My Agent", "prompt")

        agent = dispatcher.get_agent("myagent")
        assert agent is not None
        assert agent.name == "myagent"
        assert agent.description == "My Agent"

        assert dispatcher.get_agent("nonexistent") is None

    @pytest.mark.asyncio
    async def test_active_count(self):
        """验证活跃任务计数"""
        gateway = _make_mock_gateway("result")
        dispatcher = AgentDispatcher(gateway=gateway)
        dispatcher.register_agent("w", "Worker", "prompt")

        assert dispatcher.active_count == 0

        await dispatcher.dispatch_async("w", "task")
        # active count 可能已经变为 0（如果任务极快完成），所以只验证不会报错
        _ = dispatcher.active_count

        await asyncio.sleep(0.1)
        # 任务完成后应为 0
        assert dispatcher.active_count == 0


# ── Built-in Agents Tests ─────────────────────────────────


class TestBuiltinAgents:
    """测试内置代理注册"""

    def test_builtin_agents_defined(self):
        """内置代理定义存在"""
        assert "code" in BUILTIN_AGENTS
        assert "review" in BUILTIN_AGENTS
        assert "research" in BUILTIN_AGENTS
        assert "test" in BUILTIN_AGENTS

    def test_builtin_agents_have_required_fields(self):
        """每个内置代理都有 description 和 system_prompt"""
        for name, config in BUILTIN_AGENTS.items():
            assert "description" in config, f"{name} missing description"
            assert "system_prompt" in config, f"{name} missing system_prompt"
            assert "max_turns" in config, f"{name} missing max_turns"
            assert isinstance(config["system_prompt"], str)
            assert len(config["system_prompt"]) > 0

    def test_create_dispatcher_with_builtins(self):
        """create_dispatcher_with_builtins 注册了所有内置代理"""
        dispatcher = create_dispatcher_with_builtins()

        agents = dispatcher.list_agents()
        names = {a["name"] for a in agents}

        assert names == {"code", "review", "research", "test"}
        assert len(agents) == 4

    @pytest.mark.asyncio
    async def test_builtin_agents_dispatch(self):
        """内置代理可以正常调度"""
        gateway = _make_mock_gateway("Built-in agent response")
        dispatcher = create_dispatcher_with_builtins(gateway=gateway)

        for agent_name in ("code", "review", "research", "test"):
            result = await dispatcher.dispatch(agent_name, f"Test task for {agent_name}")
            assert result.success is True, f"Agent '{agent_name}' failed: {result.error}"
            assert result.output == "Built-in agent response"

    def test_builtin_agents_descriptions_meaningful(self):
        """内置代理描述不为空且有意义"""
        for name, config in BUILTIN_AGENTS.items():
            assert len(config["description"]) > 10, f"{name} description too short"

    def test_builtin_agents_system_prompts_actionable(self):
        """内置代理 system prompt 包含足够的指令"""
        for name, config in BUILTIN_AGENTS.items():
            prompt = config["system_prompt"]
            assert "You are" in prompt, f"{name} system prompt should define persona"
            assert len(prompt) > 50, f"{name} system prompt too short"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
