# 认证中间件 — JWT + 网关内部 token（API Key 校验由网关负责，引擎不处理）
from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

import jwt
from jwt import InvalidTokenError
from starlette.middleware.base import (BaseHTTPMiddleware,
                                       RequestResponseEndpoint)
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.observability.logging import tenant_id_var
from app.redis_keys import rkey

logger = logging.getLogger(__name__)

# 公开路径（不需要认证）
PUBLIC_PATHS = {"/healthz", "/readyz", "/metrics", "/info", "/docs", "/openapi.json"}

# 弱密钥黑名单：与 Go 端 config.go ValidateJWTSecret 保持一致
_WEAK_JWT_SECRETS = frozenset(
    {
        "",
        "dev-secret-change-in-production",
        "dev-secret-change-in-production-12345678",
        "changeme",
        "secret",
        "test-secret",
        "change-me",
    }
)


class AuthMiddleware(BaseHTTPMiddleware):
    """
    认证策略（按架构设计优先级）:
      1. 公开路径直接放行
      2. 网关代理路径（主路径）：仅在 X-Internal-Token 与共享密钥匹配时，
         接受 ?tenant_id= / ?user_id= 透传身份。Go 网关 ForwardRequest 已剥离
         Authorization/Cookie 等头，所有代理请求走此路径。
      3. Bearer Token 路径（直连备用）：允许直连 Python 引擎的调用方使用
         有效 JWT 直接访问（绕过 Go 网关的场景，如内部服务调用）。

    安全约束：
      - query 参数透传身份必须校验 X-Internal-Token（防伪造 P0-3）
      - JWT 必须包含非空 tenant_id claim（多租户隔离）
      - 所有比较使用 compare_digest 防计时攻击
      - JWT 弱密钥在签发和校验两端均被拒绝
    """

    def __init__(
        self, app, redis_client=None, jwt_secret: str = "", internal_token: str = ""
    ):
        super().__init__(app)
        self._redis = redis_client
        self._internal_token = internal_token

        if not jwt_secret or len(jwt_secret) < 32 or jwt_secret in _WEAK_JWT_SECRETS:
            env_secret = os.getenv("JWT_SECRET", "")
            if (
                env_secret
                and env_secret not in _WEAK_JWT_SECRETS
                and len(env_secret) >= 32
            ):
                jwt_secret = env_secret
            else:
                logger.warning(
                    "AuthMiddleware: JWT_SECRET 未配置或为弱密钥，Bearer Token 路径将拒绝所有请求"
                )
        self._jwt_secret = jwt_secret

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # 公开路径
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        # Bearer Token 路径（直连备用）：默认关闭，见 ENGINE_ALLOW_DIRECT_JWT。
        # 开启时持有任意合法 JWT 即可直连引擎、自报 tenant_id，从而绕过 Go 网关的
        # 限流与审计；生产环境应保持关闭，仅在本地调试/直连工具场景显式打开。
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            from app.config import settings

            if not settings.allow_direct_jwt:
                logger.warning(
                    "Direct JWT auth rejected (ENGINE_ALLOW_DIRECT_JWT disabled) from %s",
                    request.client.host if request.client else "unknown",
                )
                return JSONResponse(
                    {
                        "error": "Direct JWT auth is disabled; route requests through the gateway"
                    },
                    status_code=403,
                )
            token = auth_header[7:]
            tenant_id = await self._validate_jwt(token)
            if tenant_id:
                logger.info(
                    "Direct JWT auth accepted (bypassing gateway): tenant=%s", tenant_id
                )
                return await self._set_tenant_and_continue(request, call_next, tenant_id)
            logger.warning(
                "JWT auth failed: invalid/expired token from %s",
                request.client.host if request.client else "unknown",
            )
            return JSONResponse({"error": "Invalid or expired token"}, status_code=401)

        # 网关代理路径：仅在 X-Internal-Token 校验通过时才信任 query 透传身份
        query_tid = request.query_params.get("tenant_id", "")
        query_uid = request.query_params.get("user_id", "")
        if query_tid and query_uid:
            if not self._is_internal_request(request):
                logger.warning(
                    "Rejected gateway-impersonation attempt: query tenant_id=%s without X-Internal-Token",
                    query_tid,
                )
                return JSONResponse(
                    {"error": "Gateway identity requires valid X-Internal-Token"},
                    status_code=401,
                )
            return await self._set_tenant_and_continue(request, call_next, query_tid)

        return JSONResponse({"error": "Authentication required"}, status_code=401)

    def _is_internal_request(self, request: Request) -> bool:
        """校验 X-Internal-Token 是否与配置的 internal_token 常量时间匹配。

        未配置 internal_token 时拒绝所有 query 透传身份（fail-close）。
        """
        if not self._internal_token:
            return False
        provided = request.headers.get("X-Internal-Token", "")
        if not provided:
            return False
        return hmac.compare_digest(provided, self._internal_token)

    async def _validate_jwt(self, token: str) -> Optional[str]:
        """解析 JWT 获取 tenant_id，并校验弱密钥黑名单与 Redis 登出黑名单。

        与 Go 端 ValidateJWTSecret + jwt:blacklist:<jti> 保持一致。
        """
        if not self._jwt_secret or self._jwt_secret in _WEAK_JWT_SECRETS:
            logger.warning("JWT secret not configured or weak — rejecting Bearer token")
            return None
        try:
            payload = jwt.decode(token, self._jwt_secret, algorithms=["HS256"])
            tid = payload.get("tenant_id")
            if not tid:
                logger.warning("JWT missing tenant_id claim, rejecting")
                return None
            jti = payload.get("jti")
            if jti and self._redis:
                try:
                    blacklisted = await self._redis.exists(rkey(f"jwt:blacklist:{jti}"))
                    if blacklisted:
                        logger.info("JWT rejected: blacklisted jti=%s", jti)
                        return None
                except Exception as e:
                    logger.warning("JWT blacklist check failed, rejecting token: %s", e)
                    return None  # fail-close: Redis 不可用时拒绝而非放过
            return tid
        except InvalidTokenError:
            logger.debug("JWT validation failed for token")
            return None

    async def _set_tenant_and_continue(
        self, request: Request, call_next: RequestResponseEndpoint, tenant_id: str
    ) -> Response:
        """设置 tenant_id 并继续"""
        token = tenant_id_var.set(tenant_id)
        request.state.tenant_id = tenant_id
        try:
            return await call_next(request)
        finally:
            tenant_id_var.reset(token)
