# Prometheus 指标中间件
from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.observability.metrics import HTTP_REQUEST_DURATION, HTTP_REQUESTS, INSTANCE_ACTIVE_REQUESTS


class MetricsMiddleware(BaseHTTPMiddleware):
    """采集 HTTP 请求指标"""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        INSTANCE_ACTIVE_REQUESTS.inc()
        start = time.monotonic()
        response = None
        status = 500
        try:
            response = await call_next(request)
            return response
        finally:
            if response is not None:
                status = response.status_code
            elapsed = time.monotonic() - start
            path = request.url.path
            method = request.method
            # 对长路径归一化（避免高基数）
            if len(path) > 60:
                path = path[:60] + "..."
            HTTP_REQUESTS.labels(method=method, path=path, status=str(status)).inc()
            HTTP_REQUEST_DURATION.labels(method=method, path=path).observe(elapsed)
            INSTANCE_ACTIVE_REQUESTS.dec()
