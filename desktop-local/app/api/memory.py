"""本地记忆层（最小闭环）：写入 / 列表 / 删除 / 关键词检索。

契约 §4 只固定了「只绑 loopback」与 ``{"data": ...}`` 包装，端点形状是本批约定：

===================================  ==========================================
方法 / 路径                           说明
===================================  ==========================================
``POST   /v1/memory/entries``         ``{content, title?, kind?, tags?, source?}`` → 201
``GET    /v1/memory/entries``         倒序列表（``limit`` / ``offset``）
``DELETE /v1/memory/entries/{id}``    删一条（不存在 → 404）
``POST   /v1/memory/search``          ``{query, limit?}``，ILIKE 关键词匹配
===================================  ==========================================

检索本批只做子串匹配（``ILIKE``，标题 / 正文 / 标签任一命中）：够最小闭环；
第二轮若要接全文索引或向量召回，替换 ``memory_store.search_entries`` 即可，接口形状不用动。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.storage import memory_store

# 只定义 router；注册到 api/__init__.py 由集成方统一做（本批约定）
router = APIRouter()


class MemoryEntryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=memory_store.MAX_CONTENT_CHARS)
    title: str = Field(default="", max_length=memory_store.MAX_TITLE_CHARS)
    kind: str = Field(default=memory_store.DEFAULT_KIND, max_length=memory_store.MAX_KIND_CHARS)
    tags: list[str] = Field(default_factory=list, max_length=memory_store.MAX_TAGS)
    source: str = Field(default="", max_length=memory_store.MAX_SOURCE_CHARS)


class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=memory_store.MAX_KEYWORD_CHARS)
    limit: int = Field(default=20, ge=1, le=200)


# --------------------------------------------------------------------- 连接
def _dsn(request: Request) -> str:
    """取本地 PG 的 DSN；存储没起来就 503。"""
    pg = getattr(request.app.state, "pg", None)
    dsn = getattr(pg, "dsn", "") or ""
    if not dsn:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="本地 PostgreSQL 未就绪（见 GET /readyz 的 pg_error）",
        )
    return dsn


@asynccontextmanager
async def _connection(request: Request) -> AsyncIterator[Any]:
    """一条连接 + 幂等建表，用完就关（本地单用户，本批不做连接池）。"""
    try:
        conn = await memory_store.connect(_dsn(request))
    except memory_store.MemoryStoreError as exc:
        # 连不上本地 PG 是"暂不可用"（503），不是 500
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    try:
        await memory_store.ensure_memory_schema(conn)
        yield conn
    finally:
        await conn.close()


# --------------------------------------------------------------------- 路由
@router.post("/v1/memory/entries", status_code=status.HTTP_201_CREATED)
async def create_entry(payload: MemoryEntryCreate, request: Request) -> dict[str, Any]:
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="content 不能为空白")

    entry_id = uuid.uuid4().hex
    async with _connection(request) as conn:
        row = await memory_store.insert_entry(
            conn,
            entry_id=entry_id,
            kind=payload.kind.strip() or memory_store.DEFAULT_KIND,
            title=payload.title.strip(),
            content=content,
            tags=memory_store.normalize_tags(payload.tags),
            source=payload.source.strip(),
        )
    return {"data": memory_store.serialize_entry(row)}


@router.get("/v1/memory/entries")
async def list_entries(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with _connection(request) as conn:
        rows = await memory_store.list_entries(conn, limit=limit, offset=offset)
    return {"data": [memory_store.serialize_entry(row) for row in rows]}


@router.delete("/v1/memory/entries/{entry_id}")
async def delete_entry(entry_id: str, request: Request) -> dict[str, Any]:
    async with _connection(request) as conn:
        deleted = await memory_store.delete_entry(conn, entry_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"记忆条目不存在：{entry_id}",
        )
    return {"data": {"id": entry_id, "deleted": True}}


@router.post("/v1/memory/search")
async def search_entries(payload: MemorySearchRequest, request: Request) -> dict[str, Any]:
    try:
        keyword = memory_store.clean_keyword(payload.query)
    except memory_store.MemoryStoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    async with _connection(request) as conn:
        rows = await memory_store.search_entries(conn, keyword, limit=payload.limit)
    return {"data": [memory_store.serialize_entry(row) for row in rows]}
