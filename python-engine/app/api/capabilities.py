"""能力注册中心 API — 六大工作台能力发现（互通的「发现」侧）。

- GET  /v1/capabilities            按工作台列出能力
- POST /v1/capabilities/search     语义搜索能力
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from app.core.capabilities import WorkstationType, get_registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["capabilities"])


def _serialize(cap) -> dict:
    return {
        "capability_id": cap.capability_id,
        "name": cap.name,
        "description": cap.description,
        "workstation_type": cap.workstation_type.value,
        "capability_type": cap.capability_type.value,
        "tags": cap.tags,
        "status": cap.status,
        "version": cap.version,
        "input_schema": [
            {
                "name": p.name,
                "type": p.type,
                "description": p.description,
                "required": p.required,
                "default": p.default,
            }
            for p in cap.input_schema
        ],
        "stats": {
            "call_count": cap.call_count,
            "success_rate": cap.success_rate,
            "avg_duration_ms": cap.avg_duration_ms,
        },
    }


@router.get("/v1/capabilities")
async def list_capabilities(workstation: str | None = None, tenant_id: str = ""):
    """列出能力（可按工作台过滤）；全局能力对所有租户可见"""
    reg = get_registry()
    if workstation:
        try:
            wst = WorkstationType(workstation)
        except ValueError:
            return {"success": False, "error": f"unknown workstation: {workstation}"}
        caps = await reg.list_by_workstation(wst, tenant_id)
    else:
        # 全量列出：聚合所有工作台
        caps = []
        for wst in WorkstationType:
            caps.extend(await reg.list_by_workstation(wst, tenant_id))

    return {
        "success": True,
        "count": len(caps),
        "capabilities": [_serialize(c) for c in caps],
    }


@router.post("/v1/capabilities/search")
async def search_capabilities(request: Request):
    """语义搜索能力（关键词/标签/描述匹配，按相关性排序）"""
    body = await request.json()
    query = str(body.get("query") or "")
    if not query.strip():
        return {"success": False, "error": "query is required"}

    tenant_id = str(body.get("tenant_id") or request.query_params.get("user_id") or "")
    reg = get_registry()
    caps = await reg.search(
        query=query,
        tenant_id=tenant_id,
        limit=int(body.get("limit", 10)),
    )
    return {
        "success": True,
        "query": query,
        "count": len(caps),
        "capabilities": [_serialize(c) for c in caps],
    }
