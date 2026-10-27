"""本地对话接口（契约 §4：``{"data": ...}`` 包装）。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | ``/v1/chat/submit`` | 提交一条消息；默认非流式，``?stream=true`` 或 body ``stream=true`` 走 SSE |
| GET | ``/v1/chat/sessions`` | 会话列表（按最近更新倒序） |
| GET | ``/v1/chat/sessions/{id}/messages`` | 某会话的消息（按写入顺序） |

本模块只定义 ``router``，注册由 ``app/api/__init__.py`` 统一接线（本批不自己 include）。

错误响应走 FastAPI 默认形状 ``{"detail": "..."}``：错误不该被塞进 ``data`` 里假装成功。
状态码映射集中在 :func:`_http_error`：404 会话/Agent 不存在、400 请求不合法、503 本地库或
推理代理不可用、502/504 代理出错或超时。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.auth import LOCAL_USER
from app.runtime.chat import (
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_SESSION_LIMIT,
    MAX_SESSION_LIMIT,
    AgentNotFoundError,
    ChatInputError,
    ChatTurn,
    ModelRequiredError,
    SessionNotFoundError,
    run_turn,
    stream_reply,
)
from app.runtime.inference import (
    InferenceError,
    InferenceNotConfiguredError,
    ModelConfigError,
    inference_for,
)
from app.runtime.store import StorageUnavailableError, store_for

router = APIRouter()

#: 消息上限：本地没有分片上传，200k 字符已经远超任何一次合理的粘贴
MAX_MESSAGE_CHARS = 200_000

#: 拉历史消息的默认/最大条数
DEFAULT_MESSAGE_LIMIT = 200
MAX_MESSAGE_LIMIT = 500

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 若前面还有反代（本地也可能是 Electron 侧转发），明确告诉它不要缓冲
    "X-Accel-Buffering": "no",
}


class ChatSubmitBody(BaseModel):
    """``POST /v1/chat/submit`` 的请求体。"""

    session_id: str | None = None
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    model: str | None = None
    agent_id: str | None = None
    stream: bool = False
    max_tokens: int | None = Field(default=None, ge=1, le=200_000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)


# --------------------------------------------------------------------- 内部
def _store(request: Request) -> Any:
    try:
        return store_for(request.app.state)
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _ready_store(request: Request) -> Any:
    """取 store 并确保表已建好（建表失败 == 本地库不可用 == 503）。"""
    store = _store(request)
    try:
        await store.ensure_schema()
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return store


def _http_error(exc: Exception) -> HTTPException:
    """把运行时的异常翻成 HTTP 状态码。认不出来就原样抛（交给 FastAPI 的 500）。"""
    if isinstance(exc, (SessionNotFoundError, AgentNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ModelRequiredError | ChatInputError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, ModelConfigError):
        return HTTPException(status_code=500, detail=f"模型配置错误：{exc}")
    if isinstance(exc, InferenceNotConfiguredError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, InferenceError):
        return HTTPException(status_code=exc.status_code, detail=str(exc))
    if isinstance(exc, StorageUnavailableError):
        return HTTPException(status_code=503, detail=str(exc))
    raise exc


def _payload(reply: Any) -> dict[str, Any]:
    """非流式响应体（契约：业务数据放 ``data``）。"""
    return {
        "data": {
            "session_id": reply.session_id,
            "message": reply.message,
            "usage": reply.usage,
            "usage_estimated": reply.usage_estimated,
            "model": reply.model,
            "agent_id": reply.agent_id,
            "history_count": reply.history_count,
        }
    }


# --------------------------------------------------------------------- 路由
@router.post("/v1/chat/submit")
async def submit(request: Request, body: ChatSubmitBody, stream: bool = False) -> Any:
    """提交一条消息。``?stream=true`` 或 body ``stream=true`` 时返回 ``text/event-stream``。"""
    store = await _ready_store(request)
    turn = ChatTurn(
        message=body.message,
        session_id=body.session_id,
        model=body.model,
        agent_id=body.agent_id,
        max_tokens=body.max_tokens,
        temperature=body.temperature,
    )
    try:
        inference = inference_for(request.app.state)
        reply = await run_turn(store, inference, turn)
    except (ChatInputError, ModelRequiredError, SessionNotFoundError, AgentNotFoundError) as exc:
        raise _http_error(exc) from exc
    except (ModelConfigError, InferenceError, StorageUnavailableError) as exc:
        raise _http_error(exc) from exc

    if stream or body.stream:
        return StreamingResponse(stream_reply(reply), media_type="text/event-stream", headers=SSE_HEADERS)
    return _payload(reply)


@router.get("/v1/chat/sessions")
async def list_sessions(
    request: Request,
    limit: int = DEFAULT_SESSION_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """会话列表。``limit`` 会被夹到 ``1..MAX_SESSION_LIMIT``，避免一次拉爆本地库。"""
    store = await _ready_store(request)
    size = max(1, min(int(limit), MAX_SESSION_LIMIT))
    start = max(0, int(offset))
    try:
        sessions = await store.list_sessions(limit=size, offset=start)
    except StorageUnavailableError as exc:
        raise _http_error(exc) from exc
    return {"data": sessions}


@router.get("/v1/chat/sessions/{session_id}/messages")
async def list_messages(
    request: Request,
    session_id: str,
    limit: int = DEFAULT_MESSAGE_LIMIT,
) -> dict[str, Any]:
    """某会话的消息；会话不存在回 404（而不是空列表——空列表会被误读成"没说过话"）。"""
    store = await _ready_store(request)
    size = max(1, min(int(limit), MAX_MESSAGE_LIMIT))
    try:
        session = await store.get_session(session_id)
        if session is None:
            raise SessionNotFoundError(f"会话不存在：{session_id}")
        messages = await store.list_messages(session_id=session_id, limit=size)
    except (SessionNotFoundError, StorageUnavailableError) as exc:
        raise _http_error(exc) from exc
    return {"data": messages, "session": session}


#: 供诊断/测试引用的本地用户（与 ``/v1/auth/session`` 同一份）
LOCAL_TENANT_ID = int(LOCAL_USER["tenant_id"])
LOCAL_USER_ID = str(LOCAL_USER["id"])

#: ``GET /v1/chat/sessions`` 的默认窗口（与 runtime 保持一致，便于前端猜默认值）
SESSION_DEFAULT_LIMIT = DEFAULT_SESSION_LIMIT
SESSION_MAX_LIMIT = MAX_SESSION_LIMIT
HISTORY_DEFAULT_LIMIT = DEFAULT_HISTORY_LIMIT
