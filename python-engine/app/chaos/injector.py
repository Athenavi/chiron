"""混沌工程的**真实**注入通道（Python 引擎侧）。

## 分工

* **权威状态**：PostgreSQL 的 ``ent_chaos_experiments``（迁移 ``0002_ent_chaos_experiments``）
  —— 实验的创建/回滚/审计轨迹；
* **热路径**：**不查库**。Redis 的 ``chaos:active:{tenant_id}`` 承载活跃实验，
  TTL 很短且写入侧主动失效，跨副本一致（见 :func:`invalidate`）；
* **施加点**：本模块的 ASGI 中间件 —— 命中活跃故障就施加，否则原样放行。
  网关请求路径由 Go 侧的同类中间件负责（target=``gateway``）。

## 能力边界（诚实优先）

只实现两个作用面：

* ``engine``：本中间件施加（latency / error）；
* ``gateway``：由 Go 网关的中间件施加。

``llm`` / ``db`` / ``redis`` 需要在**各自的调用链**上插桩（LLM 网关、连接池），
目前**明确不支持** —— 创建这类实验会被拒绝，而不是"接受后什么都不做"。
``resource``（CPU/内存耗尽）同样不在中间件层面施加：那会影响整个进程，属专门工具，
不属请求路径注入。这正是本功能此前"501 + 假注入"的老问题，不能用新的假实现替代。

## 安全

* **默认关闭**：``CHAOS_ENABLED=false``（默认）时中间件直接放行、连 Redis 都不读；
* 注入生效时响应带 ``X-Chaos-Injected`` 头，便于把"注入的故障"与真故障区分开；
* 鉴权/授权由网关把关（``RequireEntPerm("chaos:manage")``），本模块不判权。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from app.config import settings
from app.redis_keys import rkey

logger = logging.getLogger(__name__)

#: 中间件层面支持的作用面与故障类型。
SUPPORTED_TARGETS = frozenset({"engine", "gateway"})
MIDDLEWARE_FAULT_TYPES = frozenset({"latency", "error", "timeout"})

#: 单次注入的时长上限（毫秒）—— 防止一个实验把请求挂死到连接超时。
MAX_INJECT_MS = 10_000

#: 活跃实验缓存的 TTL（秒）。短 TTL + 写入侧主动失效 = 跨副本一致且热路径不查库。
ACTIVE_TTL_SECONDS = 5


def unsupported_reason(fault_type: str, target: str) -> str | None:
    """返回"该实验无法被施加"的原因；可为空表示支持。"""
    if target not in SUPPORTED_TARGETS:
        return (
            f"target {target!r} is not wired for injection (supported: "
            f"{', '.join(sorted(SUPPORTED_TARGETS))}); "
            "llm/db/redis need instrumentation on their own call paths"
        )
    if fault_type not in MIDDLEWARE_FAULT_TYPES:
        return (
            f"fault_type {fault_type!r} cannot be applied on the request path "
            f"(supported: {', '.join(sorted(MIDDLEWARE_FAULT_TYPES))}); "
            "'resource' belongs in a dedicated tool, not a middleware"
        )
    return None


def _active_key(tenant_id: str) -> str:
    return rkey(f"chaos:active:{tenant_id}")


async def load_active_faults(tenant_id: str) -> list[dict[str, Any]]:
    """读取该租户的活跃故障：Redis 优先，未命中则查库并回填（TTL 很短）。

    任何一步失败都**退回空列表**（即不注入）—— 混沌工程自身故障时绝不能反过来
    把正常请求打挂。
    """
    if not settings.chaos_enabled or not tenant_id:
        return []

    key = _active_key(tenant_id)
    try:
        from app.redis_client import get_redis

        redis = await get_redis()
        cached = await redis.get(key)
        if cached:
            parsed: list[dict[str, Any]] = json.loads(cached)
            return parsed
    except Exception as e:  # noqa: BLE001 — 缓存不可用不影响主流程
        logger.debug("chaos: redis read failed (%s), falling back to db", e)

    try:
        from app.db import get_pool

        pool = get_pool()
        rows = await pool.fetch(
            """
            SELECT id, fault_type, target, duration_ms, intensity, config
              FROM ent_chaos_experiments
             WHERE tenant_id = $1 AND status IN ('pending', 'running')
            """,
            tenant_id,
        )
        faults = [
            {
                "id": r["id"],
                "fault_type": r["fault_type"],
                "target": r["target"],
                "duration_ms": r["duration_ms"],
                "intensity": r["intensity"],
                "config": json.loads(r["config"]) if isinstance(r["config"], str) else (r["config"] or {}),
            }
            for r in rows
        ]
    except Exception as e:  # noqa: BLE001 — 库不可用同样退回"不注入"
        logger.debug("chaos: db read failed (%s), not injecting", e)
        return []

    try:
        from app.redis_client import get_redis

        redis = await get_redis()
        await redis.set(key, json.dumps(faults), ex=ACTIVE_TTL_SECONDS)
    except Exception as e:  # noqa: BLE001
        logger.debug("chaos: redis backfill failed: %s", e)

    return faults


async def invalidate(tenant_id: str) -> None:
    """写入侧主动失效缓存（Go / Python 任何一方改了实验状态都应调用）。"""
    if not tenant_id:
        return
    try:
        from app.redis_client import get_redis

        redis = await get_redis()
        await redis.delete(_active_key(tenant_id))
    except Exception as e:  # noqa: BLE001
        logger.debug("chaos: cache invalidate failed: %s", e)


def pick_fault(faults: list[dict[str, Any]], target: str) -> dict[str, Any] | None:
    """挑一个作用于 ``target`` 的故障（多个命中时取第一个）。"""
    for f in faults:
        if f.get("target") == target and unsupported_reason(
            str(f.get("fault_type", "")), target
        ) is None:
            return f
    return None


def _tenant_id_from_scope(scope: dict[str, Any]) -> str:
    """从 ASGI scope 的 query string 取 tenant_id（引擎的身份约定，由网关注入）。"""
    from urllib.parse import parse_qs

    raw = scope.get("query_string", b"")
    if not raw:
        return ""
    try:
        params = parse_qs(raw.decode("latin-1"))
    except Exception:  # noqa: BLE001
        return ""
    values = params.get("tenant_id") or []
    return values[0] if values else ""


class ChaosInjectionMiddleware:
    """ASGI 中间件：命中活跃故障就施加 latency / error，否则原样放行。

    刻意用**纯 ASGI** 而不是 ``BaseHTTPMiddleware``：后者会包装 ``send``，
    对 SSE 流式响应可能引入缓冲 —— 引擎的对话与事件流不能因此变慢或被截断。
    注入 error 时**直接回响应、不调下游**，也不做任何 body 缓冲。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not settings.chaos_enabled:
            await self.app(scope, receive, send)
            return

        tenant_id = _tenant_id_from_scope(scope)
        if not tenant_id:
            await self.app(scope, receive, send)
            return

        try:
            faults = await load_active_faults(tenant_id)
            fault = pick_fault(faults, "engine")
        except Exception as e:  # noqa: BLE001 — 注入器自身故障绝不阻断请求
            logger.warning("chaos: injection check failed, passing through: %s", e)
            fault = None

        if fault is None:
            await self.app(scope, receive, send)
            return

        fault_type = fault["fault_type"]
        budget_ms = min(int(fault.get("duration_ms") or 0), MAX_INJECT_MS)
        intensity = float(fault.get("intensity") or 0.5)

        if fault_type == "error":
            await self._send_error(scope, send, fault)
            return

        # latency / timeout：先延迟再交给下游（timeout 按同一预算封顶，避免挂死连接）
        delay = budget_ms / 1000 * (intensity if fault_type == "latency" else 1.0)
        if delay > 0:
            logger.info(
                "chaos: injecting %s on %s for %.2fs (experiment=%s)",
                fault_type, scope.get("path"), delay, fault.get("id"),
            )
            await asyncio.sleep(delay)

        await self.app(scope, receive, send)

    @staticmethod
    async def _send_error(scope: Any, send: Any, fault: dict[str, Any]) -> None:
        code = int(fault.get("config", {}).get("error_code", 503))
        # 与 Go 侧 chaosErrorCode 对齐：非 4xx/5xx 的取值一律回退 503。
        # 否则实验配置里一个笔误（如 999）就会让 ASGI 发出发不出去的状态码。
        if not 400 <= code <= 599:
            code = 503
        logger.info(
            "chaos: injecting error %d on %s (experiment=%s)",
            code, scope.get("path"), fault.get("id"),
        )
        body = json.dumps(
            {
                "detail": "chaos experiment injected this failure",
                "chaos_experiment_id": fault.get("id"),
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"x-chaos-injected", str(fault.get("id", "")).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def now_ms() -> float:
    """单调时钟毫秒（供测试断言延迟量级）。"""
    return time.monotonic() * 1000
