"""本地 Agent 的 CRUD（契约 §4：``{"data": ...}`` 包装）。

Agent 在本地就是"一段系统提示 + 可选的默认模型"：没有工具执行器、没有编排循环，
所以字段刻意保持最少（``name`` / ``description`` / ``system_prompt`` / ``model`` /
``tools`` / ``enabled``）。``tools`` 只是**声明**，由后续批次接工具执行时再赋予语义——
现在既不校验工具名，也不会执行它们。

本模块只定义 ``router``，注册由 ``app/api/__init__.py`` 统一接线（本批不自己 include）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.runtime.store import StorageUnavailableError, store_for

router = APIRouter()

MAX_NAME_CHARS = 120
MAX_PROMPT_CHARS = 32_000
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 500


class AgentCreateBody(BaseModel):
    """``POST /v1/agents`` 的请求体。"""

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    description: str = Field(default="", max_length=2_000)
    system_prompt: str = Field(default="", max_length=MAX_PROMPT_CHARS)
    model: str = Field(default="", max_length=200)
    tools: list[str] = Field(default_factory=list)
    enabled: bool = True


class AgentUpdateBody(BaseModel):
    """``PATCH /v1/agents/{id}`` 的请求体：全部可选，只改传了的字段。"""

    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_CHARS)
    description: str | None = Field(default=None, max_length=2_000)
    system_prompt: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    model: str | None = Field(default=None, max_length=200)
    tools: list[str] | None = None
    enabled: bool | None = None


# --------------------------------------------------------------------- 内部
async def _ready_store(request: Request) -> Any:
    try:
        store = store_for(request.app.state)
        await store.ensure_schema()
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return store


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, StorageUnavailableError):
        return HTTPException(status_code=503, detail=str(exc))
    raise exc


# --------------------------------------------------------------------- 路由
@router.get("/v1/agents")
async def list_agents(
    request: Request,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    store = await _ready_store(request)
    size = max(1, min(int(limit), MAX_LIST_LIMIT))
    start = max(0, int(offset))
    try:
        agents = await store.list_agents(limit=size, offset=start)
    except StorageUnavailableError as exc:
        raise _http_error(exc) from exc
    return {"data": agents}


@router.post("/v1/agents", status_code=201)
async def create_agent(request: Request, body: AgentCreateBody) -> dict[str, Any]:
    store = await _ready_store(request)
    try:
        agent = await store.create_agent(
            name=body.name,
            description=body.description,
            system_prompt=body.system_prompt,
            model=body.model,
            tools=body.tools,
            enabled=body.enabled,
        )
    except StorageUnavailableError as exc:
        raise _http_error(exc) from exc
    return {"data": agent}


@router.get("/v1/agents/{agent_id}")
async def get_agent(request: Request, agent_id: str) -> dict[str, Any]:
    store = await _ready_store(request)
    try:
        agent = await store.get_agent(agent_id)
    except StorageUnavailableError as exc:
        raise _http_error(exc) from exc
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent 不存在：{agent_id}")
    return {"data": agent}


@router.patch("/v1/agents/{agent_id}")
async def update_agent(request: Request, agent_id: str, body: AgentUpdateBody) -> dict[str, Any]:
    store = await _ready_store(request)
    fields = body.model_dump(exclude_unset=True, exclude_none=True)
    try:
        agent = await store.update_agent(agent_id, **fields)
    except StorageUnavailableError as exc:
        raise _http_error(exc) from exc
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent 不存在：{agent_id}")
    return {"data": agent}


@router.delete("/v1/agents/{agent_id}")
async def delete_agent(request: Request, agent_id: str) -> dict[str, Any]:
    store = await _ready_store(request)
    try:
        deleted = await store.delete_agent(agent_id)
    except StorageUnavailableError as exc:
        raise _http_error(exc) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Agent 不存在：{agent_id}")
    return {"data": {"id": agent_id, "deleted": True}}
