"""pageindex-search —— **无状态**检索服务（引擎经内网 HTTP 调用）。

为什么单独一个服务：pageindex → litellm → ``openai<3`` 与引擎固定的 ``openai 3.x``
在同一个环境里无法共存。放进独立进程后两边各自演进，也把"解析大 PDF 可能 OOM"这类
风险从在线对话进程里挪走。

**它不读数据库、不落盘凭据**：库/租户/文档范围、用户的 Cloud key、LLM 连接，全部由引擎
（唯一的权限与解密权威）解析好后随请求传入。"服务自己不查权限"是有意的 —— 权限只有一处
实现（引擎的 ``kb_access``），服务侧不再复制一份。

**越界守卫留在服务侧**：PageIndex 的 ``doc_id`` 官方语义是 "targets at the prompt
level"（引导而非硬范围），所以引用必须能映射回本库文档，否则整条拒绝返回。
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.adapter import (
    Adapter,
    PageIndexError,
    RequestContext,
    UnsupportedDocument,
    sdk_version,
)
from app.config import Config

logger = logging.getLogger(__name__)

app = FastAPI(title="pageindex-search", docs_url=None, openapi_url=None, redoc_url=None)

_cfg: Config | None = None
_sem: asyncio.Semaphore | None = None
_redis_client: Any = None


def _answer_sem() -> asyncio.Semaphore:
    """问答并发上限：chat 是同步阻塞的重活，没有上限时会把线程池与内存打满。"""
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(max(1, int(os.getenv("PAGEINDEX_MAX_CONCURRENCY") or 4)))
    return _sem


async def _redis() -> Any:
    """只读 Redis（仅 /metrics 用）：queue depth 与 worker 心跳都在那里。"""
    global _redis_client
    if _redis_client is None:
        import redis.asyncio as aioredis

        _redis_client = aioredis.from_url(config().redis_url)
    return _redis_client


def config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = Config.from_env()
    return _cfg


def _require_internal_token(provided: str | None) -> None:
    """与引擎同一套内部鉴权约定（X-Internal-Token）。

    fail-close：未配置 token 时拒绝一切请求 —— 否则任何同网段进程都能借它读用户文档。
    """
    expected = config().internal_token
    if not expected:
        raise HTTPException(status_code=503, detail="INTERNAL_TOKEN 未配置，服务拒绝工作")
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid internal token")


class ContextBody(BaseModel):
    """引擎解析好、随请求传入的上下文（服务侧不查库、不解密）。"""

    mode: str = "selfhosted"          # selfhosted | cloud
    kb_id: str
    tenant_id: str = ""
    user_id: str = ""
    doc_ids: list[str] = Field(default_factory=list)
    index_ref: str = ""              # 库 folder（cloud）
    user_folder_id: str = ""          # 用户 folder（cloud）
    api_key: str = ""                 # 用户级 Cloud key（明文只在内存里）
    llm: dict[str, Any] = Field(default_factory=dict)

    def to_ctx(self) -> RequestContext:
        if self.mode not in ("selfhosted", "cloud"):
            raise HTTPException(status_code=400, detail=f"未知 mode: {self.mode!r}")
        return RequestContext(
            kb_id=self.kb_id,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            doc_ids=tuple(d for d in self.doc_ids if d),
            index_ref=self.index_ref,
            user_folder_id=self.user_folder_id,
            api_key=self.api_key,
            llm=dict(self.llm or {}),
        )


class AnswerBody(ContextBody):
    question: str


class DeleteBody(ContextBody):
    pageindex_doc_id: str


def _adapter(body: ContextBody) -> tuple[Adapter, RequestContext]:
    ctx = body.to_ctx()
    return Adapter(mode=ctx_mode(body), cfg=config()), ctx


def ctx_mode(body: ContextBody) -> str:
    return body.mode


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """健康检查**不要求内部 token**（供容器探针使用），但也不泄露任何业务信息。"""
    health = Adapter(mode="selfhosted", cfg=config()).health()
    return {**health, "engine": sdk_version(), "service": "pageindex-search"}


@app.get("/metrics")
async def metrics() -> Any:
    """Prometheus 文本指标：kb 队列深度 / 存活 worker / 处理计数。

    worker 在 Redis 里写心跳（60s TTL）与累计计数（见 app/index_worker.py），
    这里做**唯一抓取点** —— worker 自身没有 HTTP 端口，靠它暴露不现实。
    取不到 Redis 时返回 0 而不是 500：指标失败不该让整个服务挂掉。
    """
    from fastapi.responses import PlainTextResponse

    from app.runtime import rkey

    depth = workers = processed = failed = 0
    try:
        client = await _redis()
        info = await client.xinfo_stream(config().kb_stream)
        depth = int(info.get("length", 0) or 0)
        workers = len([k async for k in client.scan_iter(match="*engine:kb:worker:hb:*")])
        stats = await client.hgetall(rkey("engine:kb:stats"))
        processed = int(stats.get(b"processed", 0) or 0)
        failed = int(stats.get(b"failed", 0) or 0)
    except Exception:  # noqa: BLE001
        pass
    body = "\n".join(
        [
            "# HELP kb_queue_depth Pending kb_index tasks",
            "# TYPE kb_queue_depth gauge",
            f"kb_queue_depth {depth}",
            "kb_workers_alive " + str(workers),
            f"kb_processed_total {processed}",
            f"kb_failed_total {failed}",
        ]
    )
    return PlainTextResponse(body + "\n")


@app.post("/v1/answer")
async def answer(
    body: AnswerBody, x_internal_token: str | None = Header(default=None)
) -> dict[str, Any]:
    _require_internal_token(x_internal_token)
    ctx = body.to_ctx()
    adapter = Adapter(mode=ctx_mode(body), cfg=config())
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="question 不能为空")
    try:
        # PageIndex 的 chat 是**同步阻塞**（分钟级 LLM 工具循环）：必须下放线程，
        # 否则一个请求就卡死整个服务的事件循环（uvicorn 默认单 worker）。
        async with _answer_sem():
            result = await asyncio.to_thread(adapter.answer, ctx, body.question.strip())
    except PageIndexError as exc:
        logger.warning("answer failed: ctx=%s err=%s", ctx.redacted(), exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "answer": result.answer,
        "citations": result.citations,
        "engine": sdk_version(),
    }


@app.post("/v1/prepare")
async def prepare(
    body: ContextBody, x_internal_token: str | None = Header(default=None)
) -> dict[str, Any]:
    """确保索引位置存在（Cloud 建 folder / local 返回索引目录），供引擎回写 ``index_ref``。"""
    _require_internal_token(x_internal_token)
    ctx = body.to_ctx()
    adapter = Adapter(mode=ctx_mode(body), cfg=config())
    try:
        if body.mode == "cloud":
            kb_folder, user_folder = await asyncio.to_thread(adapter.ensure_kb_folder, ctx)
            return {"index_ref": kb_folder, "user_folder_id": user_folder, "engine": sdk_version()}
    except PageIndexError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"index_ref": adapter.storage_path(ctx), "user_folder_id": "", "engine": sdk_version()}


@app.post("/v1/delete")
async def delete(
    body: DeleteBody, x_internal_token: str | None = Header(default=None)
) -> dict[str, Any]:
    _require_internal_token(x_internal_token)
    ctx = body.to_ctx()
    await asyncio.to_thread(
        Adapter(mode=ctx_mode(body), cfg=config()).delete_document,
        ctx,
        body.pageindex_doc_id,
    )
    return {"deleted": body.pageindex_doc_id}


@app.post("/v1/supports")
async def supports(
    body: ContextBody, x_internal_token: str | None = Header(default=None), filename: str = ""
) -> dict[str, Any]:
    """上传前的类型校验（引擎在落库前问一次，避免"传成功了却索引不了"）。"""
    _require_internal_token(x_internal_token)
    adapter = Adapter(mode=ctx_mode(body), cfg=config())
    try:
        return {"supported": adapter.supports(filename)}
    except UnsupportedDocument as exc:  # pragma: no cover - supports 不抛
        return {"supported": False, "detail": str(exc)}
