"""本地知识库 HTTP 路由（契约 §4 的扩展端点）。

本模块**只定义** ``router = APIRouter()``：注册由调用方决定（本批不改
``app/api/__init__.py`` 与 ``app/main.py``），测试里可以自行 ``include_router``。

端点：

===============================  ==========================================================
``POST   /v1/kb``                建库
``GET    /v1/kb``                列库
``DELETE /v1/kb/{id}``           删库（行 + ``<data-dir>/knowledge/<id>`` 目录）
``POST   /v1/kb/{id}/documents`` 上传文档（JSON base64，原文落盘 + 分块入库）
``POST   /v1/kb/{id}/query``     关键词检索 top-k，返回 ``{answer, citations}``
``POST   /v1/kb/{id}/build``     检查 pageindex 组件；没装 409，装了 202 占位
===============================  ==========================================================

失败一律是"明确的形状"：``{"error": "<机器可读码>", "message": "<人话>"}``，
不返回 200 + 空数据。
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.knowledge import (
    PgKnowledgeStore,
    StoreUnavailable,
    chunk_text,
    find_pageindex_dir,
    keywords,
    new_id,
    pageindex_status,
    search_payload,
)
from app.knowledge import is_enabled as knowledge_enabled
from app.knowledge.search import CANDIDATE_FACTOR

router = APIRouter()

#: 单个文档上限（解码后）。本地小库够用；再大应该走 P3 的抽取组件。
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024

#: 上传名的非法字符（路径分隔符、盘符冒号、控制字符等）
_BAD_NAME = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")
_MAX_NAME = 200


class CreateKbRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120, description="知识库名称")
    description: str = Field(default="", max_length=2000)


class UploadDocumentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=_MAX_NAME, description="展示用的文件名")
    content_base64: str = Field(min_length=1, description="原文的 base64（UTF-8 文本）")
    mime_type: str = Field(default="text/plain", max_length=120)


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)


# --------------------------------------------------------------------- 工具
def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"error": code, "message": message}
    body.update(extra)
    return JSONResponse(body, status_code=status)


def _guard(request: Request) -> JSONResponse | None:
    """默认禁用：开关没开就直接拒，连 PG 都不碰。"""
    settings = request.app.state.settings
    if knowledge_enabled(settings):
        return None
    return _error(
        403,
        "knowledge_disabled",
        f"本地知识库默认关闭；设 CHIRON_LOCAL_KNOWLEDGE_ENABLED=1 后重启本地后端再试（当前 data_dir={settings.data_dir}）",
    )


def _store(request: Request) -> Any:
    """取存储：优先 ``app.state.knowledge_store``（测试注入），否则按 PG DSN 现建。"""
    injected = getattr(request.app.state, "knowledge_store", None)
    if injected is not None:
        return injected

    settings = request.app.state.settings
    pg = getattr(request.app.state, "pg", None)
    dsn = getattr(pg, "dsn", "") or ""
    store = PgKnowledgeStore(dsn=dsn, root=settings.data_dir / "knowledge")
    request.app.state.knowledge_store = store
    return store


def _safe_name(raw: str) -> str | None:
    """上传名必须是**一个**文件名：不允许目录分隔符、上跳写法、控制字符。"""
    candidate = raw.strip()
    if not candidate or candidate in {".", ".."}:
        return None
    if _BAD_NAME.search(candidate):
        return None  # 含 \\ / : * ? " < > | 或控制字符
    if ".." in candidate:
        # 明确拒绝上跳写法：即使 "a..b" 本身是合法文件名，也一律让调用方改掉
        return None
    if len(candidate) > _MAX_NAME:
        return None
    return candidate


# --------------------------------------------------------------------- 知识库
@router.post("/v1/kb")
async def create_kb(payload: CreateKbRequest, request: Request) -> JSONResponse:
    failure = _guard(request)
    if failure is not None:
        return failure

    name = payload.name.strip()
    if not name:
        return _error(400, "invalid_name", "知识库名称不能为空")

    try:
        kb = await _store(request).create_kb(name=name, description=payload.description.strip())
    except ValueError as exc:
        return _error(400, "invalid_name", str(exc))
    except StoreUnavailable as exc:
        return _error(503, "storage_unavailable", str(exc))
    return JSONResponse({"data": kb}, status_code=201)


@router.get("/v1/kb")
async def list_kb(request: Request) -> JSONResponse:
    failure = _guard(request)
    if failure is not None:
        return failure

    try:
        kbs = await _store(request).list_kbs()
    except StoreUnavailable as exc:
        return _error(503, "storage_unavailable", str(exc))
    return JSONResponse({"data": kbs})


@router.delete("/v1/kb/{kb_id}")
async def delete_kb(kb_id: str, request: Request) -> JSONResponse:
    failure = _guard(request)
    if failure is not None:
        return failure

    try:
        deleted = await _store(request).delete_kb(kb_id)
    except ValueError as exc:
        return _error(400, "invalid_kb_id", str(exc))
    except StoreUnavailable as exc:
        return _error(503, "storage_unavailable", str(exc))
    if not deleted:
        return _error(404, "kb_not_found", f"知识库不存在：{kb_id}")
    return JSONResponse({"data": {"id": kb_id, "deleted": True}})


# --------------------------------------------------------------------- 文档
@router.post("/v1/kb/{kb_id}/documents")
async def upload_document(kb_id: str, payload: UploadDocumentRequest, request: Request) -> JSONResponse:
    failure = _guard(request)
    if failure is not None:
        return failure

    filename = _safe_name(payload.name)
    if filename is None:
        return _error(400, "invalid_document_name", "文件名不合法（不能含路径分隔符、控制字符或 ..）")

    try:
        data = base64.b64decode(payload.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        return _error(400, "invalid_base64", f"content_base64 不是合法的 base64：{exc}")
    if not data:
        return _error(400, "empty_document", "文档内容为空")
    if len(data) > MAX_DOCUMENT_BYTES:
        return _error(
            400,
            "document_too_large",
            f"文档 {len(data)} 字节超过上限 {MAX_DOCUMENT_BYTES} 字节",
        )

    # 本批只索引 UTF-8 文本：二进制也当文本读（替换非法字节），不悄悄丢内容
    chunks = chunk_text(data.decode("utf-8", errors="replace"))
    if not chunks:
        return _error(400, "empty_document", "文档解码后没有可索引的文本")

    store = _store(request)
    try:
        kb = await store.get_kb(kb_id)
        if kb is None:
            return _error(404, "kb_not_found", f"知识库不存在：{kb_id}")
        doc = await store.add_document(
            kb_id=kb_id,
            name=filename,
            data=data,
            mime_type=payload.mime_type,
            chunks=chunks,
        )
    except ValueError as exc:
        return _error(400, "invalid_kb_id", str(exc))
    except StoreUnavailable as exc:
        return _error(503, "storage_unavailable", str(exc))
    return JSONResponse({"data": doc}, status_code=201)


# --------------------------------------------------------------------- 检索
@router.post("/v1/kb/{kb_id}/query")
async def query_kb(kb_id: str, payload: QueryRequest, request: Request) -> JSONResponse:
    failure = _guard(request)
    if failure is not None:
        return failure

    terms = keywords(payload.query)
    if not terms:
        return _error(400, "empty_query", "query 里没有可用于检索的关键词")

    store = _store(request)
    try:
        kb = await store.get_kb(kb_id)
        if kb is None:
            return _error(404, "kb_not_found", f"知识库不存在：{kb_id}")
        rows = await store.search(
            kb_id=kb_id,
            terms=terms,
            limit=max(payload.top_k * CANDIDATE_FACTOR, 50),
        )
    except ValueError as exc:
        return _error(400, "invalid_kb_id", str(exc))
    except StoreUnavailable as exc:
        return _error(503, "storage_unavailable", str(exc))

    result = search_payload(rows, payload.query, payload.top_k)
    # 契约 §4 的统一包装是 {"data": ...}，而本端点的字段形状被约定为 {answer, citations}：
    # 两者都满足 —— data 里是同一份对象，顶层同时给出 answer / citations。
    return JSONResponse({"data": result, "answer": result["answer"], "citations": result["citations"]})


# --------------------------------------------------------------------- 构建
@router.post("/v1/kb/{kb_id}/build")
async def build_index(kb_id: str, request: Request) -> JSONResponse:
    """检查 pageindex 组件并占位。

    **本端点不会 spawn 任何进程**：真正的索引构建属于 P3。这里只回答"组件装了没有"，
    让前端能给出准确的引导文案，而不是假装在构建。
    """
    failure = _guard(request)
    if failure is not None:
        return failure

    settings = request.app.state.settings
    status = pageindex_status(settings)
    component_dir = find_pageindex_dir(settings)
    if component_dir is None:
        return _error(
            409,
            "pageindex_not_installed",
            "未安装 pageindex 组件；把组件放到下列任一目录后重试（本批不提供自动下载）",
            candidates=status["candidates"],
        )

    try:
        kb = await _store(request).get_kb(kb_id)
    except ValueError as exc:
        return _error(400, "invalid_kb_id", str(exc))
    except StoreUnavailable as exc:
        return _error(503, "storage_unavailable", str(exc))
    if kb is None:
        return _error(404, "kb_not_found", f"知识库不存在：{kb_id}")

    return JSONResponse(
        {
            "data": {
                "status": "queued",
                "kb_id": kb_id,
                "component": str(component_dir),
                "manifest": status["manifest"],
                "job_id": new_id("job"),
                "detail": "占位：PageIndex 索引构建在 P3 实现，本端点不启动子进程",
            }
        },
        status_code=202,
    )
