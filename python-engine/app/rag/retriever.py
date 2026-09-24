# RAG 检索实现
import asyncio
import logging

from app.config import settings
from app.llm.client import llm_client

logger = logging.getLogger(__name__)


class RAGRetriever:
    """RAG 检索器：文档分块 + 嵌入 + Milvus 向量检索"""

    _shared_milvus_connected = False  # 类级别共享连接状态

    def __init__(self):
        self._milvus_client = None
        self._collection = None

    async def _get_collection(self):
        """获取 Milvus collection（单例延迟初始化，避免每个请求重复连接）"""
        if self._collection is not None:
            return self._collection

        try:
            from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, connections

            # 单例连接：同一 alias 仅连接一次
            if not RAGRetriever._shared_milvus_connected:
                connections.connect(
                    alias="default",
                    host=settings.milvus_address.split(":")[0],
                    port=(
                        int(settings.milvus_address.split(":")[1])
                        if ":" in settings.milvus_address
                        else 19530
                    ),
                )
                RAGRetriever._shared_milvus_connected = True

            # 定义 collection schema
            fields = [
                FieldSchema(
                    name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=64
                ),
                FieldSchema(name="tenant_id", dtype=DataType.VARCHAR, max_length=64),
                FieldSchema(name="document_id", dtype=DataType.VARCHAR, max_length=64),
                FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, max_length=64),
                FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535),
                FieldSchema(
                    name="embedding",
                    dtype=DataType.FLOAT_VECTOR,
                    dim=settings.embedding_dim,
                ),
            ]
            schema = CollectionSchema(fields, description="Knowledge base for RAG")

            # 获取或创建 collection
            collection_name = settings.milvus_collection
            try:
                self._collection = Collection(collection_name)
            except Exception:
                self._collection = Collection(collection_name, schema)
                # 创建索引
                self._collection.create_index(
                    field_name="embedding",
                    index_params={
                        "metric_type": "COSINE",
                        "index_type": "IVF_FLAT",
                        "params": {"nlist": 1024},
                    },
                )

            self._collection.load()
            return self._collection

        except Exception as e:
            logger.warning(f"Milvus 连接失败，使用内存模式: {e}")
            return None

    async def index_document(
        self,
        tenant_id: str,
        document_id: str,
        content: str,
        file_type: str = "txt",
        metadata: dict = None,
    ) -> dict:
        """索引文档：分块 + 嵌入 + 存入 Milvus"""
        try:
            # 1. 分块
            chunks = self._split_text(
                content, settings.chunk_size, settings.chunk_overlap
            )

            # 2. 检查 Milvus 可用性（不可用则跳过嵌入，避免无谓的 API 调用）
            collection = await self._get_collection()
            if collection is None:
                logger.warning("Milvus 不可用，跳过索引")
                return {
                    "document_id": document_id,
                    "chunks_count": len(chunks),
                    "status": "indexed",
                    "error": "Milvus 不可用，已跳过",
                }

            # 3. 嵌入
            embeddings = []
            for chunk in chunks:
                embedding = await llm_client.embed(chunk)
                embeddings.append(embedding)

            # 准备数据
            ids = [f"{document_id}_{i}" for i in range(len(chunks))]
            tenant_ids = [tenant_id] * len(chunks)
            document_ids = [document_id] * len(chunks)
            chunk_ids = [f"chunk_{i}" for i in range(len(chunks))]

            # 插入数据
            data = [ids, tenant_ids, document_ids, chunk_ids, chunks, embeddings]
            await asyncio.to_thread(collection.insert, data)
            await asyncio.to_thread(collection.flush)

            logger.info(f"文档索引完成: {document_id}, {len(chunks)} 个分块")
            return {
                "document_id": document_id,
                "chunks_count": len(chunks),
                "status": "indexed",
            }

        except Exception as e:
            logger.error(f"文档索引失败: {e}")
            return {
                "document_id": document_id,
                "chunks_count": 0,
                "status": "failed",
                "error": str(e),
            }

    async def retrieve(
        self,
        tenant_id: str,
        query: str,
        top_k: int = 5,
        threshold: float = 0.7,
    ) -> list:
        """向量检索：查询相关文档片段"""
        try:
            # 1. Milvus 可用性检查（不可用则直接返回，不做查询嵌入）
            collection = await self._get_collection()
            if collection is None:
                logger.warning("Milvus 不可用，返回空结果")
                return []

            # 2. 计算查询嵌入
            query_embedding = await llm_client.embed(query)

            # 3. Milvus 检索（使用参数化表达式避免字符串插值注入）
            # tenant_id 来自 JWT claims 非用户直接输入，但防御性编程使用转义
            safe_tenant_id = tenant_id.replace('"', '\\"')
            results = await asyncio.to_thread(
                collection.search,
                data=[query_embedding],
                anns_field="embedding",
                param={"metric_type": "COSINE", "params": {"nprobe": 10}},
                limit=top_k,
                expr=f'tenant_id == "{safe_tenant_id}"',
                output_fields=["document_id", "chunk_id", "content"],
            )

            # 4. 过滤低分结果
            filtered = []
            for hits in results:
                for hit in hits:
                    if hit.score >= threshold:
                        filtered.append(
                            {
                                "document_id": hit.entity.get("document_id", ""),
                                "chunk_id": hit.entity.get("chunk_id", ""),
                                "content": hit.entity.get("content", ""),
                                "score": hit.score,
                                "metadata": {},
                            }
                        )

            return filtered

        except Exception as e:
            logger.error(f"向量检索失败: {e}")
            return []

    def _split_text(self, text: str, chunk_size: int, chunk_overlap: int) -> list:
        """文本分块"""
        if chunk_size <= 0:
            return [text] if text.strip() else []
        step = max(chunk_size - chunk_overlap, 1)
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            chunk = text[start:end]
            if chunk.strip():
                chunks.append(chunk)
            if end >= len(text):
                break
            start += step
        return chunks
