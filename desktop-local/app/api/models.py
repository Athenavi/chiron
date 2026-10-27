"""``/v1/models``（契约 §4）。

本地模式不持有 provider key，core 也**不知道**用户装了哪些模型：清单由 ``CHIRON_LOCAL_MODELS``
显式给出（JSON 数组），没配就是空列表——不编造模型名。

为什么用环境变量而不是 ``/v1/models`` 去探测 Ollama：契约 §3 把"谁能访问 11434"限定在
主进程的 ``inference-proxy.cjs``，core 只认代理地址；清单因此属于**配置**，不是发现结果。

``POST /v1/models/verify`` 是唯一会真的花 token 的端点：发一句 ``ping``、只要 16 个 token，
用来回答"这个模型到底通不通"。失败也在 ``data.ok == false`` 里如实返回，并带上 HTTP 状态码。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.auth import LOCAL_USER
from app.runtime.inference import (
    VERIFY_MAX_TOKENS,
    VERIFY_PROMPT,
    InferenceError,
    InferenceNotConfiguredError,
    ModelConfigError,
    inference_for,
    load_configured_models,
)

router = APIRouter()

#: verify 回显的上游文本上限（只用来肉眼确认"有输出"，不需要全文）
VERIFY_ECHO_CHARS = 200


class VerifyBody(BaseModel):
    """``POST /v1/models/verify``：不给 ``model`` 就用配置里的第一个。"""

    model: str | None = Field(default=None, max_length=200)


@router.get("/v1/models")
async def list_models() -> Any:
    """可用模型清单（来自 ``CHIRON_LOCAL_MODELS``，未配置时为空）。

    配置写错（非法 JSON / 缺 id）时回 500 + ``error``：把"你填错了"和"你没填"区分开。
    """
    try:
        models = load_configured_models()
    except ModelConfigError as exc:
        return JSONResponse({"data": [], "error": str(exc)}, status_code=500)
    return {"data": models}


@router.post("/v1/models/verify")
async def verify_model(request: Request, body: VerifyBody) -> Any:
    """调一次推理代理，确认模型可用。"""
    try:
        inference = inference_for(request.app.state)
    except InferenceNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    model = (body.model or "").strip()
    if not model:
        try:
            configured = load_configured_models()
        except ModelConfigError as exc:
            raise HTTPException(status_code=500, detail=f"模型配置错误：{exc}") from exc
        model = str(configured[0]["id"]) if configured else ""
    if not model:
        raise HTTPException(
            status_code=400,
            detail="缺少模型：请求体给 model，或用 CHIRON_LOCAL_MODELS 配置可用模型清单",
        )

    started = time.perf_counter()
    try:
        result = await inference.infer(
            model=model,
            messages=[{"role": "user", "content": VERIFY_PROMPT}],
            tenant_id=int(LOCAL_USER["tenant_id"]),
            user_id=str(LOCAL_USER["id"]),
            max_tokens=VERIFY_MAX_TOKENS,
            temperature=0.0,
        )
    except InferenceError as exc:
        return JSONResponse(
            {
                "data": {
                    "ok": False,
                    "model": model,
                    "error": str(exc),
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                }
            },
            status_code=exc.status_code,
        )

    text = str(result.get("text") or "")
    return {
        "data": {
            "ok": True,
            "model": str(result.get("model") or model),
            "provider": str(result.get("provider") or ""),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "usage": result.get("usage") or {},
            "usage_estimated": bool(result.get("usage_estimated")),
            "reply": text[:VERIFY_ECHO_CHARS],
        }
    }
