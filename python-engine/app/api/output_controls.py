"""输出控制与提示精炼 API（差距B）。

两个端点，都是**用户可见**的：

* ``GET  /v1/agent/output-controls`` —— 可选风格 / 语言（**单一真相**：前端据此渲染选择器，
  不在前端硬编码枚举，避免"加了新风格前端看不见"）。
* ``POST /v1/agent/refine`` —— 把用户的粗略输入精炼成更明确的提示。**一次性、不自动应用**：
  由用户在前端显式点按触发，失败时原样保留用户输入。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/agent", tags=["agent-output"])


class RefineRequest(BaseModel):
    """精炼请求。"""

    content: str
    #: 使用的模型；空 = 引擎默认模型。
    model: str = ""


@router.get("/output-controls")
async def get_output_controls() -> dict[str, Any]:
    """返回输出控制的可选项（风格 / 语言）。"""
    from app.agent.output_controls import describe_controls

    return describe_controls()


@router.post("/refine")
async def refine_prompt_endpoint(body: RefineRequest) -> dict[str, Any]:
    """把用户输入精炼成更明确的提示。

    失败时返回 ``{"ok": false, "error": ...}`` —— **不抛出**，因为精炼失败不该让前端报错，
    前端只需保持原输入。
    """
    from app.agent.prompt_refine import refine_prompt
    from app.config import settings

    content = (body.content or "").strip()
    if not content:
        return {"ok": False, "error": "content is required"}

    try:
        # 延迟导入：app.main 体量很大且会 import 本包，模块级导入会成环。
        from app.main import get_gateway

        gateway = await get_gateway()
    except Exception as exc:  # noqa: BLE001 — 拿不到 gateway 时如实回报，不抛
        logger.warning("refine: gateway unavailable: %s", str(exc)[:160])
        return {"ok": False, "error": "gateway unavailable"}

    model = (body.model or "").strip() or settings.default_model
    result = await refine_prompt(gateway, raw=content, model=model)
    if not result.ok:
        return {"ok": False, "error": result.error}
    return {"ok": True, "refined": result.refined}
