"""桌面端云端片段推理（``POST /v1/desktop/infer``）。

为什么单开一个端点而不复用 ``/v1/chat/**``：桌面端在 ``local`` 模式下只需要"把一段
文本交给模型、拿回一段文本"，不需要 Agent 运行时、工具、会话、记忆、落库。走对话链路
会顺带带上面向对话的人格提示词、工具定义与记忆召回 —— 既改变模型输出（片段推理要的
是纯转写/摘要，不是助手口吻的答复），又让隐私边界变得不可解释（用户的本地文档片段会被
写进会话历史）。

认证：**必须是用户 JWT**，本端点不写自己的鉴权代码 —— 它复用 ``AuthMiddleware``
（``app/middleware/auth.py``）：``Authorization: Bearer <jwt>``（仅在
``ENGINE_ALLOW_DIRECT_JWT`` 打开时接受）与网关代理路径（``X-Internal-Token`` +
``?tenant_id=&user_id=``）都在那里收口。因此这里只读中间件已经设好的
``request.state.tenant_id``。

隐私：**不记录 messages / content**。日志里只有 ``request_id``、模型名、消息条数、
总字符数与（可从 usage 得到的）用量 —— 排查"为什么慢/为什么 502"够用，且不含用户
文档内容。上游错误同理：细节只进日志，响应体只给固定的 ``upstream inference failed``
（异常原文可能带上游 URL、key 片段或内部主机名）。
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Self

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

router = APIRouter(tags=["desktop"])

#: 单次请求的消息总字符上限。桌面端在本地抽取时按页/按段切分后再调；整本文档塞进来
#: 既会撞上游上下文上限，也会把一次请求的账算到用户头上。
MAX_TOTAL_CHARS = 2_000_000

#: 单次输出上限。500 行左右的片段推理够用；再大应该由调用方分段，而不是让一次请求
#: 占住上游连接（也顺带压住"一次误操作掏空额度"的风险）。
MAX_OUTPUT_TOKENS = 4096


class DesktopInferMessage(BaseModel):
    """一条消息：**只允许 user 角色与纯文本**。

    约束是刻意的：

    * ``role`` 用 ``Literal["user"]`` —— 桌面端不需要注入 system 提示词（那等于把
      "模型怎么表现"的决定权交给渲染进程），也不需要伪造 assistant 历史；
    * ``content`` 是 ``str`` —— 不接受 OpenAI 风格的多模态 parts。片段推理的输入是
      本地抽取出来的文本，接受 parts 只会扩大攻击面（内联图片 URL 会把请求变成
      SSRF 跳板）。
    """

    role: Literal["user"] = "user"
    content: str


class DesktopInferRequest(BaseModel):
    """片段推理请求。字段与渲染进程约定一致（见 docs/desktop-local/contracts.md）。"""

    model: str = Field(min_length=1, max_length=200)
    messages: list[DesktopInferMessage] = Field(min_length=1, max_length=200)
    max_tokens: int = Field(default=1024, ge=1, le=MAX_OUTPUT_TOKENS)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    #: OpenAI 风格的 ``response_format``（``{"type": "json_object"}`` / ``json_schema``）。
    #: **原样透传**给 provider：形状由上游校验，引擎不在这里二次解释（多一层白名单只会
    #: 让新形状晚一个版本才能用）。provider 不支持时网关会丢弃它并降级为纯文本
    #: （见 ``GatewayRouter._provider_extra_kwargs``）。
    response_format: dict[str, Any] | None = None
    #: 调用方生成，只用于把日志与用户侧的一次操作对上；不参与路由、不参与计费。
    request_id: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def _enforce_total_chars(self) -> Self:
        total = sum(len(m.content) for m in self.messages)
        if total > MAX_TOTAL_CHARS:
            # pydantic 会把它变成 422；消息里别带内容本身（只给长度）。
            raise ValueError(
                f"messages too large: {total} chars > {MAX_TOTAL_CHARS}"
            )
        return self


class DesktopInferUsage(BaseModel):
    """用量。``cached_tokens`` = 命中提示词缓存的输入 token（各家字段名不同，
    已由 provider 层归一化，见 ``cached_tokens_from_usage``）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


class DesktopInferResponse(BaseModel):
    content: str
    usage: DesktopInferUsage
    #: 实际服务的 provider 名（可能是路由/降级后的结果，不一定是调用方预期的那个）。
    provider: str = ""
    #: 实际使用的模型名。正常等于请求里的 ``model``；provider 回填时以回填为准。
    model: str = ""


@router.post("/v1/desktop/infer")
async def desktop_infer(
    body: DesktopInferRequest, request: Request
) -> DesktopInferResponse:
    """把一段文本交给云端 provider 推理，返回纯文本结果。

    错误只有一种对外形状：``502 upstream inference failed`` —— 无论是 gateway 未初始化、
    上游抛异常，还是 provider 以 ``finish_reason="error"`` 收尾。对桌面端而言这三者
    的处置相同（提示"云端不可用，稍后重试或切本地模式"），区分它们只会让客户端把内部
    拓扑写进自己的错误分支。
    """
    from app.main import get_gateway  # 局部导入：避免 main ←→ api 的循环导入

    tenant_id = int(getattr(request.state, "tenant_id", 0) or 0)
    # 转成 gateway 认识的裸 dict（`normalize_messages` 会再规范化为 ChatMessage）。
    messages: list[dict[str, Any]] = [
        {"role": m.role, "content": m.content} for m in body.messages
    ]
    total_chars = sum(len(m.content) for m in body.messages)
    # 注意：这里**只**打元信息。content 一个字都不进日志（见模块 docstring）。
    logger.info(
        "desktop infer: request_id=%s model=%s messages=%d chars=%d max_tokens=%d "
        "response_format=%s tenant=%s",
        body.request_id or "-",
        body.model,
        len(messages),
        total_chars,
        body.max_tokens,
        "yes" if body.response_format else "no",
        tenant_id,
    )

    try:
        gateway = await get_gateway()
    except RuntimeError:
        # 引擎启动中或 gateway 已被关闭（`app.main._gateway is None`）。
        logger.warning(
            "desktop infer: engine gateway unavailable (request_id=%s)",
            body.request_id or "-",
        )
        raise HTTPException(status_code=502, detail="upstream inference failed") from None

    try:
        resp = await gateway.chat(
            messages,
            body.model,
            tenant_id=tenant_id,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
            response_format=body.response_format,
        )
    except Exception as exc:  # noqa: BLE001 — 上游任何异常都归一到 502（细节只进日志）
        logger.warning(
            "desktop infer: upstream failure (request_id=%s): %s: %s",
            body.request_id or "-",
            type(exc).__name__,
            exc,
        )
        raise HTTPException(status_code=502, detail="upstream inference failed") from None

    if resp.finish_reason == "error":
        # gateway 把 provider 的失败包成 error 响应（不抛异常），这里补上状态码。
        logger.warning(
            "desktop infer: provider error (request_id=%s, provider=%s): %s",
            body.request_id or "-",
            resp.provider or "-",
            resp.message or "-",
        )
        raise HTTPException(status_code=502, detail="upstream inference failed")

    return DesktopInferResponse(
        content=resp.content or "",
        usage=DesktopInferUsage(
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            cached_tokens=resp.cached_tokens,
        ),
        provider=resp.provider or "",
        model=resp.model or body.model,
    )
