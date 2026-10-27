"""本地媒体库（最小闭环）：上传 / 列表 / 详情 / 原始内容 / 删除。

契约 §4 只固定了「只绑 loopback」与 ``{"data": ...}`` 包装，端点形状是本批约定：

======================  =====================================================
方法 / 路径              说明
======================  =====================================================
``POST /v1/media/upload``  JSON ``{name, mime_type, content_base64}`` → 201 + 元数据
``GET  /v1/media``         倒序列表（``limit`` / ``offset``）
``GET  /v1/media/{id}``    元数据 + ``content_base64``（``include_content=false`` 可关）
``GET  /v1/media/{id}/content``  原始字节（渲染进程 ``<img src>`` 直接预览）
``DELETE /v1/media/{id}``  删记录 + 删目录
======================  =====================================================

两条硬约束：

- 上传**不用 multipart**：壳层/REST 客户端发 JSON base64，后端不引入 ``python-multipart``。
- 响应里**不出现任何本地绝对路径**（连 ``safe_name`` 也不给）：客户端只拿 id，
  路径只在本进程里拼，且由 ``files.resolve_media_path`` 复核不越界。
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.storage import files

# 只定义 router；注册到 api/__init__.py 由集成方统一做（本批约定）
router = APIRouter()


class MediaUploadRequest(BaseModel):
    """上传体：JSON base64（不引 multipart）。"""

    name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(default=files.DEFAULT_MIME_TYPE, max_length=255)
    content_base64: str = Field(min_length=1)


# --------------------------------------------------------------------- 连接
def _dsn(request: Request) -> str:
    """取本地 PG 的 DSN；存储没起来就 503（比 500 更能表达"本地模式暂不可用"）。"""
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
    conn = await files.connect(_dsn(request))
    try:
        await files.ensure_media_schema(conn)
        yield conn
    finally:
        await conn.close()


def _not_found(media_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"媒体不存在：{media_id}",
    )


def _file_missing(media_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"媒体文件不在磁盘上：{media_id}",
    )


# --------------------------------------------------------------------- 路由
@router.post("/v1/media/upload", status_code=status.HTTP_201_CREATED)
async def upload_media(payload: MediaUploadRequest, request: Request) -> dict[str, Any]:
    _dsn(request)  # 存储不可用时先 503，别写了盘再报错
    settings = request.app.state.settings
    try:
        safe = files.safe_name(payload.name)
        data = files.decode_base64(payload.content_base64)
    except files.MediaError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    media_id = files.new_media_id()
    mime_type = files.normalize_mime_type(payload.mime_type)
    await asyncio.to_thread(files.write_media_bytes, settings, media_id, safe, data)
    try:
        async with _connection(request) as conn:
            row = await files.insert_media(
                conn,
                media_id=media_id,
                name=payload.name.strip() or safe,
                safe_name=safe,
                mime_type=mime_type,
                size_bytes=len(data),
                sha256=files.sha256_of(data),
            )
    except BaseException:
        # 记录没落库，就别留孤儿文件
        await asyncio.to_thread(files.delete_media_dir, settings, media_id)
        raise
    return {"data": files.serialize_media(row)}


@router.get("/v1/media")
async def list_media(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with _connection(request) as conn:
        rows = await files.list_media_rows(conn, limit=limit, offset=offset)
    return {"data": [files.serialize_media(row) for row in rows]}


@router.get("/v1/media/{media_id}")
async def get_media(
    media_id: str,
    request: Request,
    include_content: bool = True,
) -> dict[str, Any]:
    settings = request.app.state.settings
    async with _connection(request) as conn:
        row = await files.get_media_row(conn, media_id)
    if row is None:
        raise _not_found(media_id)

    payload = files.serialize_media(row)
    payload["content_base64"] = None
    if include_content:
        try:
            data = await asyncio.to_thread(files.read_media_bytes, settings, media_id, str(row["safe_name"]))
        except (OSError, files.MediaError) as exc:
            raise _file_missing(media_id) from exc
        payload["content_base64"] = base64.b64encode(data).decode("ascii")
    return {"data": payload}


@router.get("/v1/media/{media_id}/content")
async def download_media(media_id: str, request: Request) -> Response:
    """原始字节，供渲染进程直接预览（避免前端先 base64 再转 Blob）。"""
    settings = request.app.state.settings
    async with _connection(request) as conn:
        row = await files.get_media_row(conn, media_id)
    if row is None:
        raise _not_found(media_id)
    try:
        data = await asyncio.to_thread(files.read_media_bytes, settings, media_id, str(row["safe_name"]))
    except (OSError, files.MediaError) as exc:
        raise _file_missing(media_id) from exc

    filename = quote(str(row["name"]), safe="")
    return Response(
        content=data,
        media_type=str(row["mime_type"]),
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{filename}"},
    )


@router.delete("/v1/media/{media_id}")
async def delete_media(media_id: str, request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    async with _connection(request) as conn:
        row = await files.delete_media_row(conn, media_id)
    if row is None:
        raise _not_found(media_id)
    # 记录已删：文件删失败也不回滚（ignore_errors，避免留半截状态）
    await asyncio.to_thread(files.delete_media_dir, settings, media_id)
    return {"data": {"id": media_id, "deleted": True}}
