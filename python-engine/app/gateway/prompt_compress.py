"""Prompt 压缩 — 减少 token 消耗，降低 LLM 调用成本。

策略：
1. 对话历史截断：保留最近的 N 轮对话，丢弃中间冗余轮次
2. 消息摘要：将长消息用 LLM 摘要压缩到指定长度
3. 上下文窗口裁剪：对超出 max_context_tokens 的输入做智能裁剪

用法：
  compressor = PromptCompressor(max_context_tokens=4096)
  compressed = await compressor.compress(messages, summarize_fn=None)
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class PromptCompressor:
    """Prompt 压缩器。

    顺序执行：
    1. 对话历史截断（保留最近 N 轮 + system prompt）
    2. 消息摘要（可选，对长消息用 LLM 压缩）
    3. 上下文窗口裁剪（估算 token 数，超出时丢弃最旧消息）
    """

    def __init__(
        self,
        max_context_tokens: int = 4096,
        max_rounds: int = 20,
        max_message_length: int = 2000,
        summarize_threshold: int = 1500,
    ):
        self._max_context_tokens = max_context_tokens
        self._max_rounds = max_rounds
        self._max_message_length = max_message_length
        self._summarize_threshold = summarize_threshold

    # ── token 估算（快速近似，不依赖 tokenizer） ──

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """快速估算 token 数：中文 ~1.5 字符/token，英文 ~4 字符/token。"""
        if not text:
            return 0
        ascii_chars = sum(1 for c in text if ord(c) < 128)
        non_ascii_chars = len(text) - ascii_chars
        return int(ascii_chars / 4 + non_ascii_chars / 1.5 + 1)

    def _estimate_messages_tokens(
        self, messages: list[dict[str, Any]]
    ) -> int:
        """估算消息列表 token 总数。"""
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        total += self._estimate_tokens(
                            part.get("text", "") or part.get("image_url", {}).get("url", "") or ""
                        )
            else:
                total += self._estimate_tokens(str(content))
            total += 10  # 每条消息的 overhead（role, metadata 等）
        return total

    # ── 公开方法 ──

    async def compress(
        self,
        messages: list[dict[str, Any]],
        summarize_fn: Callable[[str], Coroutine[Any, Any, str]] | None = None,
    ) -> list[dict[str, Any]]:
        """压缩消息列表，减少 token 消耗。

        Args:
            messages: OpenAI 格式的消息列表
            summarize_fn: 可选的异步摘要函数，用于压缩长消息

        Returns:
            压缩后的消息列表
        """
        if not messages:
            return messages

        result = list(messages)

        # 1. 对话历史截断
        result = self._truncate_history(result)

        # 2. 消息摘要（可选，对长消息用 LLM 压缩）
        if summarize_fn:
            result = await self._summarize_long_messages(result, summarize_fn)

        # 3. 上下文窗口裁剪
        result = self._trim_context(result)

        return result

    def _truncate_history(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """截断对话历史：保留 system prompt + 最近 max_rounds 轮。"""
        if len(messages) <= self._max_rounds + 1:
            return messages

        # 分离 system prompt
        system_msgs = [m for m in messages if m.get("role") == "system"]
        other_msgs = [m for m in messages if m.get("role") != "system"]

        # 保留最后 max_rounds 条
        kept = other_msgs[-self._max_rounds:]
        truncated = len(other_msgs) - len(kept)
        if truncated > 0:
            logger.info(
                "Truncated %d messages (kept last %d)",
                truncated,
                len(kept),
            )

        return system_msgs + kept

    async def _summarize_long_messages(
        self,
        messages: list[dict[str, Any]],
        summarize_fn: Callable[[str], Coroutine[Any, Any, str]],
    ) -> list[dict[str, Any]]:
        """对超过阈值的长消息进行摘要压缩。"""
        result = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > self._summarize_threshold:
                try:
                    summary = await summarize_fn(content)
                    compressed = dict(msg)
                    compressed["content"] = (
                        f"[压缩摘要] {summary}\n\n"
                        f"(原始长度 {len(content)} 字符，压缩为 {len(summary)} 字符)"
                    )
                    compressed["_compressed"] = True
                    result.append(compressed)
                    logger.info(
                        "Summarized message: %d -> %d chars",
                        len(content),
                        len(summary),
                    )
                except Exception as e:
                    logger.warning("Summarize failed, keeping original: %s", e)
                    result.append(msg)
            else:
                result.append(msg)
        return result

    def _trim_context(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """裁剪上下文：当 token 超出限制时，丢弃最旧的非 system 消息。"""
        total = self._estimate_messages_tokens(messages)
        if total <= self._max_context_tokens:
            return messages

        system_msgs = [m for m in messages if m.get("role") == "system"]
        other_msgs = [m for m in messages if m.get("role") != "system"]

        # 从最旧的消息开始丢弃
        while other_msgs and self._estimate_messages_tokens(
            system_msgs + other_msgs
        ) > self._max_context_tokens:
            dropped = other_msgs.pop(0)
            logger.info(
                "Dropped message (oversized): role=%s, content_len=%d",
                dropped.get("role"),
                len(str(dropped.get("content", ""))),
            )

        if not other_msgs:
            logger.warning("All non-system messages dropped due to context limit")
            # 保留至少一条用户消息
            return system_msgs

        return system_msgs + other_msgs

    def stats(self) -> dict[str, Any]:
        """返回压缩器配置。"""
        return {
            "max_context_tokens": self._max_context_tokens,
            "max_rounds": self._max_rounds,
            "max_message_length": self._max_message_length,
            "summarize_threshold": self._summarize_threshold,
        }
