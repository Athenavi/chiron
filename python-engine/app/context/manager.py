# Context window management and compression
from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# System message overhead per the OpenAI/Anthropic convention
_SYSTEM_OVERHEAD_TOKENS = 4  # role framing per message

# Global background task tracking for graceful shutdown
_bg_tasks: set[asyncio.Task[None]] = set()


def _track_bg_task(task: asyncio.Task[None]) -> None:
    """Track a background task for graceful shutdown."""
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def wait_background_tasks(timeout: float = 10.0) -> None:
    """Wait for all background tasks to complete with a timeout.

    Called during application shutdown to ensure pending tasks are not lost.
    """
    if not _bg_tasks:
        return
    logger.info(
        "Waiting for %d background tasks to complete (timeout=%.1fs)",
        len(_bg_tasks),
        timeout,
    )
    done, pending = await asyncio.wait(
        list(_bg_tasks),
        timeout=timeout,
        return_when=asyncio.ALL_COMPLETED,
    )
    if pending:
        logger.warning(
            "%d background tasks did not complete within %.1fs", len(pending), timeout
        )
        for task in pending:
            task.cancel()
    if done:
        logger.info("%d background tasks completed", len(done))


class ContextManager:
    """Manages conversation context windows with automatic compression.

    Handles:
    - Approximate token counting (no tiktoken dependency)
    - Context window management
    - Automatic compression when approaching limits
    - Message history trimming
    """

    def __init__(self, max_tokens: int = 200_000, compression_threshold: float = 0.8):
        """
        Args:
            max_tokens: Maximum context window size in tokens.
            compression_threshold: Ratio of max_tokens at which compression kicks in
                (e.g. 0.8 means compress when usage exceeds 80 %).
        """
        self.max_tokens = max_tokens
        self.compression_threshold = compression_threshold

    # ------------------------------------------------------------------
    # Token counting
    # ------------------------------------------------------------------

    @staticmethod
    def count_tokens(text: str) -> int:
        """Approximate token count for a string.

        Heuristic:
        - English / ASCII: ~4 characters per token
        - CJK / wide characters: ~2 characters per token
        - Minimum 1 token for any non-empty text

        This is intentionally simple — it avoids a hard dependency on tiktoken
        while being accurate enough for budget management.
        """
        if not text:
            return 0

        ascii_chars = 0
        non_ascii_chars = 0

        for ch in text:
            if ord(ch) < 128:
                ascii_chars += 1
            else:
                non_ascii_chars += 1

        # English: ~4 chars/token, CJK/other: ~2 chars/token
        tokens = math.ceil(ascii_chars / 4) + math.ceil(non_ascii_chars / 2)
        return max(tokens, 1) if text.strip() else 0

    def count_message_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Count total tokens across a list of message dicts.

        Each message is expected to be a dict with at least 'role' and 'content'.
        A small per-message overhead is added for framing.
        """
        total = 0
        for msg in messages:
            # Per-message framing overhead
            total += _SYSTEM_OVERHEAD_TOKENS

            content = msg.get("content", "")
            if isinstance(content, str):
                total += self.count_tokens(content)
            elif isinstance(content, list):
                # Multi-modal content: count text parts
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        total += self.count_tokens(part.get("text", ""))
                    elif isinstance(part, str):
                        total += self.count_tokens(part)

            # Count tool call arguments if present
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        args = tc.get("arguments", "")
                        if isinstance(args, str):
                            total += self.count_tokens(args)

        return total

    # ------------------------------------------------------------------
    # Compression
    # ------------------------------------------------------------------

    async def compress(
        self,
        messages: list[dict[str, Any]],
        gateway: Any = None,
        memory_service: Any = None,
    ) -> list[dict[str, Any]]:
        """Compress messages if approaching the token limit.

        降级链实现：
        1. LLM 摘要 → 重试 1 次 → 仍失败则降级到提取式摘要
        2. 最终失败则标记 degraded=True 并调用 trim_to_fit 硬截断
        3. 回合结束后将待摘内容通过 memory_service 补交后台巩固队列

        Args:
            messages: List of message dicts (role, content, ...).
            gateway: Optional LLM gateway for generating summaries.
            memory_service: Optional MemoryService for degraded content submission.

        Returns:
            Compressed message list (may be the same reference if no compression
            was needed).
        """
        total_tokens = self.count_message_tokens(messages)
        threshold_tokens = int(self.max_tokens * self.compression_threshold)

        if total_tokens <= threshold_tokens:
            return messages

        logger.info(
            "Context compression triggered: %d tokens > threshold %d",
            total_tokens,
            threshold_tokens,
        )

        # Split messages into: system | middle (to compress) | tail (to keep)
        system_msgs: list[dict[str, Any]] = []
        other_msgs: list[dict[str, Any]] = []

        for msg in messages:
            if msg.get("role") == "system" and not system_msgs:
                system_msgs.append(msg)
            else:
                other_msgs.append(msg)

        # How many tail messages to preserve (at least 4 for a sensible conversation)
        keep_tail = max(4, len(other_msgs) // 3)

        if len(other_msgs) <= keep_tail:
            # Nothing to compress
            return messages

        middle = other_msgs[:-keep_tail]
        tail = other_msgs[-keep_tail:]

        # ── 降级链：LLM 摘要 → 重试 → 降级 → 硬截断 ──
        degraded = False
        llm_failed = False
        summary_text = ""

        # 尝试 LLM 摘要（带重试）
        if gateway is not None:
            for attempt in range(2):  # 最多重试 1 次
                try:
                    summary_text = await self._llm_summarise(middle, gateway)
                    break
                except Exception as exc:
                    logger.warning(
                        "LLM summarisation attempt %d failed: %s", attempt + 1, exc
                    )
                    llm_failed = True
                    if attempt == 0:
                        logger.info("Retrying LLM summarisation...")
                    else:
                        logger.error(
                            "LLM summarisation failed after retry, using fallback"
                        )

        # LLM 失败或未配置 → 降级到提取式摘要
        if not summary_text:
            summary_text = self._extractive_summary(middle)
            degraded = True
            logger.warning("Using extractive summary (degraded mode)")

        # 构建摘要消息
        summary_msg_content = (
            "[Context Summary]\n"
            "The following is a compressed summary of earlier conversation messages "
            "that were removed to stay within the context window.\n\n"
        )
        if degraded:
            summary_msg_content += "[DEGRADED MODE - extractive summary]\n\n"
        summary_msg_content += summary_text

        summary_msg = {
            "role": "system",
            "content": summary_msg_content,
        }

        compressed = system_msgs + [summary_msg] + tail

        # 如果摘要后仍超出预算，执行硬截断
        new_tokens = self.count_message_tokens(compressed)
        if new_tokens > self.max_tokens:
            logger.warning(
                "Compression still over budget (%d > %d), using trim_to_fit",
                new_tokens,
                self.max_tokens,
            )
            compressed = self.trim_to_fit(compressed)
            degraded = True

        # 降级后：将被压缩的中间消息提交到后台巩固队列
        if degraded and memory_service is not None:
            try:
                # 异步提交被压缩内容到 L3 巩固，追踪以便优雅关闭
                task = asyncio.create_task(
                    self._submit_degraded_content(middle, memory_service)
                )
                _track_bg_task(task)
            except Exception as exc:
                logger.warning("Failed to submit degraded content: %s", exc)

        logger.info(
            "Compression result: %d → %d tokens (%.1f%% reduction, degraded=%s, llm_failed=%s)",
            total_tokens,
            self.count_message_tokens(compressed),
            (
                (1 - self.count_message_tokens(compressed) / total_tokens) * 100
                if total_tokens
                else 0
            ),
            degraded,
            llm_failed,
        )

        return compressed

    async def _summarise(
        self, messages: list[dict[str, Any]], gateway: Any = None
    ) -> str:
        """Produce a text summary of *messages*.

        If *gateway* is supplied and exposes ``chat``, ask the LLM to summarise.
        Otherwise build a lightweight extraction-based summary.
        """
        if gateway is not None:
            try:
                return await self._llm_summarise(messages, gateway)
            except Exception as exc:
                logger.warning("LLM summarisation failed, falling back: %s", exc)

        # Fallback: extract key lines from each message
        parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                # Take the first 200 chars of each message as a snippet
                snippet = content[:200].replace("\n", " ")
                parts.append(f"[{role}]: {snippet}")
        return "\n".join(parts) if parts else "(no content)"

    @staticmethod
    async def _llm_summarise(
        messages: list[dict[str, Any]], gateway: Any
    ) -> str:
        """Use the gateway to produce a real summary."""
        summary_prompt = [
            {
                "role": "system",
                "content": (
                    "You are a context compressor. Summarise the following conversation "
                    "messages concisely, preserving all key facts, decisions, and context "
                    "needed to continue the conversation. Reply with the summary only."
                ),
            },
            {
                "role": "user",
                "content": "\n".join(
                    f"[{m.get('role', 'user')}]: {m.get('content', '')}"
                    for m in messages
                    if isinstance(m.get("content"), str)
                ),
            },
        ]
        # Try the gateway's non-streaming chat interface
        from app.config import settings

        resp = await gateway.chat(
            messages=summary_prompt, model=settings.default_model, max_tokens=1024
        )
        return (
            getattr(resp, "content", "") or "(summary generation produced no content)"
        )

    # ------------------------------------------------------------------
    # Trimming
    # ------------------------------------------------------------------

    def trim_to_fit(
        self, messages: list[dict[str, Any]], max_tokens: int | None = None
    ) -> list[dict[str, Any]]:
        """Remove oldest non-system messages until the total fits within *max_tokens*.

        The system prompt (first system message) is always preserved.

        Args:
            messages: List of message dicts.
            max_tokens: Token budget (defaults to self.max_tokens).

        Returns:
            A (possibly shorter) message list whose token count ≤ *max_tokens*.
        """
        budget = max_tokens if max_tokens is not None else self.max_tokens

        if self.count_message_tokens(messages) <= budget:
            return messages

        # Separate the system message(s) from the rest
        system_msgs: list[dict[str, Any]] = []
        other_msgs: list[dict[str, Any]] = []

        for msg in messages:
            if msg.get("role") == "system" and not system_msgs:
                system_msgs.append(msg)
            else:
                other_msgs.append(msg)

        system_tokens = self.count_message_tokens(system_msgs)
        remaining_budget = budget - system_tokens

        # Walk from the end (most recent) backwards, accumulating until we hit the budget
        kept: list[dict[str, Any]] = []
        used = 0

        for msg in reversed(other_msgs):
            msg_tokens = self.count_message_tokens([msg])
            if used + msg_tokens > remaining_budget and kept:
                break
            kept.append(msg)
            used += msg_tokens

        kept.reverse()

        result = system_msgs + kept
        logger.info(
            "Trimmed messages: %d → %d (%d tokens)",
            len(messages),
            len(result),
            self.count_message_tokens(result),
        )
        return result

    # ------------------------------------------------------------------
    # 降级链辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _extractive_summary(messages: list[dict[str, Any]]) -> str:
        """提取式摘要（LLM 不可用时的降级方案）。

        从每个消息中提取关键内容，拼接成简明摘要。
        """
        parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                # 提取前 150 字符作为摘要片段
                snippet = content[:150].replace("\n", " ")
                if len(content) > 150:
                    snippet += "..."
                parts.append(f"[{role}]: {snippet}")
        return "\n".join(parts) if parts else "(no content to summarise)"

    @staticmethod
    async def _submit_degraded_content(
        messages: list[dict[str, Any]],
        memory_service: Any,
    ) -> None:
        """将降级模式下被压缩的内容提交到后台巩固队列。

        Args:
            messages: 被压缩的中间消息列表。
            memory_service: MemoryService 实例。
        """
        try:
            # 构造摘要内容并提交
            convo_msgs = [
                m
                for m in messages
                if m.get("role") in ("user", "assistant") and m.get("content")
            ]
            if convo_msgs:
                # 从第一条消息获取 scope 信息
                scope = None
                for msg in convo_msgs:
                    if hasattr(msg, "get"):
                        tenant_id = msg.get("tenant_id", "default")
                        user_id = msg.get("user_id", "unknown")
                        if tenant_id and user_id:
                            from app.memory.layers import Scope

                            scope = Scope(
                                tenant_id=tenant_id,
                                user_id=user_id,
                                session_id="",
                            )
                            break

                if scope is None:
                    from app.memory.layers import Scope

                    scope = Scope(
                        tenant_id="default",
                        user_id="unknown",
                        session_id="",
                    )

                # 保存摘要到 L3
                combined_content = "\n".join(
                    f"[{m.get('role', 'user')}]: {m.get('content', '')[:200]}"
                    for m in convo_msgs
                )
                await memory_service.save_summary(
                    scope=scope,
                    content=combined_content,
                    topics=["degraded_compaction"],
                )
                logger.info(
                    "Submitted degraded content to L3 consolidation " "(%d messages)",
                    len(convo_msgs),
                )
        except Exception as exc:
            logger.warning("Failed to submit degraded content to L3: %s", exc)
