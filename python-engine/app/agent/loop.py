# Agent 推理循环 — 使用接口注入
from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from app.config import settings
from app.gateway.provider import ChatMessage
from app.gateway.router import GatewayRouter
from app.interfaces.llm import LLMProvider

logger = logging.getLogger(__name__)


def _safe_json(s):
    import json as _json

    try:
        return _json.loads(s)
    except Exception:
        return {}


def build_messages(
    system_prompt: str, history: list[dict], content: str
) -> list[ChatMessage]:
    """构建 LLM 消息列表"""
    messages = []

    if system_prompt:
        messages.append(ChatMessage(role="system", content=system_prompt))

    for msg in history:
        role = msg.get("role", "user")
        tool_calls = None
        if msg.get("tool_calls"):
            from app.gateway.provider import ToolCall

            tool_calls = [
                ToolCall(
                    id=tc.get("id", ""),
                    name=(
                        tc.get("function", {}).get("name", "")
                        if isinstance(tc.get("function"), dict)
                        else tc.get("name", "")
                    ),
                    arguments=(
                        tc.get("function", {}).get("arguments", "")
                        if isinstance(tc.get("function"), dict)
                        else tc.get("arguments", "")
                    ),
                )
                for tc in msg["tool_calls"]
            ]
        messages.append(
            ChatMessage(
                role=role,
                content=msg.get("content", ""),
                tool_call_id=msg.get("tool_call_id", ""),
                tool_calls=tool_calls,
            )
        )

    if content:
        messages.append(ChatMessage(role="user", content=content))

    return messages


def convert_tools(tools: list[dict]) -> list[dict]:
    """将工具定义转换为 OpenAI function 格式"""
    converted = []
    for tool in tools:
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": (
                        _safe_json(tool.get("parameters_json", "{}"))
                        if isinstance(tool.get("parameters_json"), str)
                        else tool.get("parameters", {})
                    ),
                },
            }
        )
    return converted


async def run_agent(
    gateway: GatewayRouter,
    system_prompt: str,
    history: list[dict],
    content: str,
    tools: list[dict] = None,
    llm_config: dict = None,
    max_turns: int = None,
    tenant_id: str = "",
    provider_hint: str = "",
) -> AsyncIterator[dict]:
    """
    Agent 推理循环，流式返回结果

    Yields:
        dict:
            - {"type": "text", "content": "..."}
            - {"type": "tool_call", "id": "...", "name": "...", "arguments": "..."}
            - {"type": "usage", "input_tokens": N, "output_tokens": N}
            - {"type": "done"}
            - {"type": "error", "message": "..."}
    """
    max_turns = max_turns or settings.max_turns
    llm_config = llm_config or {}
    model = llm_config.get("model", settings.default_model)
    max_tokens = llm_config.get("max_tokens", settings.default_max_tokens)
    temperature = llm_config.get("temperature", settings.default_temperature)

    messages = build_messages(system_prompt, history, content)
    openai_tools = convert_tools(tools) if tools else None

    total_input_tokens = 0
    total_output_tokens = 0

    for turn in range(max_turns):
        try:
            async for chunk in gateway.chat_stream(
                messages=messages,
                model=model,
                tenant_id=tenant_id,
                provider_hint=provider_hint,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=openai_tools,
            ):
                # 文本片段
                if chunk.content:
                    yield {"type": "text", "content": chunk.content}

                # 工具调用
                if chunk.tool_calls:
                    if chunk.finish_reason == "length":
                        # max_tokens 截断：arguments 可能是半截 JSON，解析出来是残缺参数。
                        # 不转发给执行方（宁可不做，也不拿残缺参数碰真实文件系统），
                        # 报错让模型重发完整调用。
                        logger.warning(
                            "Dropping truncated tool_calls (finish_reason=length): %s",
                            [tc.name for tc in chunk.tool_calls],
                        )
                        yield {
                            "type": "error",
                            "message": (
                                "output truncated by max_tokens before the tool call "
                                "completed; arguments may be incomplete — please "
                                "re-issue the tool call"
                            ),
                        }
                        break
                    for tc in chunk.tool_calls:
                        yield {
                            "type": "tool_call",
                            "id": tc.id,
                            "name": tc.name,
                            "arguments": tc.arguments,
                        }
                    # 工具调用后等待 Go 端执行并回调
                    break

                # Token 用量
                if chunk.input_tokens or chunk.output_tokens:
                    total_input_tokens += chunk.input_tokens
                    total_output_tokens += chunk.output_tokens

                # 完成
                if chunk.finish_reason == "stop" or chunk.finish_reason == "length":
                    yield {"type": "done"}
                    break

                # 错误
                if chunk.finish_reason == "error":
                    yield {"type": "error", "message": "LLM provider error"}
                    break
            else:
                # stream 结束但没有显式 done/tool_call
                if not openai_tools:
                    yield {"type": "done"}
                break

            # 跳出外层循环（已完成或等待 Go 网关回调）
            break

        except Exception as e:
            logger.error("Agent推理错误 (turn %d): %s", turn, e, exc_info=True)
            yield {"type": "error", "message": "LLM provider error"}
            break

    # 返回总 Token 用量
    yield {
        "type": "usage",
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
    }


