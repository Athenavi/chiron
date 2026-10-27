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

幂等（``request_id``）：桌面端在网络超时/重试时会**再发一次同一个 request_id**，而一次
片段推理可能已经在上游花掉了用户的 token。因此：

* 首个请求用 ``SET key <pending> NX EX 60`` 抢到占位；抢不到说明 60s 内已有同一个
  ``request_id`` 在跑或已跑完；
* 跑完的请求把**完整响应**写回同一个 key（TTL 不变），重复请求直接重放它，不碰上游；
* 还在跑的重复请求返回 **409**（结果尚不存在，返回一个空壳会让调用方当成真结果）；
* ≥ 502 的失败路径**删掉占位** —— 失败本来就应该可重试，占着 key 会让用户 60s 内
  连重试都做不了。

用量：成功的请求把 token 数累加到 ``desktop:infer:usage:{tenant}:{date}``（Redis Hash，
按 UTC 日期分桶，TTL 40 天）。**这里不写 credits** —— credits 的商业口径与计价表在 Go
网管的 billing 链（``internal/api/submit_handler.go`` 的 biller）里，引擎自己兼一份只
会导致重复扣费；本键只做用量对账与后续自动计费的输入（见 contracts.md §9）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal, Self

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from app.redis_keys import rkey

logger = logging.getLogger(__name__)

router = APIRouter(tags=["desktop"])

#: 幂等窗口（秒）。60s 内同一个 ``request_id`` 只打一次上游；TTL 到期后同 id 视为新请求。
IDEMPOTENCY_TTL_SECONDS = 60

#: ``desktop:infer:usage:{tenant}:{date}`` 的存活时间。留 40 天是为了跨月对账（当月 +
#: 上个月总在里面），又不至于让日键无上限堆积。
USAGE_TTL_SECONDS = 40 * 24 * 3600

#: 占位值（“已抢到、还在跑”）。真实结果是一段 JSON，永远不会等于这个字符串 ——
#: 判断“是否已完成”就靠它，不需要第二个 key。
_INFLIGHT = "pending"

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


# ── 幂等与用量（Redis） ────────────────────────────────────────────


def _idem_key(tenant_id: int, request_id: str) -> str:
    """幂等键。**租户必须进键**：``request_id`` 由客户端生成，不同租户撞上是必然的。"""
    return rkey(f"desktop:infer:idem:{tenant_id}:{request_id}")


def _usage_key(tenant_id: int, day: str) -> str:
    return rkey(f"desktop:infer:usage:{tenant_id}:{day}")


async def _redis_or_none() -> Any:
    """取 Redis；不可用时返回 ``None``（**fail-open**）。

    幂等与用量都是“锦上添花”的能力，Redis 抖动不该让桌面端的片段推理直接不可用 ——
    那样用户会先看到“云端不可用”、再去切本地模式，代价远大于丢掉一次去重。
    """
    try:
        from app.main import get_redis  # 局部导入：避免 main ←→ api 的循环导入

        return await get_redis()
    except Exception as exc:  # noqa: BLE001 — 未初始化 / 连接失败同样降级
        logger.warning(
            "desktop infer: redis unavailable, idempotency and usage are skipped: %s: %s",
            type(exc).__name__,
            exc,
        )
        return None


