"""message_codec — 提供商无关的中立消息格式与转换层。

SaaS 架构决策：内部（runtime/session 缓存/压缩）统一使用**中立格式**，
到各 provider 边界才做适配转换，以便日后在 OpenAI / Anthropic / DeepSeek /
本地模型等多家 agent 提供商之间切换（新增提供商只需新写 to_xxx/from_xxx）。

中立格式（provider-agnostic）:
    {"role": "system"|"user"|"assistant"|"tool",
     "content": str,
     "tool_call_id": str,                          # role=tool 时关联
     "tool_calls": [{"id", "name", "arguments"}]}  # role=assistant 时的工具请求
    - arguments 为 JSON 字符串
    - 不包含任何 provider 专有包装（如 OpenAI 的 type/function）

转换函数：
    to_openai(messages)      → OpenAI Chat Completions 消息格式（dict 列表）
    from_openai(messages)    → OpenAI 格式 → 中立（兼容历史缓存）
    to_chat_messages(...)    → 中立 → gateway.ChatMessage 对象列表
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.gateway.provider import ChatMessage

# ═════════════════════ 中立格式工具 ═════════════════════

ROLES = ("system", "user", "assistant", "tool")


def make_message(
    role: str,
    content: str = "",
    tool_call_id: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """构造中立格式消息（固定字段顺序：role → content → tool_call_id → tool_calls）。"""
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    msg: dict[str, Any] = {"role": role}
    # tool_calls 存在时 content 可为空串（各 provider 有自己的 content 处理）
    msg["content"] = content if content is not None else ""
    if tool_call_id and role == "tool":
        msg["tool_call_id"] = tool_call_id
    if tool_calls:
        # 中立化 tool_calls：兼容 OpenAi 包装 {id,type,function:{name,arguments}}
        # 与已有中立 {id,name,arguments} 两种输入
        normalized = []
        for tc in tool_calls:
            if (
                isinstance(tc, dict)
                and "function" in tc
                and isinstance(tc.get("function"), dict)
            ):
                fn = tc["function"]
                normalized.append(
                    {
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", ""),
                    }
                )
            else:
                normalized.append(
                    {
                        "id": tc.get("id", ""),
                        "name": tc.get("name", ""),
                        "arguments": tc.get("arguments", ""),
                    }
                )
        msg["tool_calls"] = normalized
    if extra:
        extra.pop("reasoning_content", None)  # 思考内容不随消息持久化
        if extra:
            msg.update(extra)
    return msg


def is_neutral(messages: list[dict[str, Any]]) -> bool:
    """检测消息列表是否已是中立格式（无 OpenAI 包装）。"""
    for m in messages:
        for tc in m.get("tool_calls") or []:
            if isinstance(tc, dict) and "function" in tc:
                return False
    return True


# ═════════════════════ OpenAI 适配 ═════════════════════


def to_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """中立格式 → OpenAI Chat Completions 消息（dict 列表）。"""
    out = []
    for m in messages:
        msg: dict[str, Any] = {"role": m.get("role", "user")}
        content = m.get("content") or ""
        tool_calls = m.get("tool_calls")
        msg["content"] = content if content else (None if tool_calls else "")
        tc_id = m.get("tool_call_id")
        if tc_id and msg["role"] == "tool":
            msg["tool_call_id"] = tc_id
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": tc.get("arguments", ""),
                    },
                }
                for tc in tool_calls
            ]
        out.append(msg)
    return out


def from_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI 格式 → 中立格式（兼容历史 session 缓存）。"""
    return [
        make_message(
            role=m.get("role", "user"),
            content=m.get("content") or "",
            tool_call_id=m.get("tool_call_id", ""),
            tool_calls=m.get("tool_calls"),
        )
        for m in messages
    ]


# ═════════════════════ 自动格式推断 ═════════════════════


def detect_format(messages: list[dict[str, Any]]) -> str:
    """自动推断消息格式：openai | anthropic | gemini | neutral | unknown。

    市场格式特征（调研结论）：
    - OpenAI：role 含 "tool"，或 tool_calls 带 {type:"function", function:{...}} 包装
    - Anthropic：content 是 blocks 数组且含 {type:"tool_use"}/{type:"tool_result"}
    - Gemini：role 为 "model"，或 content 含 {functionCall}/{functionResponse} parts
    - 中立：tool_calls 为 {id,name,arguments}（无包装）
    """
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "")
        content = m.get("content")
        tool_calls = m.get("tool_calls")
        # OpenAI 特异特征：tool_calls 带 {type:"function", function:{...}} 包装
        if tool_calls and any(
            isinstance(tc, dict) and "function" in tc for tc in tool_calls
        ):
            return "openai"
        # Gemini 特异特征：role=model（中立/OpenAI 均无此 role）
        if role == "model":
            return "gemini"
        if isinstance(content, list):
            types = {b.get("type") for b in content if isinstance(b, dict)}
            if types & {"tool_use", "tool_result"}:
                return "anthropic"
            if types & {"functionCall", "functionResponse"}:
                return "gemini"
    if is_neutral(messages or []):
        return "neutral"
    return "unknown"


