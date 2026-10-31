"""本地组件 HTTP 路由（契约 §2 ``ComponentInfo`` 的服务端一侧）。

本模块**只定义** ``router = APIRouter()``：注册由调用方决定（本批不改
``app/api/__init__.py`` 与 ``app/main.py``），测试里自行 ``include_router``。

端点：

=========================================  ==================================================
``GET    /v1/components``                   列出受管组件 + 组件源状态
``POST   /v1/components/{name}/install``    从组件源安装（校验 sha256 / 拒绝路径穿越）
``POST   /v1/components/{name}/start``      启动组件，返回 ``{pid, baseUrl}``
``POST   /v1/components/{name}/stop``       停止组件（未在运行时 ``stopped: false``，不报错）
``DELETE /v1/components/{name}``            卸载（删除 ``<data-dir>/components/<name>``）
=========================================  ==================================================

失败一律 ``{"error": "<机器可读码>", "message": "<人话>", ...}``，状态码来自
:class:`app.components.manager.ComponentError` 的 ``status`` / ``code``。
未配置组件源时**安装**返回 409 ``component_source_unconfigured``（列组件仍 200，
好让前端能把"源未配置"和"组件未安装"分开提示）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.components import ComponentError, ComponentManager

router = APIRouter()


# --------------------------------------------------------------------- 工具
def manager_for(request: Request) -> ComponentManager:
    """取组件管理器：优先 ``app.state.component_manager``（测试/外部注入）。"""
    injected = getattr(request.app.state, "component_manager", None)
    if injected is not None:
        return injected
    manager = ComponentManager(getattr(request.app.state, "settings", None))
    # 监督线程随管理器一起起来：只有真正用到组件端点时才需要盯进程，
    # 从没碰过组件的 core 不该凭空多一个后台线程。
    manager.start_supervisor()
    request.app.state.component_manager = manager
    return manager


def _error_response(exc: ComponentError) -> JSONResponse:
    body: dict[str, Any] = {"error": exc.code, "message": str(exc)}
    body.update(exc.extra)
    return JSONResponse(body, status_code=exc.status)


# --------------------------------------------------------------------- 列表
@router.get("/v1/components")
async def list_components(request: Request) -> JSONResponse:
    """列出组件与组件源。core 自身不在列表里（它由 Electron 主进程拉起）。"""
    manager = manager_for(request)
    items = await asyncio.to_thread(manager.list)
    return JSONResponse({"data": items, "source": manager.source_summary()})


# --------------------------------------------------------------------- 安装
@router.post("/v1/components/{name}/install")
async def install_component(name: str, request: Request) -> JSONResponse:
    manager = manager_for(request)
    try:
        result = await asyncio.to_thread(manager.install, name)
    except ComponentError as exc:
        return _error_response(exc)
    return JSONResponse({"data": result}, status_code=201)


# --------------------------------------------------------------------- 启动 / 停止
@router.post("/v1/components/{name}/start")
async def start_component(name: str, request: Request) -> JSONResponse:
    manager = manager_for(request)
    try:
        result = await asyncio.to_thread(manager.start, name)
    except ComponentError as exc:
        return _error_response(exc)
    return JSONResponse({"data": result})


@router.post("/v1/components/{name}/stop")
async def stop_component(name: str, request: Request) -> JSONResponse:
    manager = manager_for(request)
    try:
        result = await asyncio.to_thread(manager.stop, name)
    except ComponentError as exc:
        return _error_response(exc)
    return JSONResponse({"data": result})


# --------------------------------------------------------------------- 卸载
@router.delete("/v1/components/{name}")
async def remove_component(name: str, request: Request) -> JSONResponse:
    manager = manager_for(request)
    try:
        result = await asyncio.to_thread(manager.remove, name)
    except ComponentError as exc:
        return _error_response(exc)
    return JSONResponse({"data": result})
