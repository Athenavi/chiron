"""本地用户会话（契约 §4）。

本地模式下没有账号体系：任何人能连上 loopback 后端就已经是"本机主人"，因此
``POST /v1/auth/session`` 不需要口令，返回**设备 token** + 固定本地用户。

token 落在 ``<data-dir>/auth/device-token``（重启后保持不变，Electron 侧可以只发一次请求
拿到长期 token）。

鉴权：本地模式**没有账号体系**，所以这是设备身份而不是用户身份。

- ``auth_enabled=False``（默认）：token 只是本机标识，``/readyz`` 报 ``auth: "disabled"``，
  **不校验**，谁都能调所有端点；
- ``auth_enabled=True``（``CHIRON_LOCAL_AUTH_ENABLED=1``）：``DeviceTokenMiddleware`` 校验
  ``Authorization: Bearer <这个 token>``，而 ``POST /v1/auth/session`` 本身**保持公开**
  （在 ``app.middleware.EXEMPT_PATHS`` 里）——否则“要 token 才能拿 token”就是死循环。

  公开换 token 是否等于鉴权无效？在本机 loopback 威胁模型下是的：能调这个端点的进程
  本就能读 ``<data-dir>/auth/device-token``。它挡的是**浏览器里其它源/其它页面的跨源
  请求**（它们没有文件系统权限），不是同机进程。所以别把它当多用户隔离来用。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()

# 本地单租户：tenant_id 固定 1（与 Go 网关的 tenant 语义对齐的最小占位）
LOCAL_USER: dict[str, Any] = {
    "id": "local",
    "name": "Local",
    "role": "owner",
    "tenant_id": 1,
}


def load_or_create_token(path: Path) -> str:
    """读设备 token，不存在就生成一个（内容不是密钥材料，只是本机标识）。

    优先级：``CHIRON_DEVICE_TOKEN``（Electron 主进程建窗时下发的 token）> 落盘文件 >
    新生成。**必须优先用 env**：否则主进程注入的 token 与 core 自己生成的不一致，
    一旦启用鉴权，所有本地请求都会 401。env 存在时同步写盘，重启后仍一致。
    """
    env_token = (os.environ.get("CHIRON_DEVICE_TOKEN") or "").strip()
    if env_token:
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(env_token + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
        return env_token

    with contextlib.suppress(OSError):
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        # Windows 上基本无效（ACL 才管用），Linux/macOS 用来挡住同机其它用户
        os.chmod(path, 0o600)
    return token


@router.post("/v1/auth/session")
async def create_session(request: Request) -> dict[str, Any]:
    """换取本地会话（**免鉴权**，见模块 docstring）。"""
    settings = request.app.state.settings
    token = await asyncio.to_thread(load_or_create_token, settings.device_token_path)
    return {"data": {"token": token, "user": dict(LOCAL_USER)}}