def auto_normalize(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """自动推断输入格式并统一为中立格式（SaaS：任意提供商来源的历史/缓存均可消费）。"""
    if not messages:
        return []
    fmt = detect_format(messages)
    if fmt == "openai":
        return from_openai(messages)
    if fmt == "anthropic":
        return from_anthropic(messages)
    if fmt == "gemini":
        return from_gemini(messages)
    if fmt == "neutral":
        return messages
    # unknown：逐条中立化（尽力处理）
    return [
        make_message(
            role=m.get("role", "user"),
            content=m.get("content", "") if isinstance(m.get("content"), str) else "",
            tool_call_id=m.get("tool_call_id", ""),
            tool_calls=m.get("tool_calls"),
        )
        for m in messages
    ]


# ═════════════════════ Anthropic 适配 ═════════════════════


def from_anthropic(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic Messages API 格式 → 中立格式。

    - content 为 blocks 数组：text / tool_use（assistant）/ tool_result（user 消息内）
    - tool_result 拆为独立 role="tool" 消息（tool_call_id = tool_use_id）
    """
    out: list[dict[str, Any]] = []
    for m in messages or []:
        role = m.get("role", "user")
        blocks = m.get("content")
        if not isinstance(blocks, list):
            out.append(
                make_message(
                    role=role, content=blocks if isinstance(blocks, str) else ""
                )
            )
            continue
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "text":
                text_parts.append(str(b.get("text", "")))
            elif btype == "tool_use":
                tool_calls.append(
                    {
                        "id": b.get("id", ""),
                        "name": b.get("name", ""),
                        "arguments": json.dumps(
                            b.get("input") or {}, ensure_ascii=False
                        ),
                    }
                )
            elif btype == "tool_result":
                res = b.get("content")
                tool_results.append(
                    {
                        "tool_call_id": b.get("tool_use_id", ""),
                        "content": (
                            res
                            if isinstance(res, str)
                            else json.dumps(res, ensure_ascii=False, default=str)
                        ),
                    }
                )
        if tool_calls:
            out.append(
                make_message(
                    role="assistant",
                    content="\n".join(text_parts),
                    tool_calls=tool_calls,
                )
            )
        elif text_parts:
            out.append(make_message(role=role, content="\n".join(text_parts)))
        for tr in tool_results:
            out.append(
                make_message(
                    role="tool", content=tr["content"], tool_call_id=tr["tool_call_id"]
                )
            )
    return out


# ═════════════════════ Gemini 适配 ═════════════════════


def from_gemini(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gemini 格式（role: user/model，parts 数组）→ 中立格式（基础支持）。"""
    out: list[dict[str, Any]] = []
    for m in messages or []:
        role = "assistant" if m.get("role") == "model" else m.get("role", "user")
        parts = (
            m.get("content")
            if isinstance(m.get("content"), list)
            else m.get("parts", [])
        )
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for p in parts or []:
            if not isinstance(p, dict):
                continue
            if "text" in p:
                text_parts.append(str(p.get("text", "")))
            elif "functionCall" in p:
                fc = p["functionCall"]
                tool_calls.append(
                    {
                        "id": fc.get("id", ""),
                        "name": fc.get("name", ""),
                        "arguments": json.dumps(
                            fc.get("args") or {}, ensure_ascii=False
                        ),
                    }
                )
            elif "functionResponse" in p:
                fr = p["functionResponse"]
                out.append(
                    make_message(
                        role="tool",
                        content=json.dumps(
                            fr.get("response") or {}, ensure_ascii=False, default=str
                        ),
                        tool_call_id=fr.get("id", ""),
                    )
                )
        if tool_calls:
            out.append(
                make_message(
                    role="assistant",
                    content="\n".join(text_parts),
                    tool_calls=tool_calls,
                )
            )
        elif text_parts:
            out.append(make_message(role=role, content="\n".join(text_parts)))
    return out


# ═════════════════════ gateway 适配 ═════════════════════


def to_chat_messages(messages: list[dict[str, Any]]) -> list[ChatMessage]:
    """中立格式 → gateway.ChatMessage 对象列表（provider 边界）。"""
    from app.gateway.provider import ChatMessage, ToolCall

    return [
        ChatMessage(
            role=m.get("role", "user"),
            content=m.get("content") or "",
            tool_call_id=m.get("tool_call_id", ""),
            tool_calls=[
                ToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("name", ""),
                    arguments=tc.get("arguments", ""),
                )
                for tc in (m.get("tool_calls") or [])
            ]
            or None,
        )
        for m in messages
    ]
