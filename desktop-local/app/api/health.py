"""``/healthz`` 与 ``/readyz``（契约 §4）。

- ``/healthz``：进程活着就 200，不碰存储。Electron 用它判"后端起来了没"。
- ``/readyz``：**如实**报告每个存储的实际状态（含后端名与错误）。
  就绪与否只看**业务真正依赖的存储**（当前是 PG）；redis 缺失只降级不判死，
  理由见 :func:`readyz` 里的注释。契约要求的 ``auth`` 字段照原样给出。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    pg = request.app.state.pg
    redis = request.app.state.redis

    # 存储初始化现在跑在后台 task 里（见 app/main.py::lifespan）：
    # 还没跑完就如实报 "starting"，而不是让 health_async() 去连一个尚未启动的实例。
    booting = not getattr(request.app.state, "storage_ready", True)
    if booting:
        payload: dict[str, Any] = {
            "status": "starting",
            "pg": False,
            "redis": False,
            "auth": settings.auth_mode(),
            "pg_enabled": settings.pg_enabled,
            "redis_enabled": settings.redis_enabled,
            "pg_backend": "",
            "pg_version": "",
            "pg_error": "storage bootstrap in progress",
            "redis_backend": "",
            "redis_error": "storage bootstrap in progress",
            "migration": None,
            "startup_ms": getattr(request.app.state, "startup_ms", 0.0),
            "errors": list(getattr(request.app.state, "storage_errors", [])),
        }
        return JSONResponse(payload, status_code=503)

    pg_health: dict[str, Any] = (
        await pg.health_async() if settings.pg_enabled else {"ok": False, "backend": "disabled", "error": "disabled"}
    )
    redis_health: dict[str, Any] = (
        await redis.health() if settings.redis_enabled else {"ok": False, "backend": "disabled", "error": "disabled"}
    )

    checks = [
        bool(pg_health.get("ok")) if settings.pg_enabled else True,
        bool(redis_health.get("ok")) if settings.redis_enabled else True,
    ]
    pg_ok, redis_ok = checks

    # 就绪判定只看**业务路由真正依赖的存储**，不按"所有组件都健康"来算。
    #
    # 为什么 redis 不能参与判定：desktop-local 的业务端点（chat / conversations /
    # agents / media / memory / knowledge）对 redis 零引用，全仓只有本文件读
    # ``app.state.redis``。redis 是给 python-engine（云端引擎）用的，本地这一层
    # 只是按契约如实**报告**它的情况。若让 redis 判死整体，壳层探 /readyz 就永远
    # 等不到就绪，用户看到的是"应用起不来"—— 比真实故障更误导。
    #
    # 但也不能假装没事：status 仍会降级成 ``degraded`` 并在 ``redis_error`` 里
    # 写明原因，HTTP 维持 200（业务可用）。"能用"和"一切正常"是两件事。
    ready = pg_ok
    degraded = redis_ok is False

    payload: dict[str, Any] = {
        "status": "ok" if ready and not degraded else "degraded",
        "pg": bool(pg_health.get("ok")),
        "redis": bool(redis_health.get("ok")),
        "auth": settings.auth_mode(),
        "pg_enabled": settings.pg_enabled,
        "redis_enabled": settings.redis_enabled,
        "pg_backend": pg_health.get("backend", ""),
        "pg_version": pg_health.get("server_version", ""),
        "pg_error": pg_health.get("error", ""),
        "redis_backend": redis_health.get("backend", ""),
        "redis_error": redis_health.get("error", ""),
        "migration": getattr(request.app.state, "migration", None),
        "startup_ms": getattr(request.app.state, "startup_ms", 0.0),
        "errors": list(getattr(request.app.state, "storage_errors", [])),
    }
    return JSONResponse(payload, status_code=200 if ready else 503)
