"""统一执行器 (UnifiedChatHandler) 测试。

核心意图 (fail loud):
- 已实现路径 (auto / agent) 必须返回真实执行结果,绝不接受伪造的固定文案
- 未实现路径 (workflow) 必须返回明确失败,绝不允许伪装成空成功
- 执行错误 (gateway 缺失 / LLM 报错) 必须传播为 success=False,不得被吞掉
"""
from __future__ import annotations

import pytest

import app.main  # noqa: F401 — 初始化 app 包，避免循环导入
from app.api.unified_executor import UnifiedChatHandler, get_chat_handler

TENANT = "tenant-exec-test"


# ── 测试替身 ────────────────────────────────────────────────────────


class _Chunk:
    """SubAgent.run 消费的流式 chunk 形状"""

    def __init__(self, content: str = "", finish_reason: str | None = None):
        self.content = content
        self.tool_calls = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.finish_reason = finish_reason


class _FakeGateway:
    """单次返回固定内容的 gateway"""

    def __init__(self, content: str = "agent-real-output"):
        self._content = content

    async def chat_stream(self, **kwargs):
        yield _Chunk(content=self._content, finish_reason="stop")


class _ErrorGateway:
    """LLM 返回 error 的 gateway"""

    async def chat_stream(self, **kwargs):
        yield _Chunk(finish_reason="error")


def _patch_gateway(monkeypatch, gateway):
    async def _get_gateway():
        return gateway

    monkeypatch.setattr(app.main, "get_gateway", _get_gateway)


def _patch_gateway_unavailable(monkeypatch):
    async def _get_gateway():
        raise RuntimeError("Gateway not initialized")

    monkeypatch.setattr(app.main, "get_gateway", _get_gateway)


# ── 请求解析与会话管理 ──────────────────────────────────────────────


async def test_submit_creates_session_and_records_history():
    """提交任务必须创建会话、生成 trace_id 并写入用户/助手消息历史。"""
    handler = UnifiedChatHandler()
    user_input = "帮我分析一下这份销售数据"

    res = await handler.submit_task(user_input=user_input, tenant_id=TENANT)

    assert res["success"] is True
    assert res["trace_id"], "未提供 trace_id 时必须自动生成"
    session_id = res["session_id"]
    session = handler.sessions[session_id]
    assert session.tenant_id == TENANT
    assert session.title == user_input[:50]
    assert [m["role"] for m in session.messages] == ["user", "assistant"]
    assert session.shared_context["last_trace_id"] == res["trace_id"]


async def test_submit_reuses_existing_session():
    """显式传入的 session_id 必须复用而非新建。"""
    handler = UnifiedChatHandler()
    first = await handler.submit_task(user_input="第一条", tenant_id=TENANT)
    second = await handler.submit_task(
        user_input="第二条", tenant_id=TENANT, session_id=first["session_id"]
    )
    assert second["session_id"] == first["session_id"]
    # 两次对话都应留在同一会话历史中 (2 轮 user+assistant)
    assert len(handler.sessions[first["session_id"]].messages) == 4


async def test_get_session_messages_not_found():
    """查询不存在的会话必须明确报错,而非返回空列表冒充成功。"""
    handler = UnifiedChatHandler()
    res = await handler.get_session_messages(
        session_id="missing-session", tenant_id=TENANT, user_id=TENANT
    )
    assert res["success"] is False
    assert "error" in res


async def test_get_session_messages_requires_tenant():
    """S 安全修复:不提供 tenant_id 必须被拒绝,杜绝无归属读取。"""
    handler = UnifiedChatHandler()
    res = await handler.get_session_messages(session_id="missing-session")
    assert res["success"] is False
    assert res["error"] == "tenant_id is required"


async def test_get_session_messages_returns_history():
    handler = UnifiedChatHandler()
    res = await handler.submit_task(user_input="你好", tenant_id=TENANT)
    history = await handler.get_session_messages(
        session_id=res["session_id"], tenant_id=TENANT, user_id=TENANT
    )
    assert history["success"] is True
    assert history["messages"][0]["role"] == "user"
    assert "shared_context" in history


