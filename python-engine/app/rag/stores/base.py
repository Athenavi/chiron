"""
向量存储抽象基类 — 统一 Milvus / pgvector 接口
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class VectorStoreBase(ABC):
    """向量存储抽象基类"""

    @abstractmethod
    async def insert(
        self,
        collection: str,
        ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
    ) -> int:
        """插入向量数据，返回插入数量"""
        ...

    @abstractmethod
    async def search(
        self,
        collection: str,
        query_vector: list[float],
        top_k: int = 5,
        threshold: float = 0.5,
        filter_expr: str | None = None,
    ) -> list[dict[str, Any]]:
        """搜索相似向量，返回 [{id, content, doc_id, chunk_index, score, ...}]"""
        ...

    @abstractmethod
    async def delete(
        self,
        collection: str,
        ids: list[str],
    ) -> int:
        """按主键 ID 列表删除向量，返回删除数量"""
        ...

    @abstractmethod
    async def delete_by_document(
        self,
        collection: str,
        doc_id: str,
        tenant_id: str,
    ) -> int:
        """按文档 ID + 租户删除该文档的所有向量分块"""
        ...

    @abstractmethod
    async def delete_by_kb(
        self,
        collection: str,
        kb_id: str,
        tenant_id: str,
    ) -> int:
        """按知识库 ID + 租户删除该 KB 的所有向量"""
        ...

    @abstractmethod
    async def ensure_collection(
        self,
        name: str,
        dim: int,
        index_type: str = "IVF_FLAT",
    ) -> None:
        """确保集合/表存在，不存在则创建"""
        ...

    @abstractmethod
    async def close(self) -> None:
        """释放连接资源"""
        ...

    @staticmethod
    def _sanitize_expr(value: str) -> str:
        """转义用于表达式拼接的字符串值，防止注入"""
        return value.replace('"', '\\"').replace("'", "\\'")

    @staticmethod
    def _validate_table_name(name: str) -> str:
        """校验表名/集合名为合法标识符"""
        import re

        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"非法表名/集合名: {name!r}")
        return name