async def run_agent_with_llm_provider(
    llm_provider: LLMProvider,
    system_prompt: str,
    history: list[dict],
    content: str,
    tools: list[dict] = None,
    llm_config: dict = None,
    max_turns: int = None,
    tenant_id: str = "",
) -> AsyncIterator[dict]:
    """
    Agent 推理循环（使用 LLMProvider 接口），流式返回结果

    Yields:
        dict:
            - {"type": "text", "content": "..."}
            - {"type": "tool_call", "id": "...", "name": "...", "arguments": "..."}
            - {"type": "usage", "input_tokens": N, "output_tokens": N}
            - {"type": "done"}
            - {"type": "error", "message": "..."}
    """
    max_turns = max_turns or settings.max_turns
    llm_config = llm_config or {}
    model = llm_config.get("model", settings.default_model)
    max_tokens = llm_config.get("max_tokens", settings.default_max_tokens)
    temperature = llm_config.get("temperature", settings.default_temperature)

    # 构建消息
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    for msg in history:
        entry = {
            "role": msg.get("role", "user"),
            "content": msg.get("content", ""),
            "tool_call_id": msg.get("tool_call_id", ""),
        }
        if msg.get("tool_calls"):
            entry["tool_calls"] = msg["tool_calls"]
        messages.append(entry)
    if content:
        messages.append({"role": "user", "content": content})

    # 转换工具
    openai_tools = convert_tools(tools) if tools else None

    total_input_tokens = 0
    total_output_tokens = 0

    for turn in range(max_turns):
        try:
            async for chunk in llm_provider.chat(
                messages=messages,
                model=model,
                tools=openai_tools,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=True,
            ):
                # 文本片段
                if chunk.get("content"):
                    yield {"type": "text", "content": chunk["content"]}

                # 工具调用
                if chunk.get("tool_calls"):
                    for tc in chunk["tool_calls"]:
                        yield {
                            "type": "tool_call",
                            "id": tc.get("id", ""),
                            "name": tc.get("name", ""),
                            "arguments": tc.get("arguments", ""),
                        }
                    # 工具调用后等待 Go 端执行并回调
                    break

                # Token 用量
                usage = chunk.get("usage", {})
                if usage.get("prompt_tokens") or usage.get("completion_tokens"):
                    total_input_tokens += usage.get("prompt_tokens", 0)
                    total_output_tokens += usage.get("completion_tokens", 0)

                # 完成
                if chunk.get("finish_reason") in ("stop", "length"):
                    yield {"type": "done"}
                    break

                # 错误
                if chunk.get("finish_reason") == "error":
                    yield {"type": "error", "message": "LLM provider error"}
                    break
            else:
                # stream 结束但没有显式 done/tool_call
                if not openai_tools:
                    yield {"type": "done"}
                break

            # 跳出外层循环（已完成或等待 Go 网关回调）
            break

        except Exception as e:
            logger.error("Agent推理错误 (turn %d): %s", turn, e, exc_info=True)
            yield {"type": "error", "message": "LLM provider error"}
            break

    # 返回总 Token 用量
    yield {
        "type": "usage",
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
    }
