"""``/v1/models``（契约 §4，第一批为占位）。

本地模式的推理走 W4 的 loopback 推理代理，core 自己不持有 provider key，
因此这里只返回空列表 —— 不编造模型清单。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter()


@router.get("/v1/models")
async def list_models() -> dict[str, list[Any]]:
    return {"data": []}
