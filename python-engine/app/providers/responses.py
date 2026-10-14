"""OpenAI **Responses API** provider（D2）。

## 为什么单独一个 provider

Responses（`POST /v1/responses`）与 chat/completions 是**两套请求/响应形状**，不是"换个 URL"：

| | chat/completions | Responses |
| --- | --- | --- |
| 输入 | `messages`（每项 `content` 是字符串或多模态数组） | `input` 项 + 顶层 `instructions`（system 独立出来） |
| 输出 | `choices[0].delta` | 事件流（`response.output_text.delta` / `response.completed`） |
| 推理 | 顶层 `reasoning_effort` | `reasoning: {effort, summary}` |
| 工具 | `tools[].function` | `tools[]` 顶层形状不同，调用以 `function_call` 输出项出现 |

硬塞进 `OpenAIProvider` 会让那一份代码同时维护两套形状（"两个 if 分支各自演化"），
所以这里独立实现，但**复用同一份对外契约**（`ChatResponse` / `ChatMessage` / `ToolCall`），
这样网关与上层完全无感。

## 实现要点

* **system 单独走 `instructions`** —— Responses 不把 system 放进 `input`，硬塞会 400。
* **推理档位走契约表**（`reasoning_contract.resolve`）：本 provider 的契约是 nested，
  所以产出 `{"reasoning": {"effort": ...}}` —— 与 chat/completions 的顶层字段不同。
* **失败不抛给上层形状**：HTTP 错误转成 `ChatResponse(finish_reason="error", message=...)`，
  与其它 provider 的错误语义一致（上层据此下发可诊断的 message）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.gateway.provider import ChatMessage, ChatResponse, ToolCall

logger = logging.getLogger(__name__)

#: 是否为"思考摘要"事件（Responses 把推理摘要单独成流）。
_EV_TEXT = "response.output_text.delta"
_EV_REASONING = "response.reasoning_summary_text.delta"
_EV_ITEM_DONE = "response.output_item.done"
_EV_COMPLETED = "response.completed"
_EV_FAILED = "response.failed"


class ResponsesProvider:
    """OpenAI Responses API 提供者。"""

    name = "openai-responses"

    def __init__(self, api_key: str, base_url: str = "", *, timeout: float = 120.0) -> None:
        self._api_key = api_key
        # Responses 挂在同一个 API 根下（/v1/responses）；调用方给的是 /v1 基址时去掉重复段。
        base = (base_url or "https://api.openai.com/v1").rstrip("/")
        if base.endswith("/responses"):
            base = base[: -len("/responses")]
        self._base = base
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── 请求体构造（纯逻辑，可单测） ──────────────────────────────────────

    def build_payload(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        effort: str = "",
        stream: bool = True,
    ) -> dict[str, Any]:
        """把中立消息组装成 Responses 的请求体。

        **system 提取到 `instructions`**：Responses 不接受 `input` 里的 system 角色项。
        """
        instructions: list[str] = []
        items: list[dict[str, Any]] = []
        for m in messages:
            role = getattr(m, "role", "user")
            content = getattr(m, "content", "") or ""
            tool_calls = getattr(m, "tool_calls", None)
            tool_call_id = getattr(m, "tool_call_id", "") or ""

            if role == "system":
                if content.strip():
                    instructions.append(content)
                continue
            if role == "tool":
                # 工具结果在 Responses 里是 function_call_output 项。
                items.append({
                    "type": "function_call_output",
                    "call_id": tool_call_id,
                    "output": content,
                })
                continue
            if role == "assistant" and tool_calls:
                items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content}] if content else [],
                })
                for tc in tool_calls:
                    items.append({
                        "type": "function_call",
                        "call_id": tc.id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                    })
                continue
            # user / assistant 普通文本
            text_type = "output_text" if role == "assistant" else "input_text"
            items.append({
                "type": "message",
                "role": role,
                "content": [{"type": text_type, "text": content}],
            })

        payload: dict[str, Any] = {
            "model": model,
            "input": items,
            "max_output_tokens": max_tokens,
            "stream": stream,
        }
        if instructions:
            payload["instructions"] = "\n\n".join(instructions)
        if tools:
            # chat/completions 的 {"type":"function","function":{...}} → Responses 的扁平形状。
            payload["tools"] = [
                {
                    "type": "function",
                    "name": (t.get("function") or {}).get("name", ""),
                    "description": (t.get("function") or {}).get("description", ""),
                    "parameters": (t.get("function") or {}).get("parameters", {}),
                }
                for t in tools
            ]
        # 推理档位：**走契约表**（本 provider 是 nested 形态）。
        from app.providers.reasoning_contract import resolve as resolve_reasoning

        payload.update(resolve_reasoning(effort, provider=self.name, model=model))
        return payload

    # ── 流式 ─────────────────────────────────────────────────────────────

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ChatResponse]:
        from app.providers.session_context import get_llm_effort

        payload = self.build_payload(
            messages, model, max_tokens=max_tokens, temperature=temperature,
            tools=tools, effort=get_llm_effort(), stream=True,
        )
        url = f"{self._base}/responses"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        client = await self._get_client()
        try:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 300:
                    body = (await resp.aread()).decode("utf-8", "replace")[:400]
                    logger.warning("responses: HTTP %d: %s", resp.status_code, body)
                    yield ChatResponse(
                        finish_reason="error",
                        message=f"responses API error {resp.status_code}: {body}",
                    )
                    return
                async for chunk in self._parse_stream(resp):
                    yield chunk
        except httpx.HTTPError as exc:
            logger.warning("responses: transport error: %s", exc)
            yield ChatResponse(finish_reason="error", message=f"responses transport error: {exc}")

    async def _parse_stream(self, resp: httpx.Response) -> AsyncIterator[ChatResponse]:
        """把 Responses 的 SSE 事件翻成 ChatResponse。**纯解析**，不碰网络。"""
        tool_calls: list[ToolCall] = []
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        done = False

        async for line in resp.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            raw = line[len("data:"):].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = str(evt.get("type", ""))

            if etype == _EV_TEXT:
                delta = str(evt.get("delta", ""))
                if delta:
                    yield ChatResponse(content=delta)
            elif etype == _EV_REASONING:
                delta = str(evt.get("delta", ""))
                if delta:
                    yield ChatResponse(reasoning_content=delta)
            elif etype == _EV_ITEM_DONE:
                item = evt.get("item") or {}
                if item.get("type") == "function_call":
                    tool_calls.append(ToolCall(
                        id=str(item.get("call_id") or item.get("id") or ""),
                        name=str(item.get("name") or ""),
                        arguments=str(item.get("arguments") or "{}"),
                    ))
            elif etype == _EV_COMPLETED:
                usage = (evt.get("response") or {}).get("usage") or {}
                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)
                details = usage.get("input_tokens_details") or {}
                cached_tokens = int(details.get("cached_tokens") or 0)
                done = True
            elif etype == _EV_FAILED:
                err = (evt.get("response") or {}).get("error") or {}
                yield ChatResponse(
                    finish_reason="error",
                    message=f"responses failed: {err.get('message') or err}",
                )
                return

        if tool_calls:
            yield ChatResponse(
                tool_calls=tool_calls,
                finish_reason="tool_calls",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
            )
        elif done:
            yield ChatResponse(
                finish_reason="stop",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
            )

    # ── 非流式 ───────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        from app.providers.session_context import get_llm_effort

        payload = self.build_payload(
            messages, model, max_tokens=max_tokens, temperature=temperature,
            tools=tools, effort=get_llm_effort(), stream=False,
        )
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        client = await self._get_client()
        try:
            resp = await client.post(f"{self._base}/responses", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            return ChatResponse(finish_reason="error", message=f"responses transport error: {exc}")
        if resp.status_code >= 300:
            return ChatResponse(
                finish_reason="error",
                message=f"responses API error {resp.status_code}: {resp.text[:400]}",
            )
        return self.parse_response(resp.json())

    def parse_response(self, body: dict[str, Any]) -> ChatResponse:
        """解析非流式响应（**纯函数**，可单测）。"""
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for item in body.get("output") or []:
            itype = item.get("type")
            if itype == "message":
                for part in item.get("content") or []:
                    if part.get("type") in ("output_text", "text"):
                        text_parts.append(str(part.get("text") or ""))
            elif itype == "function_call":
                tool_calls.append(ToolCall(
                    id=str(item.get("call_id") or item.get("id") or ""),
                    name=str(item.get("name") or ""),
                    arguments=str(item.get("arguments") or "{}"),
                ))
        usage = body.get("usage") or {}
        details = usage.get("input_tokens_details") or {}
        finish = "tool_calls" if tool_calls else "stop"
        if body.get("status") == "incomplete":
            finish = "length"
        return ChatResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=finish,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cached_tokens=int(details.get("cached_tokens") or 0),
            model=str(body.get("model") or ""),
        )

    async def embed(self, text: str, model: str | None = None) -> Any:
        """嵌入：Responses 不提供 embed。**如实拒绝**而不是返回空向量（空向量会污染检索）。"""
        raise NotImplementedError(
            "Responses API has no embeddings endpoint; configure a chat-completions "
            "provider (or a dedicated embeddings provider) for embed()."
        )
