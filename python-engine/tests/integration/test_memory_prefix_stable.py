"""PR-3 Task 25: 集成测试 - compaction 前缀稳定。

验证 ContextManager.compress 在压缩后：
1. System 消息（前缀）与压缩前逐字节一致。
2. 保留的尾部消息（recent window）与压缩前逐字节一致。
3. 摘要块插入到 system 和 tail 之间，不破坏前后两段。
4. 压缩的降级路径（无 gateway）同样稳定。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.context.manager import ContextManager

# ── Helpers ──────────────────────────────────────────────────────────


def _make_messages(n_user_turns: int, system_content: str = "You are helpful.") -> list[dict[str, Any]]:
    """构造 [system, user, assistant, user, assistant, ...] 序列。"""
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
    for i in range(n_user_turns):
        messages.append({"role": "user", "content": f"user-turn-{i}: " + ("x" * 200)})
        messages.append({"role": "assistant", "content": f"assistant-turn-{i}: " + ("y" * 200)})
    return messages


# ── Tests ─────────────────────────────────────────────────────────────


class TestPrefixStable:
    """压缩前后 system 前缀与 tail 窗口必须逐字节一致。"""

    @pytest.mark.asyncio
    async def test_system_prefix_identical_after_compression(self):
        # max_tokens 设为足够容纳 system + summary + tail，避免硬截断
        cm = ContextManager(max_tokens=4000, compression_threshold=0.5)
        original = _make_messages(20)
        system_before = original[0]

        compressed = await cm.compress(original)

        assert compressed[0] == system_before
        assert compressed[0]["content"] == system_before["content"]

    @pytest.mark.asyncio
    async def test_tail_messages_identical_after_compression(self):
        cm = ContextManager(max_tokens=4000, compression_threshold=0.5)
        original = _make_messages(20)
        # tail 为最后 8 条（4 轮对话），压缩后应当完全保留
        tail_before = original[-8:]

        compressed = await cm.compress(original)

        tail_after = compressed[-8:]
        assert tail_after == tail_before

    @pytest.mark.asyncio
    async def test_summary_inserted_between_system_and_tail(self):
        cm = ContextManager(max_tokens=4000, compression_threshold=0.5)
        original = _make_messages(20)

        compressed = await cm.compress(original)

        assert len(compressed) < len(original)
        # 第二条消息为摘要块
        summary_msg = compressed[1]
        assert summary_msg["role"] == "system"
        assert "Context Summary" in summary_msg["content"]

    @pytest.mark.asyncio
    async def test_no_compression_when_below_threshold(self):
        cm = ContextManager(max_tokens=100_000, compression_threshold=0.8)
        original = _make_messages(4)

        compressed = await cm.compress(original)

        assert compressed == original

    @pytest.mark.asyncio
    async def test_multiple_compressions_idempotent_prefix(self):
        """重复压缩时前缀仍然稳定（不会多次嵌套摘要）。"""
        cm = ContextManager(max_tokens=4000, compression_threshold=0.5)
        original = _make_messages(20)
        system_before = original[0]

        first = await cm.compress(original)
        second = await cm.compress(first)

        # 两次压缩后 system 前缀仍保持原始内容
        assert second[0] == system_before
        # 摘要块只出现一次（不会产生摘要的摘要）
        summary_count = sum(1 for m in second if "Context Summary" in m.get("content", ""))
        assert summary_count == 1


class TestCacheHitRateUnaffected:
    """压缩不应影响后续 token 计数确定性。"""

    @pytest.mark.asyncio
    async def test_compressed_message_count_deterministic(self):
        cm = ContextManager(max_tokens=4000, compression_threshold=0.5)
        original = _make_messages(20)

        r1 = await cm.compress(original)
        r2 = await cm.compress(original)

        assert len(r1) == len(r2)
        assert r1 == r2

    def test_token_count_deterministic(self):
        cm = ContextManager(max_tokens=200)
        msgs = _make_messages(5)
        assert cm.count_message_tokens(msgs) == cm.count_message_tokens(msgs)

    @pytest.mark.asyncio
    async def test_degraded_compression_structure_stable(self):
        """无 gateway 时降级到提取式摘要，结构仍然稳定。"""
        cm = ContextManager(max_tokens=4000, compression_threshold=0.5)
        original = _make_messages(20)

        compressed = await cm.compress(original, gateway=None)

        assert compressed[0] == original[0]
        assert compressed[1]["role"] == "system"
        assert "Context Summary" in compressed[1]["content"]
        assert compressed[-8:] == original[-8:]
