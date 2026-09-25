"""
VectorStore Protocol — 对标 Go 的 storage.FileStore 模式
"""

from __future__ import annotations

from typing import Any, Protocol


class VectorStore(Protocol):
    """向量存储接口"""

    async def insert(
        self,
        collection: str,
        ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
    ) -> int:
        """插入向量数据，返回插入数量"""
        ...

    async def search(
        self,
        collection: str,
        query_vector: list[float],
        top_k: int = 5,
        threshold: float = 0.5,
        filter_expr: str | None = None,
    ) -> list[dict[str, Any]]:
        """搜索相似向量，返回结果列表"""
        ...

    async def delete(
        self,
        collection: str,
        ids: list[str],
    ) -> int:
        """删除向量数据，返回删除数量"""
        ...

    async def ensure_collection(
        self,
        name: str,
        dim: int,
        index_type: str = "IVF_FLAT",
    ) -> None:
        """确保集合存在，不存在则创建"""
        ...

    async def close(self) -> None:
        """关闭连接"""
        ...
