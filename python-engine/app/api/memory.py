"""记忆管理 API — L2 档案卡（用户长期记忆）。

路由（经 Go 网关 newProxy 代理，自动追加 ?user_id=<claims.UserID>）：
- GET    /v1/memory/profile                 整卡列表（按槽位分组统计）
- POST   /v1/memory/profile                 新建/更新条目 {slot, key, value, confidence?, source?}
- PUT    /v1/memory/profile                 编辑条目 {id, key?, value?, confidence?, source?}
- DELETE /v1/memory/profile/{id}            删除单条
- POST   /v1/memory/profile/clear           清空全部记忆 {confirm: true}
- POST   /v1/memory/search                  语义检索（相似度 + 重排序）
- POST   /v1/memory/organize                触发异步智能整理（去重/归档/补嵌入）
- GET    /v1/memory/organize/status         整理任务状态
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.memory.layers import SLOT_LABELS, SLOTS
from app.memory.service import get_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["memory"])


def _scope(request: Request) -> tuple[str, str]:
    """从网关注入的 query 参数解析 (tenant_id, user_id)。"""
    user_id = (request.query_params.get("user_id") or "").strip()
    tenant_id = (request.query_params.get("tenant_id") or "default").strip()
    return tenant_id, user_id


def _unavailable() -> JSONResponse:
    return JSONResponse(
        {"success": False, "error": "memory service unavailable (PostgreSQL required)"},
        status_code=503,
    )


def _bad_request(msg: str) -> JSONResponse:
    return JSONResponse({"success": False, "error": msg}, status_code=400)


@router.get("/v1/memory/profile")
async def list_profile(request: Request) -> Any:
    svc = get_service()
    if svc is None:
        return _unavailable()
    tenant_id, user_id = _scope(request)
    if not user_id:
        return _bad_request("user_id is required")
    include_archived = request.query_params.get("archived") == "true"
    data = await svc.list_entries(tenant_id, user_id, include_archived=include_archived)
    return {
        "success": True,
        **data,
        "slots": [{"slot": s, "label": SLOT_LABELS[s]} for s in SLOTS],
        "organize": svc.organize_status(tenant_id, user_id),
    }


@router.post("/v1/memory/profile")
async def upsert_profile(request: Request) -> Any:
    svc = get_service()
    if svc is None:
        return _unavailable()
    tenant_id, user_id = _scope(request)
    if not user_id:
        return _bad_request("user_id is required")
    body = await request.json()
    try:
        confidence = int(body.get("confidence", 50))
        confidence = max(0, min(100, confidence))
        result = await svc.upsert(
            tenant_id=tenant_id,
            user_id=user_id,
            slot=str(body.get("slot") or "fact"),
            key=str(body.get("key") or ""),
            value=str(body.get("value") or ""),
            confidence=confidence,
            source=str(body.get("source") or "user_confirmed"),
        )
    except ValueError as e:
        return _bad_request(str(e))
    return {"success": True, **result}


@router.put("/v1/memory/profile")
async def update_profile(request: Request) -> Any:
    svc = get_service()
    if svc is None:
        return _unavailable()
    tenant_id, user_id = _scope(request)
    if not user_id:
        return _bad_request("user_id is required")
    body = await request.json()
    entry_id = str(body.get("id") or "")
    if not entry_id:
        return _bad_request("id is required")
    try:
        confidence = body.get("confidence")
        if confidence is not None:
            confidence = max(0, min(100, int(confidence)))
        entry = await svc.update_entry(
            tenant_id,
            user_id,
            entry_id,
            key=str(body["key"]) if body.get("key") is not None else None,
            value=str(body["value"]) if body.get("value") is not None else None,
            confidence=int(confidence) if confidence is not None else None,
            source=str(body["source"]) if body.get("source") else None,
        )
    except ValueError as e:
        return _bad_request(str(e))
    if entry is None:
        return JSONResponse(
            {"success": False, "error": "entry not found"}, status_code=404
        )
    return {"success": True, "entry": entry.to_dict()}


@router.delete("/v1/memory/profile/{entry_id}")
async def delete_profile(entry_id: str, request: Request) -> Any:
    svc = get_service()
    if svc is None:
        return _unavailable()
    tenant_id, user_id = _scope(request)
    if not user_id:
        return _bad_request("user_id is required")
    deleted = await svc.delete_entry(tenant_id, user_id, entry_id)
    if not deleted:
        return JSONResponse(
            {"success": False, "error": "entry not found"}, status_code=404
        )
    return {"success": True, "deleted": entry_id}


@router.post("/v1/memory/profile/clear")
async def clear_profile(request: Request) -> Any:
    """清空当前用户全部记忆（隐私出口）。要求 body {confirm: true} 防误触。"""
    svc = get_service()
    if svc is None:
        return _unavailable()
    tenant_id, user_id = _scope(request)
    if not user_id:
        return _bad_request("user_id is required")
    body = await request.json()
    if not body.get("confirm"):
        return _bad_request("confirm=true is required to clear all memories")
    count = await svc.clear_all(tenant_id, user_id)
    return {"success": True, "deleted": count}


@router.post("/v1/memory/search")
async def search_memory(request: Request) -> Any:
    svc = get_service()
    if svc is None:
        return _unavailable()
    tenant_id, user_id = _scope(request)
    if not user_id:
        return _bad_request("user_id is required")
    body = await request.json()
    try:
        data = await svc.search(
            tenant_id,
            user_id,
            query=str(body.get("query") or ""),
            top_k=int(body.get("top_k", 10)),
            slot=str(body["slot"]) if body.get("slot") else None,
        )
    except ValueError as e:
        return _bad_request(str(e))
    return {"success": True, **data}


@router.post("/v1/memory/organize")
async def organize_memory(request: Request) -> Any:
    svc = get_service()
    if svc is None:
        return _unavailable()
    tenant_id, user_id = _scope(request)
    if not user_id:
        return _bad_request("user_id is required")
    result = await svc.start_organize(tenant_id, user_id)
    return {
        "success": True,
        **result,
        "status": svc.organize_status(tenant_id, user_id),
    }


@router.get("/v1/memory/organize/status")
async def organize_status(request: Request) -> Any:
    svc = get_service()
    if svc is None:
        return _unavailable()
    tenant_id, user_id = _scope(request)
    if not user_id:
        return _bad_request("user_id is required")
    return {"success": True, "status": svc.organize_status(tenant_id, user_id)}


# ── L3 摘要管理 ──────────────────────────────────────


@router.get("/v1/memory/summaries")
async def list_summaries(request: Request) -> Any:
    """列出摘要记忆（管理端审计）。"""
    svc = get_service()
    if svc is None:
        return _unavailable()
    tenant_id, user_id = _scope(request)
    if not user_id:
        return _bad_request("user_id is required")
    limit = int(request.query_params.get("limit", "50") or 50)
    # S 修复：限制上限，避免非法/超大 limit 导致 DB 无界查询
    limit = min(max(limit, 1), 200)
    data = await svc.list_summaries(tenant_id, user_id, limit)
    return {"success": True, **data}


# ── 冲突裁决 ─────────────────────────────────────────


@router.get("/v1/memory/conflicts")
async def list_conflicts(request: Request) -> Any:
    """列出待裁决的记忆冲突。"""
    svc = get_service()
    if svc is None:
        return _unavailable()
    tenant_id, user_id = _scope(request)
    if not user_id:
        return _bad_request("user_id is required")
    conflicts = await svc.list_conflicts_shared(tenant_id, user_id)
    return {"success": True, "conflicts": conflicts, "count": len(conflicts)}


@router.post("/v1/memory/conflicts/{conflict_id}/resolve")
async def resolve_conflict(conflict_id: str, request: Request) -> Any:
    """裁决冲突：keep_old / adopt_new（use_new 同义）/ manual。"""
    svc = get_service()
    if svc is None:
        return _unavailable()
    tenant_id, user_id = _scope(request)
    if not user_id:
        return _bad_request("user_id is required")
    body = await request.json()
    resolution = str(body.get("resolution") or "")
    manual_value = body.get("manual_value")
    # 裁决方式的合法性交给 MemoryService 判定（它同时兼容 adopt_new / use_new 两种
    # 命名）：在路由层再维护一份白名单，只会让两处口径迟早分叉。
    try:
        result = await svc.resolve_conflict(conflict_id, resolution, manual_value)
    except ValueError as e:
        # 冲突不存在 → 404，裁决方式非法 → 400。两者的 ValueError 语义不同，
        # 一律返回 404 会让前端把「参数打错」误判成「冲突已不存在」。
        msg = str(e)
        status = 404 if "not found" in msg else 400
        return JSONResponse(
            status_code=status, content={"success": False, "error": msg}
        )
    return {"success": True, "conflict": result}


@router.delete("/v1/memory/conflicts/{conflict_id}")
async def delete_conflict(conflict_id: str, request: Request) -> Any:
    """删除冲突（用户否认时调用）。"""
    svc = get_service()
    if svc is None:
        return _unavailable()
    tenant_id, user_id = _scope(request)
    if not user_id:
        return _bad_request("user_id is required")
    deleted = await svc.delete_conflict(conflict_id)
    if not deleted:
        return JSONResponse(
            {"success": False, "error": "conflict not found"}, status_code=404
        )
    return {"success": True, "deleted": conflict_id}
