"""D2：OpenAI Responses provider 的**纯逻辑**单测（build_payload / parse_response）。

不碰网络：请求体构造与响应解析都是纯函数，且正是"两套形状"最容易搞错的地方。
"""

from __future__ import annotations

from app.gateway.provider import ChatMessage, ToolCall
from app.providers.responses import ResponsesProvider


def _p() -> ResponsesProvider:
    return ResponsesProvider("k", "https://api.openai.com/v1")


def test_system_goes_to_instructions_not_input() -> None:
    payload = _p().build_payload(
        [ChatMessage(role="system", content="be terse"), ChatMessage(role="user", content="hi")],
        "gpt-5",
    )
    assert payload["instructions"] == "be terse"
    # system **不得**出现在 input 里 —— Responses 不接受该角色，硬塞会 400。
    assert all(item.get("role") != "system" for item in payload["input"])


def test_user_message_shape() -> None:
    payload = _p().build_payload([ChatMessage(role="user", content="hi")], "m")
    assert payload["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]


def test_tool_result_becomes_function_call_output() -> None:
    payload = _p().build_payload(
        [ChatMessage(role="tool", content='{"ok":true}', tool_call_id="c1")], "m"
    )
    assert payload["input"] == [
        {"type": "function_call_output", "call_id": "c1", "output": '{"ok":true}'}
    ]


def test_assistant_tool_calls_become_function_call_items() -> None:
    payload = _p().build_payload(
        [ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="read_file", arguments="{}")])],
        "m",
    )
    kinds = [i["type"] for i in payload["input"]]
    assert "function_call" in kinds
    call = next(i for i in payload["input"] if i["type"] == "function_call")
    assert call["call_id"] == "c1" and call["name"] == "read_file"


def test_tools_are_flattened() -> None:
    payload = _p().build_payload(
        [ChatMessage(role="user", content="x")],
        "m",
        tools=[{"type": "function", "function": {"name": "t", "description": "d", "parameters": {"type": "object"}}}],
    )
    assert payload["tools"] == [
        {"type": "function", "name": "t", "description": "d", "parameters": {"type": "object"}}
    ]


def test_reasoning_uses_nested_contract() -> None:
    # 本 provider 的契约是 nested：reasoning 子对象，**不是**顶层 reasoning_effort。
    payload = _p().build_payload([ChatMessage(role="user", content="x")], "gpt-5", effort="low")
    assert payload.get("reasoning") == {"effort": "low"}
    assert "reasoning_effort" not in payload


def test_no_effort_means_no_reasoning_field() -> None:
    payload = _p().build_payload([ChatMessage(role="user", content="x")], "m", effort="")
    assert "reasoning" not in payload


def test_base_url_strips_trailing_responses_segment() -> None:
    p = ResponsesProvider("k", "https://x/v1/responses/")
    # 调用方多写了 /responses 时不能拼成 /responses/responses。
    assert p._base == "https://x/v1"


def test_parse_response_text_and_usage() -> None:
    body = {
        "model": "gpt-5",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}],
        "usage": {"input_tokens": 10, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 7}},
    }
    r = _p().parse_response(body)
    assert r.content == "hello"
    assert r.finish_reason == "stop"
    assert (r.input_tokens, r.output_tokens, r.cached_tokens) == (10, 3, 7)
    assert r.model == "gpt-5"


def test_parse_response_tool_calls() -> None:
    body = {
        "output": [{"type": "function_call", "call_id": "c9", "name": "ls", "arguments": "{}"}],
        "usage": {},
    }
    r = _p().parse_response(body)
    assert r.finish_reason == "tool_calls"
    assert r.tool_calls[0].name == "ls" and r.tool_calls[0].id == "c9"


def test_parse_response_incomplete_maps_to_length() -> None:
    r = _p().parse_response({"status": "incomplete", "output": [], "usage": {}})
    assert r.finish_reason == "length"


async def test_embed_refuses_instead_of_returning_empty_vector() -> None:
    # 空向量会污染检索（相似度恒为 0/NaN），所以这里**如实拒绝**。
    import pytest

    with pytest.raises(NotImplementedError):
        await _p().embed("x")