async def _begin_request(
    redis: Any, tenant_id: int, request_id: str
) -> tuple[str, DesktopInferResponse | None]:
    """抢幂等占位并读回已完成的结果。

    返回 ``(state, replay)``，``state`` 的含义：

    * ``claimed`` —— 抢到了，本次请求该真的打上游；
    * ``replay``  —— 60s 内跑完过同一个 ``request_id``，``replay`` 是它的响应；
    * ``pending`` —— 同一个 ``request_id`` 还在跑（调用方应返回 409）；
    * ``unavailable`` —— Redis 读/写失败，按"没启用幂等"继续（fail-open）。
    """
    key = _idem_key(tenant_id, request_id)
    try:
        # NX 是原子条件写入：并发两个同 request_id 的请求只有一个能拿到 True。
        claimed = await redis.set(key, _INFLIGHT, nx=True, ex=IDEMPOTENCY_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — 写不进去就当作没启用（fail-open）
        logger.warning(
            "desktop infer: idempotency claim failed (fail-open) request_id=%s: %s: %s",
            request_id,
            type(exc).__name__,
            exc,
        )
        return "unavailable", None
    if claimed:
        return "claimed", None

    try:
        raw = await redis.get(key)
    except Exception as exc:  # noqa: BLE001 — 读失败不能让用户白白被拒
        logger.warning(
            "desktop infer: idempotency read failed (fail-open) request_id=%s: %s: %s",
            request_id,
            type(exc).__name__,
            exc,
        )
        return "unavailable", None
    if not raw or raw == _INFLIGHT:
        return "pending", None
    try:
        return "replay", DesktopInferResponse.model_validate_json(raw)
    except Exception:  # noqa: BLE001 — 脏数据（旧格式/半截写入）当作“未完成”，本次请求继续
        logger.warning(
            "desktop infer: unreadable idempotency payload, treating as pending "
            "request_id=%s",
            request_id,
        )
        return "unavailable", None


async def _remember_response(redis: Any, tenant_id: int, request_id: str, resp: DesktopInferResponse) -> None:
    """把完整响应写回幂等键（覆盖占位，TTL 重置为 60s）。"""
    try:
        await redis.set(
            _idem_key(tenant_id, request_id),
            resp.model_dump_json(),
            ex=IDEMPOTENCY_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — 去重失效不影响本次结果
        logger.warning(
            "desktop infer: idempotency store failed request_id=%s: %s: %s",
            request_id,
            type(exc).__name__,
            exc,
        )


async def _release_request_id(redis: Any, tenant_id: int, request_id: str) -> None:
    """失败路径释放占位（失败必须可重试）。"""
    try:
        await redis.delete(_idem_key(tenant_id, request_id))
    except Exception as exc:  # noqa: BLE001 — 删不掉就等 60s 自然过期
        logger.warning(
            "desktop infer: idempotency release failed request_id=%s: %s: %s",
            request_id,
            type(exc).__name__,
            exc,
        )


async def _record_usage(redis: Any, tenant_id: int, usage: DesktopInferUsage) -> None:
    """累加当日用量。**不写 credits** —— 理由见模块 docstring。"""
    key = _usage_key(tenant_id, time.strftime("%Y-%m-%d", time.gmtime()))
    try:
        await redis.hincrby(key, "requests", 1)
        await redis.hincrby(key, "input_tokens", usage.input_tokens)
        await redis.hincrby(key, "output_tokens", usage.output_tokens)
        await redis.hincrby(key, "cached_tokens", usage.cached_tokens)
        await redis.expire(key, USAGE_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — 用量是账本，但记不上不能连坐本次响应
        logger.warning(
            "desktop infer: usage record failed tenant=%s: %s: %s",
            tenant_id,
            type(exc).__name__,
            exc,
        )


async def _run_inference(
    body: DesktopInferRequest, messages: list[dict[str, Any]], tenant_id: int
) -> DesktopInferResponse:
    """真正打上游的那一段（错误映射全在这里，幂等与用量由调用方收口）。"""
    from app.main import get_gateway  # 局部导入：避免 main ←→ api 的循环导入

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


@router.post("/v1/desktop/infer")
async def desktop_infer(
    body: DesktopInferRequest, request: Request
) -> DesktopInferResponse:
    """把一段文本交给云端 provider 推理，返回纯文本结果。

    错误只有一种对外形状：``502 upstream inference failed`` —— 无论是 gateway 未初始化、
    上游抛异常，还是 provider 以 ``finish_reason="error"`` 收尾。对桌面端而言这三者
    的处置相同（提示"云端不可用，稍后重试或切本地模式"），区分它们只会让客户端把内部
    拓扑写进自己的错误分支。

    唯一的例外是**幂等冲突**：同一个 ``request_id`` 在 60s 内仍在跑时返回 ``409``。
    它和 502 不同类：这不是"云端不可用"，而是"你刚问过、答案还在路上"。
    """
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

    redis = await _redis_or_none()
    # 幂等只在调用方**显式**给了 request_id 时启用：空 id 就是"我不需要去重"的表态。
    idempotent = bool(body.request_id) and redis is not None
    if idempotent:
        state, prior = await _begin_request(redis, tenant_id, body.request_id)
        if state == "replay" and prior is not None:
            # 重放：同一个 request_id 的第二次请求拿到与第一次**完全相同**的响应，
            # 且不再花用户的 token（客户端重试后的期望正是这个）。
            logger.info(
                "desktop infer: idempotent replay request_id=%s tenant=%s",
                body.request_id,
                tenant_id,
            )
            return prior
        if state == "pending":
            # 占位还在 = 同名请求仍在跑。返回空 content 会被当成真结果写进文档，
            # 所以这里明确拒绝，由客户端稍后重试（届时会命中上面的重放分支）。
            logger.info(
                "desktop infer: duplicate in-flight request_id=%s tenant=%s",
                body.request_id,
                tenant_id,
            )
            raise HTTPException(status_code=409, detail="duplicate request in progress")
        if state == "unavailable":
            # Redis 抽风：本次请求照常处理，只是不做去重（fail-open）。
            idempotent = False

    try:
        result = await _run_inference(body, messages, tenant_id)
    except Exception:
        # 失败（含 502 / 上游异常）必须可重试：占位留着会让同一个 request_id 在 60s
        # 内连重试都拿 409，而这恰恰是最需要重试的时刻。
        if idempotent:
            await _release_request_id(redis, tenant_id, body.request_id)
        raise
    except BaseException:
        # CancelledError（客户端断开 / 关机）：取消途中再 await 是不安全的，占位交给
        # 60s TTL 自然过期（最坏后果是这 60s 内同 id 的重试拿 409）。
        raise

    if idempotent:
        await _remember_response(redis, tenant_id, body.request_id, result)
    if redis is not None:
        await _record_usage(redis, tenant_id, result.usage)
    return result
