"""引擎的认证中间件必须真的挂在 app 上。

回归背景（本仓库最严重的一处发现）：``AuthMiddleware`` 与 ``RateLimitMiddleware``
曾经只存在于 ``_setup_middleware()`` —— 而那条路径**从未被调用**。
``uvicorn.run("app.main:create_app")`` 走的是 ``create_app()`` → ``_setup_middleware_early()``，
那里只挂了 ErrorHandler / Metrics / RequestContext / PrivacyMode。

后果不是"某条校验弱了"，而是引擎 HTTP 面**完全没有认证**：

- 任何能访问 8000 端口的人都可以自报 ``?user_id=<他人>&tenant_id=<他人租户>``，
  直接读写他人数据（memory / knowledge / chat sessions 都从这个 query 取身份）；
- ``config.allow_direct_jwt`` 那层"唯一防线"因此从未生效 —— 它就在那个没被挂载的
  中间件里；
- 而 Go 网关其实早已就绪（``python_client.go:289`` 注入 ``X-Internal-Token``、
  ``:786`` 剥离客户端伪造的同名头），闸门只是没合上。

所以这组断言盯的是**挂载状态**本身，而不是中间件的内部逻辑 —— 缺陷就在挂载上。
"代码存在"不等于"防线生效"，这是最容易被忽略的一类失效。
"""

from __future__ import annotations

import pytest


def _middleware_names() -> list[str]:
    from app.main import create_app

    return [m.cls.__name__ for m in create_app().user_middleware]


def test_auth_middleware_is_mounted():
    """AuthMiddleware 必须在 app 的中间件栈里。"""
    names = _middleware_names()
    assert "AuthMiddleware" in names, (
        "引擎未挂载 AuthMiddleware：HTTP 面将无任何认证，"
        f"任何人都能自报 user_id/tenant_id（当前栈: {names}）"
    )


def test_probe_paths_stay_public():
    """带认证后，探针路径仍必须放行，否则容器会被判定为不健康。"""
    from app.middleware.auth import PUBLIC_PATHS

    for path in ("/healthz", "/readyz", "/metrics"):
        assert path in PUBLIC_PATHS, f"{path} 不在 PUBLIC_PATHS：挂上认证后探针会被 401"


@pytest.mark.parametrize("path", ["/v1/memory/profile", "/v1/kb", "/v1/chat/sessions/x/messages"])
def test_identity_bearing_paths_are_not_public(path):
    """带身份的路径绝不能是公开路径 —— 那等于把未认证入口留给身份伪造。"""
    from app.middleware.auth import PUBLIC_PATHS

    assert path not in PUBLIC_PATHS


def test_gateway_identity_requires_internal_token():
    """query 透传身份必须依赖 X-Internal-Token：缺失/错误时拒绝。"""
    from app.middleware.auth import AuthMiddleware

    class _Req:
        def __init__(self, token: str | None):
            self.headers = {} if token is None else {"X-Internal-Token": token}

    mw = AuthMiddleware.__new__(AuthMiddleware)
    mw._internal_token = "s" * 40

    assert mw._is_internal_request(_Req("s" * 40)) is True
    assert mw._is_internal_request(_Req("wrong-token")) is False
    assert mw._is_internal_request(_Req(None)) is False

    # 未配置 internal_token 时必须 fail-close，不能放行任何 query 透传身份
    mw._internal_token = ""
    assert mw._is_internal_request(_Req("s" * 40)) is False


@pytest.mark.asyncio
async def test_gateway_proxy_without_query_identity_is_allowed():
    """带合法 token、**不带 query 身份**的网关代理请求必须放行。

    回归背景（业务不可用）：中间件曾只认 query 身份，而 `/v1/agent/submit`、
    `/v1/agent/approval`、followup 这些端点**从不带 query**（它们从 body / X-User-ID
    取身份）。于是网关的每一次 submit 代理都拿到 401，前端只看到
    "Service temporarily unavailable. Please try again." —— 整条对话链路不可用。

    这里用不存在的路径探测：放行 → 404（仅路由缺失），未放行 → 401。
    """
    from httpx import ASGITransport, AsyncClient

    from app.config import settings
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        allowed = await ac.get(
            "/v1/__probe__", headers={"X-Internal-Token": settings.internal_token}
        )
        rejected = await ac.get("/v1/__probe__", headers={"X-Internal-Token": "wrong-token"})

    assert allowed.status_code == 404, (
        "带合法 token 的网关代理请求被认证中间件拒了 —— body-身份端点会全链路 401"
    )
    assert rejected.status_code == 401
