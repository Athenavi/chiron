"""LLM Provider 流式解析测试"""
from unittest.mock import AsyncMock, MagicMock

from app.providers.openai import OpenAIProvider

# ── mock OpenAI chunk 结构 ───────────────────────────────


class _Usage:
    def __init__(self, prompt_tokens, completion_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _Delta:
    def __init__(self, reasoning_content=None, content=None, tool_calls=None):
        self.reasoning_content = reasoning_content
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class _Chunk:
    def __init__(self, choices, usage=None):
        self.choices = choices
        self.usage = usage


def _make_provider(chunks):
    """构造一个 chat.completions.create 返回给定 chunk 序列的 OpenAIProvider"""
    provider = OpenAIProvider(api_key="test-key")

    async def fake_chunks():
        for c in chunks:
            yield c

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=fake_chunks())
    provider._client = client
    return provider


async def _collect(provider):
    result = []
    async for c in provider.chat_stream(messages=[], model="deepseek-chat"):
        result.append(c)
    return result


# ── 测试 ─────────────────────────────────────────────────


async def test_usage_yielded_only_once_when_finish_chunk_carries_usage():
    """DeepSeek 标准流：最后一个 chunk 同时携带 finish_reason 与 usage。

    usage 必须只 yield 一次（token 用量/计费依赖累加语义），
    重复 yield 会让调用方把 token 统计翻倍。
    """
    provider = _make_provider([
        _Chunk([_Choice(_Delta(reasoning_content="思考一"))]),
        _Chunk([_Choice(_Delta(reasoning_content="思考二"))]),
        _Chunk([_Choice(_Delta(content="最终回答"))]),
        _Chunk([_Choice(_Delta(), finish_reason="stop")], usage=_Usage(prompt_tokens=10, completion_tokens=20)),
    ])

    chunks = await _collect(provider)

    input_total = sum(c.input_tokens for c in chunks)
    output_total = sum(c.output_tokens for c in chunks)
    assert input_total == 10, f"input_tokens 应只累计一次（10），实际 {input_total}"
    assert output_total == 20, f"output_tokens 应只累计一次（20），实际 {output_total}"


async def test_usage_yielded_once_when_usage_in_separate_chunk():
    """usage 单独成 chunk（无 choices 无 finish_reason）时也要恰好 yield 一次。"""
    provider = _make_provider([
        _Chunk([_Choice(_Delta(reasoning_content="思考一"))]),
        _Chunk([_Choice(_Delta(), finish_reason="stop")]),
        _Chunk([], usage=_Usage(prompt_tokens=10, completion_tokens=20)),
    ])

    chunks = await _collect(provider)

    input_total = sum(c.input_tokens for c in chunks)
    output_total = sum(c.output_tokens for c in chunks)
    assert input_total == 10, f"input_tokens 应只累计一次（10），实际 {input_total}"
    assert output_total == 20, f"output_tokens 应只累计一次（20），实际 {output_total}"


async def test_reasoning_content_yielded_as_increments_only():
    """reasoning_content 必须以增量 yield：调用方（runtime.py）按增量累加，
    若末尾再 yield 一次完整累积文本，思考内容会被重复拼接到 2~3 倍。
    """
    provider = _make_provider([
        _Chunk([_Choice(_Delta(reasoning_content="思考一"))]),
        _Chunk([_Choice(_Delta(reasoning_content="思考二"))]),
        _Chunk([_Choice(_Delta(), finish_reason="stop")], usage=_Usage(prompt_tokens=10, completion_tokens=20)),
    ])

    chunks = await _collect(provider)

    parts = [c.reasoning_content for c in chunks if c.reasoning_content]
    assert parts == ["思考一", "思考二"], (
        f"reasoning_content 应按增量逐段 yield（每段只出现一次），实际 {parts!r}"
    )
