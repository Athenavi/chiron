"""回归：/v1/agents/dispatch 真执行路径的 gateway 注入。

历史 bug（2026-08-21）：agents.py 从 app.main 导入的 get_gateway 是 async 函数，
调用时漏了 await，导致 SubAgent 拿到 coroutine 对象，执行时报
'coroutine' object has no attribute 'chat_stream'。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


class _FakeGateway:
    """占位 gateway——SubAgent.run 被 mock，它只需是个普通对象。"""


@pytest.mark.asyncio
async def test_dispatch_passes_real_gateway_not_coroutine():
    """system_prompt 路径：传给 SubAgent 的 gateway 必须是实例而非 coroutine。"""
    app = create_app()
    captured: dict = {}

    class _FakeResult(SimpleNamespace):
        pass

    async def fake_run(self, task, context=None, tenant_id=""):
        captured["gateway"] = self._gateway
        return _FakeResult(
            success=True, output="ok", error="",
            tool_calls=[], token_usage={}, duration=0.1,
        )

    with patch("app.main._gateway", _FakeGateway()):
        with patch("app.agent.multi_agent.SubAgent.run", fake_run):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/v1/agents/dispatch",
                    json={"task": "do something", "system_prompt": "You are a helper."},
                    params={"user_id": "u-test", "tenant_id": "t-test"},
                )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["output"] == "ok"
    # 核心断言：不是 coroutine（asyncio.iscoroutine 会捕获漏 await 的回归）
    gw = captured.get("gateway")
    assert gw is not None, "SubAgent 未收到 gateway"
    assert not asyncio.iscoroutine(gw), (
        "gateway 是 coroutine —— get_gateway() 漏了 await（回归 bug 复现）"
    )
    assert isinstance(gw, _FakeGateway)


@pytest.mark.asyncio
async def test_dispatch_gateway_not_initialized_returns_error():
    """gateway 未初始化时 fail-loud 返回明确错误，而不是执行中途崩溃。"""
    app = create_app()
    with patch("app.main._gateway", None):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/v1/agents/dispatch",
                json={"task": "do something", "system_prompt": "You are a helper."},
                params={"user_id": "u-test", "tenant_id": "t-test"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert "gateway" in body["error"].lower()
