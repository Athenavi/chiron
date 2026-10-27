"""``/healthz`` 与 ``/readyz``（契约 §4）。

- ``/healthz``：进程活着就 200，不碰存储。Electron 用它判"后端起来了没"。
- ``/readyz``：**如实**报告每个存储的实际状态（含后端名与错误），一项不健康就 503。
  契约要求的 ``auth`` 字段照原样给出（第一批是 ``disabled``）。
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
    ready = all(checks)

    payload: dict[str, Any] = {
        "status": "ok" if ready else "degraded",
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
