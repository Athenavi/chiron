"""LLM client — 统一嵌入入口（供 RAG 检索等模块导入）。

策略与 RAGBuilder 保持一致：本地模型（settings.local_embedding_model）优先，
Gateway API 回退，确保存储与查询嵌入口径一致。
gateway 由 main.py 启动时通过 bind_gateway 注入（与 tools/graph.py 等模块的模式一致）。

fail-loud：未配置任何后端（本地模型 + gateway 均不可用）时抛出 RuntimeError，
绝不返回零向量伪装成功。
"""

from __future__ import annotations

import asyncio
import logging

from app.config import settings

logger = logging.getLogger(__name__)


class LLMClient:
    """嵌入客户端：本地模型优先、Gateway API 回退"""

    def __init__(self):
        self._gateway = None
        self._local_encoder = None
        self._local_encoder_lock = asyncio.Lock()

    def bind_gateway(self, gateway) -> None:
        """注入 GatewayRouter（main.py 启动时调用一次）"""
        self._gateway = gateway

    async def embed(self, text: str) -> list[float]:
        embedding = await self._get_local_embedding(text)
        if embedding is None:
            embedding = await self._get_api_embedding(text)
        if not embedding:
            raise RuntimeError(
                "embedding unavailable: no local model configured "
                "(settings.local_embedding_model) and no LLM gateway bound "
                "(bind_gateway) or embed request failed — "
                "refusing to return zero vectors"
            )
        return embedding

    async def _get_local_embedding(self, text: str) -> list[float] | None:
        """使用本地模型计算嵌入（可插拔，默认关闭）

        配置 settings.local_embedding_model（如 BGE/Jina 的本地路径/模型名）后启用；
        依赖 sentence-transformers（可选 extra）。任何失败均回退到 API 嵌入。
        """
        if not settings.local_embedding_model:
            return None
        try:
            async with self._local_encoder_lock:
                if self._local_encoder is None:
                    from sentence_transformers import SentenceTransformer

                    self._local_encoder = await asyncio.to_thread(
                        SentenceTransformer, settings.local_embedding_model
                    )
            vector = await asyncio.to_thread(self._local_encoder.encode, text)
            return [float(x) for x in vector.tolist()]
        except Exception as e:
            logger.warning("本地嵌入计算失败（回退到 API）: %s", e)
            return None

    async def _get_api_embedding(self, text: str) -> list[float] | None:
        """使用 Gateway API 计算嵌入"""
        if self._gateway is None:
            return None
        try:
            resp = await self._gateway.embed(text, settings.embedding_model)
            return resp.embedding
        except Exception as e:
            logger.warning("API 嵌入计算失败: %s", e)
            return None


llm_client = LLMClient()