async def test_get_session_messages_requires_user():
    """不提供 user_id 必须被拒绝：只校验租户时,同租户内可跨用户读消息。"""
    handler = UnifiedChatHandler()
    res = await handler.get_session_messages(session_id="s", tenant_id=TENANT)
    assert res["success"] is False
    assert res["error"] == "user_id is required"


async def test_get_session_messages_cross_tenant_blocked():
    """S 安全修复:其他租户不得读取本租户会话消息(跨租户隔离)。"""
    handler = UnifiedChatHandler()
    res = await handler.submit_task(user_input="机密消息", tenant_id=TENANT)
    blocked = await handler.get_session_messages(
        session_id=res["session_id"], tenant_id="other-tenant", user_id=TENANT
    )
    assert blocked["success"] is False
    assert blocked["error"] == "Session not found"


async def test_get_session_messages_cross_user_in_same_tenant_blocked():
    """S 安全修复:同租户的**其他用户**也不得读取本会话消息。

    会话归属是用户级的,租户只是它的外边界 —— 只校验 tenant 时,
    同租户用户只要拿到 session_id(或枚举它)就能读走别人的完整会话。
    """
    handler = UnifiedChatHandler()
    res = await handler.submit_task(user_input="同一租户的机密消息", tenant_id=TENANT)
    blocked = await handler.get_session_messages(
        session_id=res["session_id"], tenant_id=TENANT, user_id="another-user"
    )
    assert blocked["success"] is False
    assert blocked["error"] == "Session not found"


async def test_get_session_messages_cross_user_db_path_blocked():
    """DB 路径的归属查询必须同时带 tenant_id 与 user_id(不能只带 tenant)。"""
    from unittest.mock import AsyncMock, patch

    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=None)

    handler = UnifiedChatHandler()
    with patch("app.api.unified_executor.get_pool", return_value=pool):
        res = await handler.get_session_messages(
            session_id="s-1", tenant_id="t-1", user_id="u-1"
        )

    assert res["success"] is False
    query = pool.fetchrow.call_args[0][0]
    assert "tenant_id = $2" in query
    assert "user_id = $3" in query, "DB 路径缺少 user 归属条件"


# ── auto 模式 (TaskRouter,已实现) ───────────────────────────────────


async def test_auto_mode_runs_task_router_end_to_end():
    """auto 模式必须真实走 TaskRouter 编排并返回成功状态。

    无 LLM gateway 时 TaskRouter 降级到关键词意图识别,
    但整条链路必须完整返回而非抛错或伪造结果。
    """
    handler = UnifiedChatHandler()
    res = await handler.submit_task(user_input="搜索一下相关资料", tenant_id=TENANT)
    assert res["success"] is True
    assert res["metadata"]["task_id"], "TaskRouter 必须返回真实 task_id"
    # 输出必须是 TaskRouter 聚合结果的序列化,而非伪造固定文案
    assert "Agent 执行结果" not in res["output"]
    assert "工作流执行结果" not in res["output"]


async def test_auto_mode_propagates_router_output():
    """TaskRouter 的聚合输出必须原样透传给用户 (输出管道不被改写)。"""
    handler = UnifiedChatHandler()

    async def _fake_route_task(**kwargs):
        return {
            "task_id": "task_fake_1",
            "status": "completed",
            "total_duration_ms": 5,
            "subtasks": [],
            "output": {
                "subtasks_completed": 1,
                "outputs": [{"output": {"answer": 42}}],
            },
        }

    handler.router.route_task = _fake_route_task  # type: ignore[method-assign]

    res = await handler.submit_task(user_input="1+1=?", tenant_id=TENANT)
    assert res["success"] is True
    assert res["metadata"]["task_id"] == "task_fake_1"
    assert "42" in res["output"]


