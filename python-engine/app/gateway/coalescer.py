"""
请求合并器 — 将相同 Prompt 请求合并，减少 API 调用
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class RequestCoalescer:
    """
    请求合并器

    将相同 Prompt 的请求在短时间内合并，减少 API 调用

    工作原理：
    1. 收到请求时，计算 prompt 哈希
    2. 如果已有相同请求在处理，等待其结果
    3. 否则创建新请求，其他相同请求等待
    4. 第一个请求完成后，结果广播给所有等待者
    """

    def __init__(
        self,
        window_ms: int = 10,
        max_pending: int = 1000,
    ):
        """
        初始化请求合并器

        Args:
            window_ms: 合并窗口（毫秒），在此窗口内的相同请求会被合并
            max_pending: 最大并发请求数
        """
        self._window_ms = window_ms
        self._max_pending = max_pending
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._pending_count: int = 0
        self._lock = asyncio.Lock()
        # coalesced_rate 是浮点（saved_calls 等是整数），故显式标注为 Any 值
        self._stats: dict[str, Any] = {
            "total_requests": 0,
            "coalesced_requests": 0,
            "saved_calls": 0,
        }

    async def coalesce(
        self,
        prompt: str,
        callback: Any,
        **kwargs: Any,
    ) -> Any:
        """
        合并请求

        Args:
            prompt: 提示词
            callback: 实际调用的回调函数
            **kwargs: 传递给回调的参数

        Returns:
            调用结果
        """
        self._stats["total_requests"] += 1

        # 计算请求哈希
        key = self._compute_key(prompt, kwargs)

        # 检查是否有相同请求在处理（快速路径，锁内无 await）
        future: asyncio.Future[Any] | None = None
        async with self._lock:
            if key in self._pending:
                self._stats["coalesced_requests"] += 1
                self._stats["saved_calls"] += 1
                future = self._pending[key]

        if future is not None:
            logger.debug("Coalescing request with key %s", key)
            return await future

        # 创建新 Future（双重检查锁定）
        async with self._lock:
            if key in self._pending:
                future = self._pending[key]
            elif self._pending_count >= self._max_pending:
                logger.warning(
                    "Max pending requests reached, proceeding without coalescing"
                )
                return await callback(**kwargs)
            else:
                future = asyncio.get_running_loop().create_future()
                self._pending[key] = future
                self._pending_count += 1

        try:
            # 等待一小段时间，让更多相同请求加入
            await asyncio.sleep(self._window_ms / 1000.0)

            # 执行实际调用
            result = await callback(**kwargs)

            # 设置结果
            if not future.done():
                future.set_result(result)

            return result

        except asyncio.CancelledError:
            # 保持 CancelledError 传播
            if not future.done():
                future.cancel()
            raise
        except Exception as e:
            if not future.done():
                future.set_exception(e)
            raise

        finally:
            # 清理
            async with self._lock:
                if key in self._pending:
                    del self._pending[key]
                self._pending_count -= 1

    def _compute_key(self, prompt: str, kwargs: dict[str, Any]) -> str:
        """计算请求哈希（内置 hash() 受 PYTHONHASHSEED 影响不可靠，
        改确定性 md5 + json.dumps 确保键值序列化唯一）。"""
        kwargs_str = json.dumps(kwargs, sort_keys=True, ensure_ascii=False)
        key_str = f"{prompt}|{kwargs_str}"
        return hashlib.md5(key_str.encode("utf-8")).hexdigest()

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        stats = self._stats.copy()
        stats["pending_count"] = self._pending_count

        if stats["total_requests"] > 0:
            stats["coalesced_rate"] = (
                stats["coalesced_requests"] / stats["total_requests"]
            )
        else:
            stats["coalesced_rate"] = 0

        return stats


class PromptHasher:
    """
    Prompt 哈希计算器

    用于语义缓存和请求合并
    """

    @staticmethod
    def hash(prompt: str, **kwargs: Any) -> str:
        """
        计算 Prompt 哈希

        Args:
            prompt: 提示词
            **kwargs: 其他参数

        Returns:
            哈希值（16进制字符串）
        """
        # 标准化 prompt
        normalized = prompt.strip().lower()

        # 添加关键参数
        parts = [normalized]
        for k in sorted(kwargs.keys()):
            if k in ("model", "temperature", "max_tokens"):
                parts.append(f"{k}={kwargs[k]}")

        content = "|".join(parts)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    @staticmethod
    def semantic_hash(embedding: list[float], prefix_dims: int = 64) -> str:
        """
        计算语义哈希（用于语义缓存）

        Args:
            embedding: 嵌入向量
            prefix_dims: 使用前N维计算哈希

        Returns:
            哈希值
        """
        # 只使用前N维
        prefix = embedding[:prefix_dims]

        # 量化为整数
        quantized = [int(v * 1000) for v in prefix]

        # 计算哈希
        content = ",".join(str(v) for v in quantized)
        return hashlib.sha256(content.encode()).hexdigest()[:16]
