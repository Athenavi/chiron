"""设备 token 鉴权中间件（契约 §4）。

第一批的立场是：**本地 core 不校验任何 token**。任何能连上 ``127.0.0.1`` 的进程都已经
是本机主人，加一层口令只会在握手链路上多一个失败点。因此 ``settings.auth_enabled``
默认 ``False`` —— 中间件存在，但直接放行（``/readyz`` 报 ``auth: "disabled"``）。

桌面端需要时可以显式开启（``CHIRON_LOCAL_AUTH_ENABLED=1``）：此后除三个免鉴权端点外，
一律要求 ``Authorization: Bearer <设备 token>``，与 ``<data-dir>/auth/device-token``
（也就是 ``POST /v1/auth/session`` 返回的那一个）做**常量时间**比较。

三个免鉴权端点少了哪个握手都会断：

- ``/healthz``、``/readyz``：壳层要据此判断"后端起来了没"，此时它还没拿到 token；
- ``/v1/auth/session``：token 正是从这里换出来的，要求先有 token 就是死循环。

为什么写成纯 ASGI 而不是 ``BaseHTTPMiddleware``：后者把响应包成 anyio 流再转发，
SSE（``/events``）的心跳会被额外缓冲，客户端看起来像"连上了但没数据"。
"""

from __future__ import annotations

import hmac
from collections.abc import Iterable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.auth import load_or_create_token
from app.config import Settings

#: 无需 token 的路径（规范化后比较，见 :func:`_normalize_path`）
EXEMPT_PATHS: frozenset[str] = frozenset({"/healthz", "/readyz", "/v1/auth/session"})

#: ``WWW-Authenticate`` 的值：告诉 401 的客户端该用哪种 scheme
AUTH_SCHEME = "Bearer"

UNAUTHORIZED_BODY: dict[str, str] = {
    "error": "unauthorized",
    "message": "本地 core 已开启设备 token 鉴权（CHIRON_LOCAL_AUTH_ENABLED=1）；"
    "请在 Authorization 头里带 `Bearer <设备 token>`，token 可从 POST /v1/auth/session 取得",
}


def _normalize_path(path: str) -> str:
    """尾斜杠等价：``/healthz/`` 不该因为多一个斜杠就被判 401。

    FastAPI 会把 ``/healthz/`` 307 到 ``/healthz``，但中间件先于路由执行，
    这时候还看不到重定向结果，只能自己容忍两种写法。
    """
    stripped = path.rstrip("/")
    return stripped or "/"


def parse_bearer(header: str | None) -> str:
    """从 ``Authorization`` 头里取 token。

    scheme 不匹配（``Basic …``）、没有值、头缺失都返回空串 —— 交给调用方统一判 401，
    不要在这里区分"没给"与"给错了"（对探测者来说两者不该有区别）。
    """
    if not header:
        return ""
    scheme, _, value = header.partition(" ")
    if scheme.strip().lower() != AUTH_SCHEME.lower():
        return ""
    return value.strip()


def tokens_match(expected: str, supplied: str) -> bool:
    """常量时间比较设备 token；任一侧为空一律 False（空 == 空 绝不算通过）。"""
    if not expected or not supplied:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))


def _authorization_header(scope: Scope) -> str | None:
    """ASGI 头是 ``list[tuple[bytes, bytes]]`` 且可能重复，取第一个 ``authorization``。"""
    for name, value in scope.get("headers") or []:
        if name == b"authorization":
            # latin-1 是 ASGI 头的约定解码方式（token 是 ascii，不会失真）
            return value.decode("latin-1")
    return None


class DeviceTokenMiddleware:
    """``auth_enabled`` 打开时校验设备 token，关闭时原样放行。

    配置来源默认是 ``scope["app"].state.settings``（``create_app`` 里挂上去的那份），
    这样测试改 ``app.state.settings`` 就能生效；需要固定一份时用 ``settings=`` 注进去。
    """

    def __init__(
        self,
        app: ASGIApp,
        settings: Settings | None = None,
        token: str | None = None,
        exempt_paths: Iterable[str] | None = None,
    ) -> None:
        self.app = app
        self._settings = settings
        # token 显式注入（测试 / 特殊场景）；否则首次校验时才去读盘，避免启动期多余 IO
        self._token = token
        self._exempt = frozenset(_normalize_path(p) for p in (exempt_paths or EXEMPT_PATHS))

    # ------------------------------------------------------------------ 内部
    def _settings_for(self, scope: Scope) -> Settings | None:
        if self._settings is not None:
            return self._settings
        app = scope.get("app")  # starlette 在 applications.py 里写入，指向 FastAPI 实例
        return getattr(getattr(app, "state", None), "settings", None)

    def _expected_token(self, settings: Settings) -> str:
        if self._token is None:
            # 与 POST /v1/auth/session 走**同一个**函数：两处若各算各的，
            # 一旦启用鉴权，"换来的 token" 与 "校验用的 token" 不一致就全是 401。
            # 该函数优先读 CHIRON_DEVICE_TOKEN，其次才是落盘文件。
            self._token = load_or_create_token(settings.device_token_path)
        return self._token

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            UNAUTHORIZED_BODY,
            status_code=401,
            headers={"WWW-Authenticate": AUTH_SCHEME},
        )
        await response(scope, receive, send)

    # ------------------------------------------------------------------ ASGI
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # lifespan / websocket 不走这里（SSE 是 http，仍会经过）
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = self._settings_for(scope)
        if settings is None or not settings.auth_enabled:
            await self.app(scope, receive, send)
            return

        if _normalize_path(scope.get("path", "")) in self._exempt:
            await self.app(scope, receive, send)
            return

        supplied = parse_bearer(_authorization_header(scope))
        if tokens_match(self._expected_token(settings), supplied):
            await self.app(scope, receive, send)
            return

        await self._reject(scope, receive, send)
