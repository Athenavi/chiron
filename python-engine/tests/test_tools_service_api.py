"""工具 / Agent 的 HTTP 契约测试。

这些测试走 ASGITransport 发**真实 HTTP 请求**，因此必须按真实调用方式构造：
引擎的 AuthMiddleware 要求"网关路径"同时具备 `?user_id=&tenant_id=` 与
`X-Internal-Token`（后者由 tests/conftest.py 的 autouse fixture 统一注入，
因为真实链路上它是 Go 网关加的）。只带其中一个会被 401 —— 这正是设计意图。
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app

# 网关注入的身份（AuthMiddleware 要求 query 身份 + X-Internal-Token 同时存在）
IDENTITY = {"user_id": "test-user", "tenant_id": "1"}


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
async def test_list_agents_endpoint_removed():
    """`GET /v1/agents` 已在 40dfc99 **有意移除**，本用例把它钉成既定契约。

    移除理由（提交信息与 agents.py 注释）：页面主链路由 Go 的 DB `agents` 表提供，
    工具链改用 `list_subagent_runs`；保留一个"内存 registry 假派发"的列表端点只会
    让调用方以为存在一套并不存在的 Agent 注册表。若将来要恢复该端点，请连同这条
    用例一起改 —— 不要让它悄悄回来。
    """
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/v1/agents", params=IDENTITY)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_dispatch_agent_without_system_prompt_fails_explicitly():
    """无 `system_prompt` ⇒ **显式失败**，不得谎报 `status=dispatched`。

    40dfc99 之前这里回退到"内存 registry 的假派发"：返回 dispatched 但什么都没做，
    调用方与模型都会以为任务已派出（「失败要显式」）。
    真配置派发路径（带 system_prompt）由 tests/test_agents_dispatch_gateway.py 覆盖。
    """
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/v1/agents/dispatch",
            json={"task": "summarize doc", "agent_type": "knowledge"},
            params=IDENTITY,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert "system_prompt" in body["error"]
    assert body.get("status") != "dispatched"
