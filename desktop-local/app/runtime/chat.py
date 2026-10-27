"""对话编排：历史 → messages → 推理 → 落库 → (可选) SSE。

这里放**不依赖 FastAPI 的逻辑**：HTTP 层（``app/api/chat.py``）只做参数校验与异常→状态码
映射，编排本身可以脱离请求对象单测。

三条刻意的取舍：

1. **先落用户消息，再调推理**。推理失败时历史里仍留着用户输入——用户不会因为一次 502
   就丢掉刚写的那段话（代价是可能出现"只有 user 没有 assistant"的会话，这是可接受的）。
2. **不做假的 token 流**。推理代理只提供非流式 ``/infer``，因此 ``stream=true`` 时我们先把
   回复取回来，再按块发 SSE。事件形状与将来的真流式一致（``text`` / ``done``），前端不用改。
3. **不编造模型名**。没给 model、配置里也没有模型时抛 :class:`ModelRequiredError`（400），
   而不是塞一个 ``"default"`` 让代理去猜。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.runtime.inference import first_model_id

#: 一次推理最多带回多少条历史消息（本地上下文很小，别把整段对话塞进去）
DEFAULT_HISTORY_LIMIT = 24

#: SSE 文本事件的切块长度（字符）。只为让前端有"在流动"的观感，不是真 token 边界。
STREAM_CHUNK_SIZE = 24

#: 会话标题取用户首条消息的前若干字符（本地不做标题模型）
TITLE_MAX_CHARS = 40

#: ``GET /v1/chat/sessions`` 的默认/最大页大小
DEFAULT_SESSION_LIMIT = 50
MAX_SESSION_LIMIT = 200


# --------------------------------------------------------------------- 异常
class ChatInputError(ValueError):
    """请求内容不合法（空消息等）→ 400。"""


class SessionNotFoundError(LookupError):
    """``session_id`` 指向不存在的会话 → 404。"""


class AgentNotFoundError(LookupError):
    """``agent_id`` 指向不存在的 Agent → 404。"""


class ModelRequiredError(ChatInputError):
    """没有可用的模型名（请求体没给、配置里也没有）→ 400。"""


# --------------------------------------------------------------------- 数据结构
@dataclass(frozen=True, slots=True)
class ChatTurn:
    """一次提交的输入（HTTP 层的 body 映射到这里）。"""

    message: str
    session_id: str | None = None
    model: str | None = None
    agent_id: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    history_limit: int = DEFAULT_HISTORY_LIMIT


@dataclass(frozen=True, slots=True)
class ChatReply:
    """一次提交的结果（非流式直接返回它，流式按块发它）。"""

    session_id: str
    message: dict[str, Any]
    usage: dict[str, Any] = field(default_factory=dict)
    usage_estimated: bool = False
    model: str = ""
    agent_id: str | None = None
    history_count: int = 0


# --------------------------------------------------------------------- 工具
def derive_title(message: str) -> str:
    """用首条用户消息做会话标题（单行、截断）。"""
    first_line = message.strip().splitlines()[0] if message.strip() else ""
    return first_line[:TITLE_MAX_CHARS]


def resolve_model(turn: ChatTurn, session: dict[str, Any] | None, agent: dict[str, Any] | None) -> str:
    """按「请求体 → Agent → 会话 → 配置清单第一个」的顺序定模型，都没有就报错。"""
    for candidate in (
        turn.model,
        (agent or {}).get("model"),
        (session or {}).get("model"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    configured = first_model_id()  # 配置非法时抛 ModelConfigError，交给 API 层转 500
    if configured:
        return configured
    raise ModelRequiredError("缺少模型：请求体给 model，或用 CHIRON_LOCAL_MODELS 配置可用模型清单")


def build_messages(history: list[dict[str, Any]], user_text: str, system_prompt: str = "") -> list[dict[str, str]]:
    """拼出送给代理的 messages（只留 role + content，代理也只接受这两种字段）。"""
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    for item in history:
        role = item.get("role")
        content = item.get("content")
        if role in {"system", "user", "assistant"} and isinstance(content, str) and content:
            messages.append({"role": str(role), "content": content})
    messages.append({"role": "user", "content": user_text})
    return messages


# --------------------------------------------------------------------- 编排
async def run_turn(store: Any, inference: Any, turn: ChatTurn) -> ChatReply:
    """跑一轮对话：定位/创建会话 → 拼历史 → 推理 → 落库。

    ``store`` / ``inference`` 是鸭子类型（见 ``runtime.store.LocalStore``、
    ``runtime.inference.InferenceClient``），测试里换成内存实现即可。
    """
    user_text = (turn.message or "").strip()
    if not user_text:
        raise ChatInputError("message 不能为空")

    session: dict[str, Any] | None = None
    if turn.session_id:
        session = await store.get_session(turn.session_id)
        if session is None:
            raise SessionNotFoundError(f"会话不存在：{turn.session_id}")

    agent_id = turn.agent_id or (session or {}).get("agent_id")
    agent: dict[str, Any] | None = None
    if agent_id:
        agent = await store.get_agent(agent_id)
        if agent is None:
            raise AgentNotFoundError(f"Agent 不存在：{agent_id}")
        agent_id = agent.get("id") or agent_id

    model = resolve_model(turn, session, agent)

    if session is None:
        session = await store.create_session(
            title=derive_title(user_text),
            model=model,
            agent_id=agent_id,
        )
    elif model != session.get("model") or (agent_id or None) != (session.get("agent_id") or None):
        await store.touch_session(session["id"], model=model, agent_id=agent_id)

    history = await store.list_messages(session_id=session["id"], limit=max(1, turn.history_limit))
    messages = build_messages(history, user_text, str((agent or {}).get("system_prompt") or ""))

    # 先落用户消息：推理失败时用户的输入仍然在历史里（见模块 docstring 第 1 条）
    await store.append_message(
        session_id=session["id"],
        role="user",
        content=user_text,
        model=model,
        agent_id=agent_id,
    )

    result = await inference.infer(
        model=model,
        messages=messages,
        tenant_id=getattr(store, "tenant_id", 1),
        user_id=getattr(store, "user_id", "local"),
        max_tokens=turn.max_tokens,
        temperature=turn.temperature,
    )

    reply_text = str(result.get("text") or "")
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    assistant_message = await store.append_message(
        session_id=session["id"],
        role="assistant",
        content=reply_text,
        model=str(result.get("model") or model),
        agent_id=agent_id,
        prompt_tokens=_int_or_none(usage.get("prompt_tokens")),
        completion_tokens=_int_or_none(usage.get("completion_tokens")),
    )

    return ChatReply(
        session_id=session["id"],
        message=assistant_message,
        usage=usage,
        usage_estimated=bool(result.get("usage_estimated")),
        model=str(result.get("model") or model),
        agent_id=agent_id,
        history_count=len(messages) - 1,
    )


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------- SSE
def sse_frame(event: str, payload: dict[str, Any]) -> str:
    """一帧 SSE：``event:`` 给 ``addEventListener`` 用，``data:`` 里也带 ``type``。"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def chunk_text(text: str, size: int = STREAM_CHUNK_SIZE) -> list[str]:
    """按字符切块；空文本回一个空块，让"至少有一条 text 事件"始终成立。"""
    step = max(1, size)
    if not text:
        return [""]
    return [text[index : index + step] for index in range(0, len(text), step)]


async def stream_reply(reply: ChatReply, *, chunk_size: int = STREAM_CHUNK_SIZE) -> AsyncIterator[str]:
    """把已完成的回复按块发成 SSE：``start`` → ``text``* → ``done``。"""
    yield sse_frame(
        "start",
        {
            "type": "start",
            "session_id": reply.session_id,
            "message_id": reply.message.get("id", ""),
            "model": reply.model,
            "agent_id": reply.agent_id,
        },
    )
    for chunk in chunk_text(str(reply.message.get("content") or ""), chunk_size):
        yield sse_frame("text", {"type": "text", "session_id": reply.session_id, "text": chunk})
    yield sse_frame(
        "done",
        {
            "type": "done",
            "session_id": reply.session_id,
            "message": reply.message,
            "usage": reply.usage,
            "usage_estimated": reply.usage_estimated,
            "model": reply.model,
        },
    )


def error_frame(message: str, *, code: str = "inference_error") -> str:
    """流里出错时的帧（流式请求已经不是"一次性 5xx"，只能用事件报错）。"""
    return sse_frame("error", {"type": "error", "code": code, "message": message})
