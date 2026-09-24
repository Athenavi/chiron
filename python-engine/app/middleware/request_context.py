# 请求上下文中间件 — request_id / tenant_id 注入
from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.observability.logging import request_id_var, tenant_id_var


class RequestContextMiddleware(BaseHTTPMiddleware):
    """为每个请求注入 request_id 和 tenant_id 到 contextvars"""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # request_id: 优先取 header，否则生成
        req_id = request.headers.get("X-Request-ID", "")
        if not req_id:
            req_id = uuid.uuid4().hex[:16]
        token_req = request_id_var.set(req_id)

        # tenant_id: 从 JWT 或 header 提取（auth middleware 后会设置）
        tenant_id = request.headers.get("X-Tenant-ID", "")
        token_tenant = tenant_id_var.set(tenant_id)

        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = req_id
            return response
        finally:
            request_id_var.reset(token_req)
            tenant_id_var.reset(token_tenant)
