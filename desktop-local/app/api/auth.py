"""本地用户会话（契约 §4）。

本地模式下没有账号体系：任何人能连上 loopback 后端就已经是"本机主人"，因此
``POST /v1/auth/session`` 不需要口令，返回**设备 token** + 固定本地用户。

token 落在 ``<data-dir>/auth/device-token``（重启后保持不变，Electron 侧可以只发一次请求
拿到长期 token）。第一批 ``/readyz`` 仍报 ``auth: "disabled"``，即**不校验**这个 token。
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
    """读设备 token，不存在就生成一个（内容不是密钥材料，只是本机标识）。"""
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
    settings = request.app.state.settings
    token = await asyncio.to_thread(load_or_create_token, settings.device_token_path)
    return {"data": {"token": token, "user": dict(LOCAL_USER)}}