async def test_executor_error_status_must_not_become_success():
    """执行器返回 status=error 时,submit_task 必须报失败。

    意图: 错误状态不得被包装成 success=True 的响应。
    """
    handler = UnifiedChatHandler()

    async def _failing_route_task(**kwargs):
        return {
            "task_id": "task_err",
            "status": "error",
            "output": {"error": "boom from router"},
        }

    handler.router.route_task = _failing_route_task  # type: ignore[method-assign]

    res = await handler.submit_task(user_input="任意任务", tenant_id=TENANT)
    assert res["success"] is False
    assert "boom from router" in res["error"]


# ── agent 模式 (SubAgent.run,已实现) ────────────────────────────────


async def test_agent_mode_returns_real_agent_output(monkeypatch):
    """agent 模式必须真实调用 SubAgent.run 并透传 LLM 输出。

    意图: 返回内容来自真实执行,而非硬编码占位文案。
    """
    _patch_gateway(monkeypatch, _FakeGateway(content="unique-agent-answer-001"))
    handler = UnifiedChatHandler()

    res = await handler.submit_task(user_input="帮我写首诗", tenant_id=TENANT, mode="agent")

    assert res["success"] is True
    assert "unique-agent-answer-001" in res["output"]


async def test_agent_mode_without_gateway_fails_loud(monkeypatch):
    """gateway 未初始化时 agent 模式必须返回明确失败。"""
    _patch_gateway_unavailable(monkeypatch)
    handler = UnifiedChatHandler()

    res = await handler.submit_task(user_input="任意任务", tenant_id=TENANT, mode="agent")

    assert res["success"] is False
    assert "gateway" in res["error"].lower()


async def test_agent_mode_llm_error_fails_loud(monkeypatch):
    """LLM 返回 error 时必须传播为 success=False,不得静默成功。"""
    _patch_gateway(monkeypatch, _ErrorGateway())
    handler = UnifiedChatHandler()

    res = await handler.submit_task(user_input="任意任务", tenant_id=TENANT, mode="agent")

    assert res["success"] is False
    assert "Agent execution failed" in res["error"]
    # 失败必须进入会话历史,便于用户看到原因
    session = handler.sessions[res["session_id"]]
    assert session.messages[-1].get("error")


# ── workflow 模式 (已实现: 单节点 LLM 工作流) ──────────────────────


async def test_workflow_mode_returns_real_workflow_output(monkeypatch):
    """workflow 模式必须真实执行工作流并返回 LLM 输出。

    意图: 返回内容来自真实执行,而非硬编码占位文案。
    """
    _patch_gateway(monkeypatch, _FakeGateway(content="unique-workflow-answer-002"))
    handler = UnifiedChatHandler()

    res = await handler.submit_task(user_input="跑个工作流", tenant_id=TENANT, mode="workflow")

    assert res["success"] is True
    assert "unique-workflow-answer-002" in res["output"]


async def test_workflow_mode_without_gateway_fails_loud(monkeypatch):
    """gateway 未初始化时 workflow 模式必须返回明确失败。"""
    _patch_gateway_unavailable(monkeypatch)
    handler = UnifiedChatHandler()

    res = await handler.submit_task(user_input="任意任务", tenant_id=TENANT, mode="workflow")

    assert res["success"] is False
    assert "gateway" in res["error"].lower()


# ── 输出提取 ────────────────────────────────────────────────────────


def test_extract_output_prefers_last_subtask_output():
    handler = UnifiedChatHandler()
    result = {
        "output": {
            "outputs": [
                {"output": {"step": 1}},
                {"output": {"step": 2}},
            ]
        }
    }
    out = handler._extract_output(result)
    assert "2" in out and "1" not in out


def test_extract_output_handles_plain_string():
    handler = UnifiedChatHandler()
    assert handler._extract_output({"output": "plain-text"}) == "plain-text"


# ── 单例 ────────────────────────────────────────────────────────────


def test_get_chat_handler_is_singleton():
    assert get_chat_handler() is get_chat_handler()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
