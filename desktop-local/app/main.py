"""本地 core FastAPI 应用（契约 §4）。

设计要点：

- 只绑 ``127.0.0.1``（默认），端口 ``0`` 表示由内核随机分配；真实监听端口以 stdout 上那行
  ``{"event": "listening", ...}`` 为准（Electron 主进程按行解析，不要去猜）。
- 存储在 ``lifespan`` 里初始化。**存储起不来不阻断进程**：``/healthz`` 依然 200，
  ``/readyz`` 如实返回 503 + 逐条错误，这样壳层能显示"本地模式不可用"而不是"应用打不开"。
- 第一批没有任何业务路由（对话 / Agent / 知识库都不在本批）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import (
    agents,
    auth,
    chat,
    components,
    conversations,
    events,
    health,
    knowledge,
    media,
    memory,
    models,
    plugins,
    session_map,
)
from app.config import Settings
from app.middleware import DeviceTokenMiddleware
from app.storage.pg import PostgresServer
from app.storage.redis import RedisServer


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造应用。不在这里启动存储（交给 lifespan），方便测试换配置。"""
    cfg = settings or Settings()
    cfg.ensure_dirs()

    pg = PostgresServer(cfg)
    redis = RedisServer(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """**存储初始化不阻塞 uvicorn 启动**。

        为什么必须这样：uvicorn 的 ``server.started`` 只有在 lifespan startup 阶段
        返回后才置真，而 ``serve()`` 靠 ``server.started`` 才打印 listening 事件。
        早期版本在 lifespan 里同步等 PG（``pg_start_timeout`` 默认 60s），于是：

          冷启动慢 / 数据目录被残留 postmaster 占用 → PG 要等满 60s
          → listening 事件永远不出现 → Electron 30s 超时 → 用户看到"本地后端启动失败"

        现在 lifespan 立刻返回，存储放到后台 task：
          * uvicorn 立即监听、立即打印 listening（Electron 握手成功）；
          * ``/healthz`` 立刻 200；
          * ``/readyz`` 在初始化期间返回 503 + ``status: "starting"``（如实上报，不装就绪）；
          * 业务路由在 DSN 为空时给 503，而不是 500。
        """
        app.state.storage_errors = []
        app.state.storage_ready = False
        app.state.startup_ms = 0.0
        app.state.migration = None

        async def _bootstrap() -> None:
            started = time.perf_counter()
            try:
                if cfg.pg_enabled:
                    try:
                        # pg_ctl / embedded 包都是阻塞调用，扔到线程里免得卡住事件循环
                        await asyncio.to_thread(pg.start)
                        if cfg.run_migrations:
                            app.state.migration = await asyncio.to_thread(pg.migrate)
                    except Exception as exc:  # noqa: BLE001 —— 逐条记录，交给 /readyz 上报
                        app.state.storage_errors.append(f"pg: {type(exc).__name__}: {exc}")

                if cfg.redis_enabled:
                    try:
                        await redis.start()
                        if redis.error:
                            app.state.storage_errors.append(f"redis: {redis.error}")
                    except Exception as exc:  # noqa: BLE001
                        app.state.storage_errors.append(f"redis: {type(exc).__name__}: {exc}")
            finally:
                app.state.startup_ms = round((time.perf_counter() - started) * 1000, 1)
                # "初始化结束"≠"健康"：是否可用仍以 /readyz 的逐项探测为准
                app.state.storage_ready = True

        task = asyncio.create_task(_bootstrap(), name="chiron-local-bootstrap")
        app.state.bootstrap_task = task
        try:
            yield
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await redis.stop()
            await asyncio.to_thread(pg.stop)

    app = FastAPI(
        title="Chiron Local Core",
        version=__version__,
        summary="Chiron 桌面端本地后端（骨架 / P0）",
        lifespan=lifespan,
    )
    app.state.settings = cfg
    app.state.pg = pg
    app.state.redis = redis
    app.state.storage_errors = []
    app.state.migration = None
    app.state.startup_ms = 0.0

    # 设备 token 鉴权（契约 §4）：默认 auth_enabled=False，中间件直接放行；
    # 桌面端置 CHIRON_LOCAL_AUTH_ENABLED=1 后，除 /healthz、/readyz、/v1/auth/session
    # 外一律要求 `Authorization: Bearer <设备 token>`。配置从 app.state.settings 读，
    # 所以这里不传死一份，测试换配置也能生效。
    app.add_middleware(DeviceTokenMiddleware)

    # 会话地图是 iframe：打包版主窗口跑在 `file://`，iframe 里的 adapter.js 要跨源请求
    # `http://127.0.0.1:<port>`（Origin 头是字面量 "null"）。没有这层 CORS，
    # 浏览器会拦掉全部响应，地图依旧白屏 —— 即使本地路由已经补齐。
    #
    # 只放行本机来源：`null`（桌面壳的 file:// 页面 —— 浏览器把 opaque origin 序列化成
    # 字面量 "null"）与 `localhost/127.0.0.1` 的任意端口（开发态 vite dev server）。
    # 不接受 `*`：本地 core 的威胁模型是"别让浏览器里的**其它**站点读到用户数据"。
    # 代价说清楚：`null` 无法与"用户打开的本机恶意 HTML"区分，所以本地模式的隔离边界
    # 最终落在 Bearer token（`CHIRON_LOCAL_AUTH_ENABLED=1`）上，而不是 CORS。
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^(null|file://.*|https?://(localhost|127\.0\.0\.1)(:\d+)?)$",
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.include_router(health.router, tags=["health"])
    app.include_router(auth.router, tags=["auth"])
    app.include_router(models.router, tags=["models"])
    app.include_router(events.router, tags=["events"])
    # 第二批：本地业务能力。知识库默认禁用（CHIRON_LOCAL_KNOWLEDGE_ENABLED），
    # 端点会返回 403 knowledge_disabled，而不是静默空列表。
    app.include_router(chat.router, tags=["chat"])
    app.include_router(agents.router, tags=["agents"])
    app.include_router(media.router, tags=["media"])
    app.include_router(memory.router, tags=["memory"])
    app.include_router(knowledge.router, tags=["knowledge"])
    app.include_router(components.router, tags=["components"])
    # 插件：本地**没有**插件运行时，端点只回答"未实现"（空列表 + 501 + status.reason），
    # 但必须存在 —— 否则插件页会把 404 渲染成"加载失败"，把"没接入"说成"坏了"。
    app.include_router(plugins.router, tags=["plugins"])
    # 会话地图：iframe（sessionmap/adapter.js）只认这两族端点，缺一个地图就白屏。
    app.include_router(session_map.router, tags=["session-map"])
    app.include_router(conversations.router, tags=["conversations"])
    return app


# --------------------------------------------------------------------- CLI
def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="chiron-local",
        description="Chiron 桌面端本地 core 后端（骨架）",
    )
    parser.add_argument("--data-dir", default=None, help="数据目录（默认按平台选择）")
    parser.add_argument("--host", default=None, help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="监听端口；0 = 随机（默认）")
    parser.add_argument("--inference-proxy", default=None, help="loopback 推理代理地址")
    parser.add_argument("--log-level", default=None, help="uvicorn 日志级别，默认 info")
    parser.add_argument("--no-pg", action="store_true", help="不启动 embedded PostgreSQL")
    parser.add_argument("--no-redis", action="store_true", help="不启动 Redis 适配层")
    parser.add_argument("--no-migrate", action="store_true", help="不跑 alembic upgrade head")
    return parser.parse_args(argv)


def settings_from_args(args: argparse.Namespace) -> Settings:
    overrides: dict[str, Any] = {}
    if args.data_dir:
        overrides["data_dir"] = args.data_dir
    if args.host:
        overrides["host"] = args.host
    if args.port is not None:
        overrides["port"] = args.port
    if args.inference_proxy is not None:
        overrides["inference_proxy"] = args.inference_proxy
    if args.log_level:
        overrides["log_level"] = args.log_level
    if args.no_pg:
        overrides["pg_enabled"] = False
    if args.no_redis:
        overrides["redis_enabled"] = False
    if args.no_migrate:
        overrides["run_migrations"] = False
    return Settings(**overrides)


async def serve(settings: Settings) -> int:
    """起 uvicorn，并在**真正绑定成功之后**打印监听端口。"""
    from uvicorn import Config as UvicornConfig
    from uvicorn import Server

    app = create_app(settings)
    config = UvicornConfig(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    server = Server(config)
    task = asyncio.create_task(server.serve())

    while not server.started:
        if task.done():
            await task  # 让真实异常冒出来
            return 1
        await asyncio.sleep(0.02)

    bound_port = settings.port
    servers = getattr(server, "servers", None) or []
    for instance in servers:
        for sock in getattr(instance, "sockets", None) or []:
            bound_port = int(sock.getsockname()[1])
            break
        if bound_port != settings.port:
            break

    announce = {
        "event": "listening",
        "host": settings.host,
        "port": bound_port,
        "pid": os.getpid(),
        "version": __version__,
        "data_dir": str(settings.data_dir),
        "startup_ms": getattr(app.state, "startup_ms", 0.0),
        "storage_errors": list(getattr(app.state, "storage_errors", [])),
    }
    print(json.dumps(announce, ensure_ascii=False), flush=True)

    try:
        await task
    except asyncio.CancelledError:
        return 0
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = settings_from_args(args)
    settings.ensure_dirs()
    try:
        return asyncio.run(serve(settings))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
