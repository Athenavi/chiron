"""Tests for GatewayRouter — 预算扣减必须在流式路径也生效。

背景 bug: chat_stream（所有 Agent 推理的主路径）此前只做预算预检查
(TokenBudget.check)，从不按实际用量 deduct，导致 per-tenant 月度 token
预算（budget:{tenant}:{month} 的 used 字段）永远不增长、超限请求永不
被拒绝。非流式 chat() 路径正确扣减，两条路径行为必须一致。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.gateway.provider import (
    ChatResponse,
    EmbeddingResponse,
    LLMProvider,
)
from app.gateway.router import GatewayRouter


class FakeProvider(LLMProvider):
    """返回预设 chunks 的假 provider"""

    name = "fake"

    def __init__(self, chunks: list[ChatResponse]):
        self._chunks = chunks

    async def chat_stream(self, messages, model, **kwargs):
        for c in self._chunks:
            yield c

    async def chat(self, messages, model, **kwargs):
        return self._chunks[-1]

    async def embed(self, text, model):
        return EmbeddingResponse()

    async def close(self):
        pass


def _make_budget():
    budget = MagicMock()
    budget.check = AsyncMock(return_value=True)
    budget.deduct = AsyncMock()
    return budget


class TestChatStreamBudget:
    @pytest.mark.asyncio
    async def test_chat_stream_deducts_actual_tokens(self):
        """流式推理结束后必须按实际 token 用量扣减预算（与 chat() 一致）。

        若不扣减，流式调用（Agent 主路径）永远不会消耗 used 额度，
        月度预算限制形同虚设——这是本测试要防住的回归。
        """
        provider = FakeProvider([
            ChatResponse(content="你好"),
            ChatResponse(finish_reason="stop", input_tokens=120, output_tokens=80),
        ])
        budget = _make_budget()
        router = GatewayRouter(providers={"fake": provider}, budget=budget)

        chunks = [c async for c in router.chat_stream([], "fake-model", tenant_id="t1")]

        assert chunks[-1].finish_reason == "stop"
        budget.deduct.assert_awaited_once_with("t1", 200)

    @pytest.mark.asyncio
    async def test_chat_stream_no_deduct_without_tokens(self):
        """无 token 用量时不应触发扣减（deduct 内部对 <=0 也会忽略）。"""
        provider = FakeProvider([
            ChatResponse(content="hi", finish_reason="stop"),
        ])
        budget = _make_budget()
        router = GatewayRouter(providers={"fake": provider}, budget=budget)

        [c async for c in router.chat_stream([], "fake-model", tenant_id="t1")]

        budget.deduct.assert_not_called()

    @pytest.mark.asyncio
    async def test_chat_stream_provider_error_does_not_deduct(self):
        """provider 抛异常时（error 路径）不扣减，与 chat() 的异常语义一致。"""
        class FailingProvider(FakeProvider):
            async def chat_stream(self, messages, model, **kwargs):
                if False:
                    yield  # 保持 async generator 形态
                raise RuntimeError("connection reset")

        budget = _make_budget()
        router = GatewayRouter(providers={"fake": FailingProvider([])}, budget=budget)

        chunks = [c async for c in router.chat_stream([], "fake-model", tenant_id="t1")]

        assert chunks[-1].finish_reason == "error"
        budget.deduct.assert_not_called()
