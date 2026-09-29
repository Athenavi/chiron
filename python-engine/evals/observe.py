"""事件流 → 观测（E2 的核心逻辑，纯函数、可单测）。

引擎的 SSE 事件形状（见 `app/agent/runtime.py` 的 yield 点）：

| 事件 | 关键字段 |
|---|---|
| `text` | `content`（思考增量以 `[thinking]…[/thinking]` 包裹） |
| `tool_call` | `tool_call_id` / `tool_name` / `tool_arguments` |
| `tool_result` | `tool_call_id` / `tool_name` / `content`（JSON 字符串，失败时含 `error`） |
| `trace_span` | `span_name`（`llm_call` = 一次模型调用）+ `content`（JSON 明细） |
| `done` | `input_tokens` / `output_tokens` |
| `guardrail_blocked` / `error` | `content` / `error` |

**步数口径**：`steps` 取 `llm_call` span 的数量（= 模型调用回合数），而**不是**工具调用数 ——
一轮里可以有任意多个工具调用，用后者会把"回合"与"步"混为一谈，efficiency 断言随之失真。
"""

from __future__ import annotations

import json
from typing import Any

from evals.assertions import Observation

_THINKING_OPEN = "[thinking]"
_THINKING_CLOSE = "[/thinking]"


def _strip_thinking(text: str) -> str:
    """去掉思考增量，只留正文。

    思考不属于"最终回答"：算进 `final_text` 会让 `final_text_contains` 在"模型想过但没说"
    的情况下误判为通过。
    """
    if _THINKING_OPEN not in text:
        return text
    out: list[str] = []
    depth = 0
    index = 0
    while index < len(text):
        if text.startswith(_THINKING_OPEN, index):
            depth += 1
            index += len(_THINKING_OPEN)
            continue
        if text.startswith(_THINKING_CLOSE, index):
            depth = max(0, depth - 1)
            index += len(_THINKING_CLOSE)
            continue
        if depth == 0:
            out.append(text[index])
        index += 1
    return "".join(out)


def _span_name_of(raw: Any) -> str:
    """从 `trace_span.content`（JSON 字符串）里取 `span_name`。"""
    if not isinstance(raw, str):
        return ""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if isinstance(payload, dict):
        return str(payload.get("span_name", ""))
    return ""


def _extract_error(raw: Any) -> str | None:
    """从 `tool_result.content`（JSON 字符串）里取 `error` 字段。

    "工具被拒"与"工具没被调用"必须区分开：前者是本项目最关心的护栏证据。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(payload, dict):
        error = payload.get("error")
        if error:
            return str(error)
    return None


def observation_from_events(events: list[dict[str, Any]]) -> Observation:
    """把 SSE 事件流归约为 `Observation`。"""
    obs = Observation(events=list(events))
    texts: list[str] = []
    calls: dict[str, dict[str, Any]] = {}
    llm_calls = 0

    for evt in events:
        etype = str(evt.get("type", ""))
        if etype == "text":
            texts.append(_strip_thinking(str(evt.get("content", ""))))
        elif etype == "tool_call":
            key = str(evt.get("tool_call_id", "")) or f"call-{len(calls)}"
            calls[key] = {
                "name": str(evt.get("tool_name", "")),
                "args": str(evt.get("tool_arguments", "")),
                "error": None,
            }
        elif etype == "tool_result":
            key = str(evt.get("tool_call_id", "")) or f"call-{len(calls)}"
            entry = calls.setdefault(
                key, {"name": str(evt.get("tool_name", "")), "args": "", "error": None}
            )
            entry["error"] = _extract_error(evt.get("content"))
        elif etype == "trace_span":
            span = str(evt.get("span_name", "")) or _span_name_of(evt.get("content"))
            if span == "llm_call":
                llm_calls += 1
        elif etype == "done":
            obs.tokens = int(evt.get("input_tokens") or 0) + int(
                evt.get("output_tokens") or 0
            )

    obs.final_text = "".join(texts)
    obs.tool_calls = list(calls.values())
    obs.steps = llm_calls
    return obs
