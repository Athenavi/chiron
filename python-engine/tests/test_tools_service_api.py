"""工具 / 工作流 / Agent 的 HTTP 契约测试。

这些测试走 ASGITransport 发**真实 HTTP 请求**，因此必须按真实调用方式构造：
引擎的 AuthMiddleware 要求"网关路径"同时具备 `?user_id=&tenant_id=` 与
`X-Internal-Token`（后者由 tests/conftest.py 的 autouse fixture 统一注入，
因为真实链路上它是 Go 网关加的）。只带其中一个会被 401 —— 这正是设计意图。
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app, get_gateway

# 网关注入的身份（AuthMiddleware 要求 query 身份 + X-Internal-Token 同时存在）
IDENTITY = {"user_id": "test-user", "tenant_id": "test-tenant"}


def _mock_gateway():
    from unittest.mock import MagicMock

    from app.gateway.provider import ChatResponse

    gw = MagicMock()

    async def fake_stream(**_kwargs):
        yield ChatResponse(content="", finish_reason="stop")

    gw.chat_stream = fake_stream
    return gw


@pytest.mark.asyncio
async def test_list_tools_returns_tools_key():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/v1/tools", params=IDENTITY)
    assert resp.status_code == 200
    body = resp.json()
    assert "tools" in body
    assert isinstance(body["tools"], list)


@pytest.mark.asyncio
async def test_identity_is_required_without_gateway_headers():
    """不带网关身份必须 401 —— 这是引擎认证的底线，必须有测试盯着。

    本仓库的 conftest 会**自动**给 ASGITransport 请求注入网关的 `X-Internal-Token`
    （模拟真实链路），所以这里显式传空值把它关掉 —— 本用例验证的正是"没有它"。
    """
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/v1/tools", headers={"X-Internal-Token": ""})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_execute_tool_requires_name():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post("/v1/tools/execute", json={"input": {}}, params=IDENTITY)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_execute_tool_returns_output_for_known_tool():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/v1/tools/execute",
            json={"name": "shell_exec", "input": {"command": "echo ok"}},
            params=IDENTITY,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("exit_code") == 0
    assert "ok" in body.get("stdout", "")


@pytest.mark.asyncio
async def test_execute_workflow_returns_instance():
    app = create_app()
    app.dependency_overrides[get_gateway] = _mock_gateway
    payload = {
        "name": "wf-test",
        "nodes": [
            {"id": "input_1", "label": "Input", "node_type": "input"},
            {"id": "output_1", "label": "Output", "node_type": "output"},
        ],
        "edges": [{"source_id": "input_1", "target_id": "output_1"}],
        "initial_state": {"input": "hello"},
    }
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/v1/graphs/demo/execute", json=payload, params=IDENTITY)
        assert resp.status_code == 200
        body = resp.json()
        # execute 为异步提交契约：立即返回 running + instance_id，前端轮询 status
        assert body["status"] == "running"
        assert "instance_id" in body
    finally:
        app.dependency_overrides.pop(get_gateway, None)


@pytest.mark.integration  # 需要真实 PostgreSQL：status 查询只走 workflow_instances，无内存 fallback
@pytest.mark.asyncio
async def test_workflow_status_returns_instance():
    app = create_app()
    app.dependency_overrides[get_gateway] = _mock_gateway
    payload = {
        "name": "wf-status",
        "nodes": [
            {"id": "input_1", "label": "Input", "node_type": "input"},
            {"id": "output_1", "label": "Output", "node_type": "output"},
        ],
        "edges": [{"source_id": "input_1", "target_id": "output_1"}],
        "initial_state": {"input": "hello"},
    }
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            run_resp = await ac.post("/v1/graphs/demo/execute", json=payload, params=IDENTITY)
            instance_id = run_resp.json()["instance_id"]
            status_resp = await ac.get(
                f"/v1/workflows/{instance_id}/status", params=IDENTITY
            )
        assert status_resp.status_code == 200
        body = status_resp.json()
        assert body["instance_id"] == instance_id
        assert body["status"] in {"completed", "error"}
    finally:
        app.dependency_overrides.pop(get_gateway, None)


@pytest.mark.asyncio
async def test_workflow_status_missing_returns_404():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/v1/workflows/not-exist/status", params=IDENTITY)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_agents_returns_agents():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/v1/agents", params=IDENTITY)
    assert resp.status_code == 200
    body = resp.json()
    assert "agents" in body
    assert isinstance(body["agents"], list)
    assert len(body["agents"]) >= 1


@pytest.mark.asyncio
async def test_dispatch_agent_returns_dispatched():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/v1/agents/dispatch",
            json={"task": "summarize doc", "agent_type": "knowledge"},
            params=IDENTITY,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("agent_type") == "knowledge"
    assert body.get("status") == "dispatched"
