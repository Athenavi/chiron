# 统一异常处理中间件
from __future__ import annotations

import logging
import traceback

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.observability.logging import request_id_var

logger = logging.getLogger(__name__)


class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    """捕获未处理异常，返回统一 JSON 格式错误"""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        try:
            response = await call_next(request)
            return response
        except Exception as e:
            req_id = request_id_var.get("")
            logger.error(
                "Unhandled exception: %s\n%s",
                str(e),
                traceback.format_exc(),
            )
            return JSONResponse(
                {
                    "error": "Internal server error",
                    "detail": (
                        str(e) if logger.isEnabledFor(logging.DEBUG) else ""
                    ),  # 安全：生产环境 LOG_LEVEL=INFO 时不泄露异常详情
                    "request_id": req_id,
                },
                status_code=500,
            )
