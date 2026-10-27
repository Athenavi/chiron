"""本地「会话」API（会话地图的 ``adapter.js`` 只认这个形状）。

为什么需要它：会话地图的数据面写死了两族端点 —— ``/v1/session-map/*``（布局）与
``/v1/conversations``（会话本体）。后者此前**只存在于 Go 网关**，本地模式下 404，
地图因此拿不到任何卡片（真机探针：``conversations: 404``）。

本地已有 ``/v1/chat/sessions``，但形状不同（``{data:[...]}/{data:[...],session}``），
而 ``adapter.js`` 要的是 ``{data:{title,tag,alias,messages:[{role,content,created_at}]}}``。
这里做一层薄适配，**不重复实现存储** —— 数据仍走 ``LocalStore``。

与云端差异：本地没有 ``tag`` / ``alias`` / 分支血缘（``parent_title`` /
``branch_from_seq``）。字段仍然返回（空值），让地图的既有渲染分支不用改。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.runtime.store import StorageUnavailableError, store_for

router = APIRouter()

MAX_PER_PAGE = 100
DEFAULT_PER_PAGE = 30


def _store(request: Request) -> Any:
    """``store_for`` 要的是 ``app.state``（它从那里的 ``pg.dsn`` 取地址），不是 settings。"""
    try:
        return store_for(request.app.state)
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _ready_store(request: Request) -> Any:
    store = _store(request)
    try:
        await store.ensure_schema()
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return store


def _conversation(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": session.get("id", ""),
        "title": session.get("title", ""),
        "model": session.get("model", ""),
        "agent_id": session.get("agent_id") or "",
        "message_count": int(session.get("message_count") or 0),
        "created_at": session.get("created_at", ""),
        "updated_at": session.get("updated_at", ""),
        # 云端才有的字段：本地返回空值，保持地图的渲染分支不变
        "tag": "",
        "alias": "",
        "parent_title": "",
        "branch_from_seq": 0,
    }


@router.get("/v1/conversations")
async def list_conversations(
    request: Request,
    per_page: int = DEFAULT_PER_PAGE,
    page: int = 1,
) -> dict[str, Any]:
    """会话列表（最近更新倒序）。``per_page`` 夹到 ``1..100``。"""
    store = await _ready_store(request)
    size = max(1, min(int(per_page), MAX_PER_PAGE))
    page_no = max(1, int(page))
    try:
        sessions = await store.list_sessions(limit=size, offset=(page_no - 1) * size)
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"data": [_conversation(s) for s in sessions]}


@router.get("/v1/conversations/{conversation_id}")
async def get_conversation(
    request: Request,
    conversation_id: str,
    limit: int = 200,
) -> dict[str, Any]:
    """单个会话 + 消息。

    地图的 ``loadMessages`` 读 ``data.messages``，消息字段只认
    ``{id, role, content, created_at}`` —— 本地存储正好是这个形状，原样透传。
    """
    store = await _ready_store(request)
    size = max(1, min(int(limit), 500))
    try:
        session = await store.get_session(conversation_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"会话不存在：{conversation_id}")
        messages = await store.list_messages(session_id=conversation_id, limit=size)
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    payload = _conversation(session)
    payload["messages"] = messages
    return {"data": payload}


@router.delete("/v1/conversations/{conversation_id}")
async def delete_conversation(request: Request, conversation_id: str) -> dict[str, Any]:
    """删除会话。

    本地 ``LocalStore`` 目前没有 ``delete_session``；地图的删除按钮走宿主 RPC
    （``sessionmapBridge``），这里是兜底：明确回 501 而不是假装删掉。
    """
    raise HTTPException(
        status_code=501,
        detail="本地模式暂不支持直接删除会话；请在对话页操作（地图的删除按钮由宿主代理）",
    )


@router.post("/v1/conversations/{conversation_id}/fork")
async def fork_conversation(request: Request, conversation_id: str) -> dict[str, Any]:
    """分叉会话：由宿主（ChatView RPC）执行，这里只回答"未实现"。"""
    raise HTTPException(
        status_code=501,
        detail="本地模式的分叉由宿主代理执行（sessionmapBridge），不经过此端点",
    )


#: 供诊断引用
CONVERSATIONS_MAX_PER_PAGE = MAX_PER_PAGE
