# 请求限流中间件 — 调用 Gateway 的 TenantRateLimiter
from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.gateway.ratelimit import TenantRateLimiter
from app.observability.logging import tenant_id_var

logger = logging.getLogger(__name__)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """基于 tenant_id 的请求限流"""

    def __init__(self, app, limiter: TenantRateLimiter):
        super().__init__(app)
        self._limiter = limiter

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # 公开路径跳过
        if request.url.path in {"/healthz", "/readyz", "/metrics", "/info"}:
            return await call_next(request)

        tenant_id = tenant_id_var.get("")
        if not tenant_id:
            return await call_next(request)

        allowed = await self._limiter.allow(tenant_id)
        if not allowed:
            remaining = await self._limiter.get_remaining(tenant_id)
            return JSONResponse(
                {"error": "Rate limit exceeded", "remaining": remaining},
                status_code=429,
                headers={"Retry-After": "1"},
            )

        return await call_next(request)
