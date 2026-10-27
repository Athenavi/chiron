"""``/events`` 服务端事件流（契约 §4，第一批只发心跳）。

用 SSE 而不是 WebSocket：Electron 渲染进程用原生 ``EventSource`` 就能连，断线自动重连，
第一批只需要"通道活着"的证据。业务事件（token 流、任务进度）第二批再加。

心跳间隔与条数上限来自配置：``CHIRON_LOCAL_SSE_HEARTBEAT_SECONDS`` /
``CHIRON_LOCAL_SSE_MAX_HEARTBEATS``（后者 0 = 无限；测试里设 1~2 让流自然结束）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.config import Settings

router = APIRouter()


async def _heartbeats(request: Request, settings: Settings, session_id: str) -> AsyncIterator[str]:
    sent = 0
    while True:
        if await request.is_disconnected():
            return
        payload = {
            "session_id": session_id,
            "seq": sent + 1,
            "ts": round(time.time(), 3),
        }
        yield f"event: heartbeat\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        sent += 1
        if settings.sse_max_heartbeats and sent >= settings.sse_max_heartbeats:
            return
        await asyncio.sleep(settings.sse_heartbeat_seconds)


@router.get("/events")
async def events(request: Request, session_id: str = "") -> StreamingResponse:
    settings: Settings = request.app.state.settings
    return StreamingResponse(
        _heartbeats(request, settings, session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 若有中间代理（W4 推理代理 / 未来的本地反代），别缓冲 SSE
            "X-Accel-Buffering": "no",
        },
    )
