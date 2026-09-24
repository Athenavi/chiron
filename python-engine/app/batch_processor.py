"""批量处理器 — 支持批量 LLM 推理、批量嵌入和批量知识库索引。

通过 Redis Streams 分发任务，由 QueueWorker 消费。

典型用例：
- 批量嵌入：将一批文本片段批量向量化
- 批量 LLM 调用：对一组 prompt 批量调用 LLM
- 批量知识库索引：批量处理文档块
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


class BatchProcessor:
    """批量任务处理器"""

    def __init__(self, gateway=None, redis=None):
        self._gateway = gateway
        self._redis = redis

    # ── 批量嵌入 ──

    async def embed_batch(
        self, texts: list[str], model: str = "text-embedding-3-small", dim: int = 1536
    ) -> list[list[float]]:
        """批量文本嵌入（带限流和重试）

        Args:
            texts: 文本列表
            model: 嵌入模型名
            dim: 向量维度

        Returns:
            向量列表，顺序与输入一致
        """
        if not self._gateway:
            logger.error("embed_batch: gateway not available")
            return [[] for _ in texts]

        results: list[list[float]] = []
        batch_size = 20  # 每批最多 20 条

        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    embeddings = await self._gateway.embed(
                        texts=batch, model=model, dimensions=dim
                    )
                    if embeddings and len(embeddings) == len(batch):
                        results.extend(embeddings)
                        break
                except Exception as e:
                    logger.warning(
                        "embed_batch retry %d/%d: %s", attempt + 1, max_retries, e
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1 * (attempt + 1))
                    else:
                        logger.error("embed_batch failed after %d retries", max_retries)
                        # 失败时用零向量占位
                        results.extend([[]] * len(batch))

        # 补齐缺失项
        if len(results) < len(texts):
            results.extend([[]] * (len(texts) - len(results)))
        return results

    # ── 批量 LLM 推理 ──

    async def llm_batch(
        self,
        prompts: list[str],
        system_prompt: str = "",
        model: str = "",
        max_concurrency: int = 5,
    ) -> list[str]:
        """批量 LLM 推理（带并发控制）

        Args:
            prompts: 用户 prompt 列表
            system_prompt: 可选系统提示词
            model: 模型名（为空使用默认）
            max_concurrency: 最大并发数

        Returns:
            响应文本列表，顺序与输入一致
        """
        if not self._gateway:
            return ["error: gateway unavailable"] * len(prompts)

        sem = asyncio.Semaphore(max_concurrency)

        async def _call(prompt: str) -> str:
            async with sem:
                for attempt in range(3):
                    try:
                        messages = []
                        if system_prompt:
                            messages.append({"role": "system", "content": system_prompt})
                        messages.append({"role": "user", "content": prompt})

                        text = ""
                        async for chunk in self._gateway.chat_stream(
                            messages=messages, model=model or ""
                        ):
                            if chunk.content:
                                text += chunk.content
                        return text or ""
                    except Exception as e:
                        logger.warning(
                            "llm_batch retry %d/3: %s", attempt + 1, e
                        )
                        if attempt < 2:
                            await asyncio.sleep(1 * (attempt + 1))
                return "error: max retries exceeded"

        tasks = [_call(p) for p in prompts]
        return await asyncio.gather(*tasks)

    # ── 批量知识库索引 ──

    async def knowledge_index_batch(
        self,
        documents: list[dict[str, Any]],
        kb_id: str,
        tenant_id: str,
        concurrency: int = 3,
    ) -> list[dict[str, Any]]:
        """批量知识库文档索引：解析 → 分块 → 嵌入 → 存储

        Args:
            documents: 文档列表，每项包含 {id, content, metadata}
            kb_id: 知识库 ID
            tenant_id: 租户 ID
            concurrency: 并发索引数

        Returns:
            每项处理结果 {doc_id, status, chunks, error}
        """
        from app.rag.builder import build_knowledge
        from app.rag.stores.milvus_store import MilvusStore

        vector_store = None
        try:
            milvus_addr = settings.milvus_address
            if milvus_addr:
                host = milvus_addr.split(":")[0]
                port = int(milvus_addr.split(":")[1]) if ":" in milvus_addr else 19530
                vector_store = MilvusStore(host=host, port=port)
        except Exception as e:
            logger.warning("knowledge_index_batch: Milvus unavailable: %s", e)

        # 从环境获取嵌入模型
        embed_model = settings.llm_model or "text-embedding-3-small"

        sem = asyncio.Semaphore(concurrency)

        async def _index(doc: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                doc_id = doc.get("id", uuid.uuid4().hex)
                try:
                    result = await build_knowledge(
                        doc_id=doc_id,
                        kb_id=kb_id,
                        tenant_id=tenant_id,
                        content=doc.get("content", ""),
                        metadata=doc.get("metadata", {}),
                        embed_model=embed_model,
                        vector_store=vector_store,
                        gateway=self._gateway,
                    )
                    return {
                        "doc_id": doc_id,
                        "status": "success",
                        "chunks": result.get("chunks", 0),
                    }
                except Exception as e:
                    logger.error("knowledge index doc %s failed: %s", doc_id, e)
                    return {"doc_id": doc_id, "status": "failed", "error": str(e)}

        tasks = [_index(doc) for doc in documents]
        return await asyncio.gather(*tasks)
