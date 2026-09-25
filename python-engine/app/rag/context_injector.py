"""
会话级 RAG 上下文注入器

功能：
1. 从 llm_config 中提取 kb_id 列表（支持单知识库/多知识库）
2. 使用 HybridRetriever 执行混合检索
3. 格式化为 LLM 友好的知识上下文区块
4. 支持配置项：top_k, threshold, 结果截断长度
"""

from __future__ import annotations

import logging
from typing import Any

from app.rag.hybrid_search import HybridRetriever

logger = logging.getLogger(__name__)

# 默认注入参数
_DEFAULT_TOP_K = 5
_DEFAULT_THRESHOLD = 0.5
_DEFAULT_MAX_SNIPPET_LEN = 800
_DEFAULT_MAX_TOTAL_LEN = 6000


class RAGContextInjector:
    """会话级 RAG 上下文注入器

    从 task/llm_config 中读取知识库配置，执行检索并格式化为 prompt 区块。
    """

    def __init__(self, hybrid_retriever: HybridRetriever | None = None):
        self._hybrid_retriever = hybrid_retriever

    @property
    def hybrid_retriever(self) -> HybridRetriever | None:
        return self._hybrid_retriever

    @hybrid_retriever.setter
    def hybrid_retriever(self, retriever: HybridRetriever | None) -> None:
        self._hybrid_retriever = retriever

    def extract_kb_config(self, llm_config: dict[str, Any] | None) -> list[dict[str, Any]]:
        """从 llm_config 中提取知识库配置

        llm_config 支持两种格式：
        1. 简单模式: {"kb_id": "kb-uuid-1234"}
        2. 高级模式: {"kb_ids": [{"id": "kb-uuid-1234", "top_k": 3, "threshold": 0.7}, ...]}

        Returns:
            [{"kb_id": str, "top_k": int, "threshold": float}, ...]
        """
        if not llm_config:
            return []

        # 高级模式：多知识库
        kb_ids_config = llm_config.get("kb_ids")
        if isinstance(kb_ids_config, list) and kb_ids_config:
            out = []
            for item in kb_ids_config:
                if isinstance(item, str):
                    out.append({
                        "kb_id": item,
                        "top_k": _DEFAULT_TOP_K,
                        "threshold": _DEFAULT_THRESHOLD,
                    })
                elif isinstance(item, dict) and item.get("id"):
                    out.append({
                        "kb_id": item["id"],
                        "top_k": item.get("top_k", _DEFAULT_TOP_K),
                        "threshold": item.get("threshold", _DEFAULT_THRESHOLD),
                    })
            return out

        # 简单模式：单个知识库
        kb_id = llm_config.get("kb_id")
        if kb_id:
            return [{
                "kb_id": kb_id,
                "top_k": llm_config.get("kb_top_k", _DEFAULT_TOP_K),
                "threshold": llm_config.get("kb_threshold", _DEFAULT_THRESHOLD),
            }]

        return []

    async def retrieve_context(
        self,
        query: str,
        tenant_id: str,
        kb_configs: list[dict[str, Any]],
        use_hybrid: bool = True,
    ) -> list[dict[str, Any]]:
        """执行检索并返回格式化结果

        Args:
            query: 用户查询
            tenant_id: 租户 ID
            kb_configs: extract_kb_config 的输出
            use_hybrid: 是否使用混合检索（向量 + 全文）

        Returns:
            [{kb_id, content, doc_id, chunk_index, score, source}]
        """
        if not self._hybrid_retriever or not kb_configs:
            return []

        all_results: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for config in kb_configs:
            kb_id = config["kb_id"]
            top_k = config.get("top_k", _DEFAULT_TOP_K)

            if use_hybrid:
                results = await self._hybrid_retriever.hybrid_search(
                    query=query,
                    kb_id=kb_id,
                    tenant_id=tenant_id,
                    top_k=top_k,
                    threshold=config.get("threshold", _DEFAULT_THRESHOLD),
                )
            else:
                # 纯向量检索
                if not self._hybrid_retriever.vector_store:
                    continue
                from app.llm.client import llm_client

                query_embedding = await llm_client.embed(query)
                if not query_embedding:
                    continue

                filter_expr = (
                    f'kb_id == "{kb_id}" AND tenant_id == "{tenant_id}"'
                )
                results = await self._hybrid_retriever.vector_store.search(
                    collection=kb_id,
                    query_vector=query_embedding,
                    top_k=top_k,
                    threshold=config.get("threshold", _DEFAULT_THRESHOLD),
                    filter_expr=filter_expr,
                )

            # 去重
            for r in results:
                rid = r.get("id", "")
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    r["kb_id"] = kb_id
                    all_results.append(r)

        # 按 score 排序
        all_results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return all_results

    def format_prompt_block(
        self,
        results: list[dict[str, Any]],
        max_snippet_len: int = _DEFAULT_MAX_SNIPPET_LEN,
        max_total_len: int = _DEFAULT_MAX_TOTAL_LEN,
    ) -> str:
        """将检索结果格式化为 LLM 提示词中的知识区块

        Args:
            results: retrieve_context 的输出
            max_snippet_len: 每条片段最大字符数
            max_total_len: 总区块最大字符数

        Returns:
            格式化后的字符串，空结果返回空字符串
        """
        if not results:
            return ""

        lines: list[str] = []
        total_len = 0

        for i, r in enumerate(results):
            kb_id = r.get("kb_id", "?")
            source = r.get("source", "vector")
            score = r.get("score", 0)
            doc_id = r.get("doc_id", "?")
            content = r.get("content", "")

            if len(content) > max_snippet_len:
                content = content[:max_snippet_len] + "…"

            entry = f"[{i+1}] (kb={kb_id}, score={score:.3f}, source={source}, doc={doc_id})\n{content}"

            if total_len + len(entry) > max_total_len:
                remaining = max_total_len - total_len
                if remaining > 100:
                    lines.append(entry[:remaining] + "…")
                break

            lines.append(entry)
            total_len += len(entry) + 1  # +1 for newline

        if not lines:
            return ""

        return (
            "## 知识库参考\n\n"
            "以下内容来自您的知识库，可能对回答有帮助：\n\n"
            + "\n\n".join(lines)
        )

    async def inject(
        self,
        system_prompt: str,
        query: str,
        tenant_id: str,
        llm_config: dict[str, Any] | None = None,
        use_hybrid: bool = True,
    ) -> str:
        """一键注入：提取配置 → 检索 → 格式化 → 追加到 system_prompt

        Args:
            system_prompt: 原始系统提示词
            query: 用户查询
            tenant_id: 租户 ID
            llm_config: 任务配置（含 kb_id/kb_ids）
            use_hybrid: 是否使用混合检索

        Returns:
            注入后的 system_prompt（若无知识库配置则原样返回）
        """
        kb_configs = self.extract_kb_config(llm_config)
        if not kb_configs:
            return system_prompt

        results = await self.retrieve_context(query, tenant_id, kb_configs, use_hybrid)
        if not results:
            return system_prompt

        block = self.format_prompt_block(results)
        if not block:
            return system_prompt

        return f"{system_prompt}\n\n{block}"
