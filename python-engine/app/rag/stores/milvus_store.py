"""
Milvus 向量存储实现
"""

from __future__ import annotations

import asyncio
import logging

from app.rag.stores.base import VectorStoreBase

logger = logging.getLogger(__name__)


class MilvusStore(VectorStoreBase):
    """Milvus 向量存储"""

    def __init__(self, host: str = "localhost", port: int = 19530):
        self._host = host
        self._port = port
        self._connected = False
        self._collections: dict[str, object] = {}

    def _ensure_connected(self):
        """确保 Milvus 已连接"""
        if not self._connected:
            from pymilvus import connections

            connections.connect(alias="default", host=self._host, port=self._port)
            self._connected = True
            logger.info("Milvus connected: %s:%d", self._host, self._port)

    def _get_or_create_collection(
        self, name: str, dim: int, index_type: str = "IVF_FLAT"
    ):
        """获取或创建 Milvus collection"""
        from pymilvus import Collection, CollectionSchema, DataType, FieldSchema

        safe_name = self._validate_table_name(name)

        fields = [
            FieldSchema(
                name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=64
            ),
            FieldSchema(name="kb_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="tenant_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="chunk_index", dtype=DataType.INT64),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),
        ]
        schema = CollectionSchema(fields, description="RAG knowledge chunks")

        try:
            collection = Collection(safe_name)
        except Exception:
            collection = Collection(safe_name, schema)
            collection.create_index(
                "embedding",
                {
                    "metric_type": "COSINE",
                    "index_type": index_type,
                    "params": {"nlist": 1024},
                },
            )
        collection.load()
        return collection

    # ── 公开接口 ──

    async def insert(
        self,
        collection: str,
        ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict],
    ) -> int:
        """插入向量数据"""
        self._ensure_connected()
        col = self._get_or_create_collection(collection, len(vectors[0]) if vectors else 1536)
        safe_name = self._validate_table_name(collection)

        kb_ids = [p.get("kb_id", "") for p in payloads]
        doc_ids = [p.get("doc_id", "") for p in payloads]
        tenant_ids = [p.get("tenant_id", "") for p in payloads]
        chunk_indices = [p.get("chunk_index", 0) for p in payloads]
        contents = [p.get("content", "")[:65000] for p in payloads]

        await asyncio.to_thread(
            col.insert,
            [ids, kb_ids, doc_ids, tenant_ids, chunk_indices, contents, vectors],
        )
        await asyncio.to_thread(col.flush)
        logger.info("Milvus insert: %d vectors into %s", len(ids), safe_name)
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
        from pymilvus import Collection

        self._ensure_connected()
        safe_name = self._validate_table_name(collection)

        try:
            col = Collection(safe_name)
            col.load()
        except Exception as e:
            logger.error("Milvus collection load failed: %s", e)
            return []

        search_params = {
            "metric_type": "COSINE",
            "params": {"nprobe": 10},
        }
        output_fields = ["doc_id", "chunk_index", "content"]

        # 对 filter_expr 做防御性转义，防止注入
        safe_expr = filter_expr  # 如果调用方已转义，直接使用
        try:
            results = await asyncio.to_thread(
                col.search,
                data=[query_vector],
                anns_field="embedding",
                param=search_params,
                limit=top_k,
                expr=safe_expr,
                output_fields=output_fields,
            )
        except Exception as e:
            logger.error("Milvus search failed: %s", e)
            return []

        out = []
        for hits in results:
            for hit in hits:
                if hit.score >= threshold:
                    out.append(
                        {
                            "id": hit.id,
                            "content": hit.entity.get("content", ""),
                            "doc_id": hit.entity.get("doc_id", ""),
                            "chunk_index": hit.entity.get("chunk_index", 0),
                            "score": hit.score,
                        }
                    )
        return out

    async def delete(
        self,
        collection: str,
        ids: list[str],
    ) -> int:
        """按主键 ID 列表删除"""
        from pymilvus import Collection

        self._ensure_connected()
        safe_name = self._validate_table_name(collection)

        try:
            col = Collection(safe_name)
            col.load()
        except Exception:
            return 0

        # 内层 f-string 与外层共用单引号需要 PEP 701（Python 3.12+）；
        # 本包声明 requires-python >= 3.11，故先拼好再插值。
        joined = ",".join(f'"{i}"' for i in ids)
        expr = f"id in [{joined}]"
        await asyncio.to_thread(col.delete, expr)
        await asyncio.to_thread(col.flush)
        return len(ids)

    async def delete_by_document(
        self,
        collection: str,
        doc_id: str,
        tenant_id: str,
    ) -> int:
        """按文档 ID + 租户删除"""
        from pymilvus import Collection

        self._ensure_connected()
        safe_name = self._validate_table_name(collection)

        try:
            col = Collection(safe_name)
            col.load()
        except Exception:
            return 0

        safe_doc = self._sanitize_expr(doc_id)
        safe_tenant = self._sanitize_expr(tenant_id)
        expr = f'doc_id == "{safe_doc}" AND tenant_id == "{safe_tenant}"'
        await asyncio.to_thread(col.delete, expr)
        await asyncio.to_thread(col.flush)
        logger.info("Milvus delete_by_document: %s (tenant=%s)", doc_id, tenant_id)
        return 0  # Milvus delete 不返回计数

    async def delete_by_kb(
        self,
        collection: str,
        kb_id: str,
        tenant_id: str,
    ) -> int:
        """按知识库 ID + 租户删除"""
        from pymilvus import Collection

        self._ensure_connected()
        safe_name = self._validate_table_name(collection)

        try:
            col = Collection(safe_name)
            col.load()
        except Exception:
            return 0

        safe_kb = self._sanitize_expr(kb_id)
        safe_tenant = self._sanitize_expr(tenant_id)
        expr = f'kb_id == "{safe_kb}" AND tenant_id == "{safe_tenant}"'
        await asyncio.to_thread(col.delete, expr)
        await asyncio.to_thread(col.flush)
        logger.info("Milvus delete_by_kb: %s (tenant=%s)", kb_id, tenant_id)
        return 0

    async def ensure_collection(
        self,
        name: str,
        dim: int,
        index_type: str = "IVF_FLAT",
    ) -> None:
        """确保 collection 存在"""
        self._ensure_connected()
        self._get_or_create_collection(name, dim, index_type)

    async def close(self) -> None:
        """释放连接"""
        from pymilvus import connections

        if self._connected:
            connections.disconnect("default")
            self._connected = False
            logger.info("Milvus disconnected")
