"""本地插件路由（契约 §4 的**占位**实现）。

本地模式还没有插件运行时：Go 网关侧那套清单存储（``internal/api/plugin_handler.go``）、
运维命令白名单、逐用户一次性授权，在 core 里都不存在。但插件页是前端一级入口，
直接 404 会被渲染成"加载失败" —— 那是在说谎：真实原因是"还没接入"，不是"坏了"。

所以这里的立场是三条：

- **能力可见但为空**：``GET /v1/plugins`` 返回 ``{"data": []}``，页面显示"暂无插件"，
  而不是红色错误条；
- **状态如实**：``GET /v1/plugins/status`` 给出
  ``{"enabled": false, "reason": "local_plugins_not_implemented"}``，壳层据此显示
  "本地插件能力待接入"，不必靠猜；
- **写操作不假装成功**：``approve / install / test`` 一律 501 +
  ``{"error": "local_plugins_not_implemented"}``，绝不写入"已安装 / 已授权"的假记录。

唯一真能保存的是用户级总开关 ``commands_enabled``（``GET/PUT /v1/plugins/authorization``）。
它存在 ``app.state`` 里，**只活在本进程内**，重启即回默认 ``false``。这不会误导：
本地没有任何插件执行路径，这个开关现在不改任何行为，保留它只是让前端那个开关
有一个真实落点，而不是每次加载都报错。

本模块**只定义** ``router = APIRouter()``：注册由 :mod:`app.main` 负责，
测试也可以自己 ``include_router``。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()

#: 机器可读的原因码：接口存在、能力未实现。前端按它显示"待接入"而不是"出错"。
NOT_IMPLEMENTED = "local_plugins_not_implemented"

#: 用户级总开关在 ``app.state`` 上的键名（内存态，进程内有效）
AUTHORIZATION_KEY = "plugins_authorization"


class AuthorizationBody(BaseModel):
    """``PUT /v1/plugins/authorization`` 的请求体。

    ``commands_enabled`` 必填：给默认值等于让调用方"漏字段"时静默改状态。
    """

    commands_enabled: bool


def _authorization(request: Request) -> dict[str, Any]:
    """取（必要时初始化）本进程的授权状态。

    默认关：与 Go 侧一致 —— 没被用户确认过就不允许执行命令。
    """
    current = getattr(request.app.state, AUTHORIZATION_KEY, None)
    if not isinstance(current, dict):
        current = {"commands_enabled": False}
        setattr(request.app.state, AUTHORIZATION_KEY, current)
    return current


def _not_implemented() -> JSONResponse:
    """写操作的统一答复：501，且**只有**一个原因码，不带任何伪造的结果。"""
    return JSONResponse({"error": NOT_IMPLEMENTED}, status_code=501)


# --------------------------------------------------------------------- 列表 / 状态
@router.get("/v1/plugins")
async def list_plugins() -> dict[str, list[Any]]:
    """空列表：本地没有任何插件来源（没有安装目录、没有市场、没有内置清单）。"""
    return {"data": []}


@router.get("/v1/plugins/status")
async def plugins_status() -> dict[str, Any]:
    """插件能力本身的状态。

    ``enabled`` 是**能力**开关（不是用户授权开关）：本地永远 ``false``，
    直到真正实现插件运行时为止。
    """
    return {"data": {"enabled": False, "reason": NOT_IMPLEMENTED}}


# --------------------------------------------------------------------- 用户级授权
@router.get("/v1/plugins/authorization")
async def get_authorization(request: Request) -> dict[str, Any]:
    """当前用户的插件命令总开关（默认关闭）。"""
    return {"data": dict(_authorization(request))}


@router.put("/v1/plugins/authorization")
async def set_authorization(body: AuthorizationBody, request: Request) -> dict[str, Any]:
    """写总开关并回读当前值（前端以响应为准，不自己猜）。"""
    state = _authorization(request)
    state["commands_enabled"] = body.commands_enabled
    return {"data": {"commands_enabled": body.commands_enabled}}


# --------------------------------------------------------------------- 写操作（未实现）
@router.post("/v1/plugins/{name}/approve")
async def approve_plugin(name: str) -> JSONResponse:
    """逐插件一次性授权：本地无插件存储，无法记录"已授权"。"""
    return _not_implemented()


@router.post("/v1/plugins/{name}/install")
async def install_plugin(name: str) -> JSONResponse:
    """安装插件：本地没有来源、没有校验链，不写半成品记录。"""
    return _not_implemented()


@router.post("/v1/plugins/{name}/test")
async def test_plugin(name: str) -> JSONResponse:
    """试跑插件：没有执行器，就别说"测试通过"。"""
    return _not_implemented()
