"""
Milvus Vector Store — 连接池化实现，对接 VectorStore Protocol
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class MilvusVectorStore:
    """Milvus 向量存储 — 实现 VectorStore Protocol"""

    def __init__(
        self,
        address: str = "",
        collection_prefix: str = "",
    ):
        self._address = address
        self._collection_prefix = collection_prefix
        self._connected = False

    def _ensure_connected(self) -> None:
        """确保连接已建立"""
        if not self._connected:
            from pymilvus import connections

            connections.connect(
                "default",
                host=self._address.split(":")[0],
                port=self._address.split(":")[1] if ":" in self._address else "19530",
            )
            self._connected = True
            logger.info("Milvus connected: %s", self._address)

    def _get_collection_name(self, name: str) -> str:
        """获取带前缀的集合名"""
        if self._collection_prefix:
            return f"{self._collection_prefix}_{name}"
        return name

    async def insert(
        self,
        collection: str,
        ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict],
    ) -> int:
        """插入向量数据"""
        self._ensure_connected()
        from pymilvus import Collection

        col_name = self._get_collection_name(collection)
        col = Collection(col_name)

        # 准备数据
        data = [
            ids,
            vectors,
            [p.get("content", "") for p in payloads],
            [p.get("tenant_id", "") for p in payloads],
        ]

        col.insert(data)
        col.flush()

        logger.info("Inserted %d vectors into %s", len(ids), col_name)
        return len(ids)

    async def search(
        self,
        collection: str,
        query_vector: list[float],
        top_k: int = 5,
        threshold: float = 0.5,
        filter_expr: str | None = None,
    ) -> list[dict]:
        """搜索相似向量"""
        self._ensure_connected()
        from pymilvus import Collection

        col_name = self._get_collection_name(collection)
        col = Collection(col_name)
        col.load()

        results = col.search(
            data=[query_vector],
            anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"nprobe": 10}},
            limit=top_k,
            expr=filter_expr,
            output_fields=["content", "tenant_id"],
        )

        return [
            {
                "id": hit.id,
                "content": hit.entity.get("content", ""),
                "score": hit.score,
                "tenant_id": hit.entity.get("tenant_id", ""),
            }
            for hits in results
            for hit in hits
            if hit.score >= threshold
        ]

    async def delete(
        self,
        collection: str,
        ids: list[str],
    ) -> int:
        """删除向量数据"""
        self._ensure_connected()
        from pymilvus import Collection

        col_name = self._get_collection_name(collection)
        col = Collection(col_name)

        expr = f"id in {ids}"
        col.delete(expr)

        logger.info("Deleted %d vectors from %s", len(ids), col_name)
        return len(ids)

    async def ensure_collection(
        self,
        name: str,
        dim: int,
        index_type: str = "IVF_FLAT",
    ) -> None:
        """确保集合存在"""
        self._ensure_connected()
        from pymilvus import Collection, CollectionSchema, DataType, FieldSchema

        col_name = self._get_collection_name(name)

        # 检查集合是否已存在
        from pymilvus import utility

        if utility.has_collection(col_name):
            logger.info("Collection %s already exists", col_name)
            return

        # 创建集合
        fields = [
            FieldSchema(
                name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=64
            ),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="tenant_id", dtype=DataType.VARCHAR, max_length=64),
        ]
        schema = CollectionSchema(fields)
        col = Collection(col_name, schema)

        # 创建索引
        index_params = {
            "metric_type": "COSINE",
            "index_type": index_type,
            "params": {"nlist": 1024},
        }
        col.create_index("embedding", index_params)

        logger.info(
            "Created collection %s (dim=%d, index=%s)", col_name, dim, index_type
        )

    async def close(self) -> None:
        """关闭连接"""
        if self._connected:
            from pymilvus import connections

            connections.disconnect("default")
            self._connected = False
            logger.info("Milvus connection closed")
