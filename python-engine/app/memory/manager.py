# 记忆管理 — 使用接口注入
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import redis.asyncio as aioredis

from app.config import settings
from app.gateway.router import GatewayRouter
from app.interfaces.cache import CacheClient
from app.interfaces.vectorstore import VectorStore
from app.redis_keys import rkey

logger = logging.getLogger(__name__)


class MemoryManager:
    """记忆管理器：短期记忆(Redis) + 长期记忆(Milvus)"""

    def __init__(
        self,
        llm_gateway: GatewayRouter,
        redis: aioredis.Redis,
        vector_store: VectorStore | None = None,
        cache_client: CacheClient | None = None,
    ):
        self._gateway = llm_gateway
        self._redis = redis
        self._vector_store = vector_store
        self._cache_client = cache_client
        self._milvus_connected = False
        self._milvus_collection: Any = None

    def _ensure_milvus(self) -> None:
        if not self._milvus_connected:
            from pymilvus import connections

            host = settings.milvus_address.split(":")[0]
            port = (
                int(settings.milvus_address.split(":")[1])
                if ":" in settings.milvus_address
                else 19530
            )
            connections.connect(alias="memory", host=host, port=port)
            self._milvus_connected = True

    def _get_milvus_collection(self) -> Any:
        if self._milvus_collection is not None:
            return self._milvus_collection
        self._ensure_milvus()
        from pymilvus import Collection, CollectionSchema, DataType, FieldSchema

        fields = [
            FieldSchema(
                name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=64
            ),
            FieldSchema(name="tenant_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="user_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="session_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="memory_type", dtype=DataType.VARCHAR, max_length=32),
            FieldSchema(
                name="embedding",
                dtype=DataType.FLOAT_VECTOR,
                dim=settings.embedding_dim,
            ),
            FieldSchema(name="created_at", dtype=DataType.VARCHAR, max_length=32),
            FieldSchema(name="metadata_json", dtype=DataType.VARCHAR, max_length=65535),
        ]
        schema = CollectionSchema(fields, description="Long-term memory")
        try:
            self._milvus_collection = Collection("memory_store", using="memory")
        except Exception:
            self._milvus_collection = Collection("memory_store", schema, using="memory")
            self._milvus_collection.create_index(
                "embedding",
                {
                    "metric_type": "COSINE",
                    "index_type": "IVF_FLAT",
                    "params": {"nlist": 256},
                },
            )
        self._milvus_collection.load(using="memory")
        return self._milvus_collection

    async def save_memory(
        self,
        tenant_id: str,
        user_id: str,
        session_id: str,
        content: str,
        memory_type: str = "short_term",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """保存记忆"""
        memory_id = str(uuid.uuid4())
        created_at = str(int(time.time()))
        try:
            if memory_type == "short_term":
                return await self._save_short_term(
                    memory_id,
                    tenant_id,
                    user_id,
                    session_id,
                    content,
                    created_at,
                    metadata,
                )
            else:
                return await self._save_long_term(
                    memory_id,
                    tenant_id,
                    user_id,
                    session_id,
                    content,
                    created_at,
                    metadata,
                )
        except Exception as e:
            logger.error("保存记忆失败: %s", e)
            return {"memory_id": memory_id, "status": "failed", "error": str(e)}

    async def _save_short_term(
        self,
        memory_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        content: str,
        created_at: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """保存短期记忆"""
        if self._cache_client:
            # 使用 CacheClient 接口
            key = rkey(f"memory:{tenant_id}:{user_id}:{session_id}")
            data = {
                "id": memory_id,
                "content": content,
                "created_at": created_at,
                "metadata": metadata or {},
            }
            await self._cache_client.rpush(key, json.dumps(data))
            await self._cache_client.expire(key, settings.short_term_ttl)
        else:
            # 直接使用 Redis
            key = rkey(f"memory:{tenant_id}:{user_id}:{session_id}")
            data = {
                "id": memory_id,
                "content": content,
                "created_at": created_at,
                "metadata": metadata or {},
            }
            await self._redis.rpush(key, json.dumps(data))
            await self._redis.expire(key, settings.short_term_ttl)
        return {"memory_id": memory_id, "status": "saved"}

    async def _save_long_term(
        self,
        memory_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        content: str,
        created_at: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """保存长期记忆"""
        # 计算嵌入向量
        resp = await self._gateway.embed(content, settings.embedding_model)
        if not resp.embedding:
            return {
                "memory_id": memory_id,
                "status": "failed",
                "error": "Embedding failed",
            }

        if self._vector_store:
            # 使用 VectorStore 接口
            collection = "memory_store"
            await self._vector_store.ensure_collection(collection, len(resp.embedding))
            await self._vector_store.insert(
                collection=collection,
                ids=[memory_id],
                vectors=[resp.embedding],
                payloads=[
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "session_id": session_id,
                        "content": content,
                        "memory_type": "long_term",
                        "created_at": created_at,
                        "metadata_json": json.dumps(metadata or {}),
                    }
                ],
            )
        else:
            # 直接使用 Milvus
            collection = self._get_milvus_collection()
            collection.insert(
                [
                    [memory_id],
                    [tenant_id],
                    [user_id],
                    [session_id],
                    [content],
                    ["long_term"],
                    [resp.embedding],
                    [created_at],
                    [json.dumps(metadata or {})],
                ]
            )
            collection.flush(using="memory")
        return {"memory_id": memory_id, "status": "saved"}

    async def query_memory(
        self,
        tenant_id: str,
        user_id: str,
        query: str,
        top_k: int = 5,
        memory_type: str = "all",
    ) -> list[Any]:
        """查询记忆"""
        results = []
        try:
            if memory_type in ("short_term", "all"):
                results.extend(await self._query_short_term(tenant_id, user_id, top_k))
            if memory_type in ("long_term", "all"):
                results.extend(
                    await self._query_long_term(tenant_id, user_id, query, top_k)
                )
            results.sort(key=lambda x: x.get("relevance", 0), reverse=True)
            return results[:top_k]
        except Exception as e:
            logger.error("查询记忆失败: %s", e)
            return []

    async def _query_short_term(
        self, tenant_id: str, user_id: str, top_k: int
    ) -> list[Any]:
        """查询短期记忆"""
        if self._cache_client:
            # 使用 CacheClient 接口
            pattern = rkey(f"memory:{tenant_id}:{user_id}:*")
            keys = []
            async for key in self._cache_client.scan_iter(match=pattern):
                keys.append(key)
            results = []
            for key in keys[:5]:
                memories: list[Any] = await self._cache_client.lrange(key, -top_k, -1)
                for mem_str in memories:
                    try:
                        mem = json.loads(mem_str)
                        results.append(
                            {
                                "memory_id": mem["id"],
                                "content": mem["content"],
                                "relevance": 0.8,
                                "created_at": mem.get("created_at", ""),
                                "memory_type": "short_term",
                                "metadata": mem.get("metadata", {}),
                            }
                        )
                    except (json.JSONDecodeError, KeyError):
                        continue
            return results
        else:
            # 直接使用 Redis
            pattern = rkey(f"memory:{tenant_id}:{user_id}:*")
            keys = []
            async for key in self._redis.scan_iter(match=pattern):
                keys.append(key if isinstance(key, str) else key.decode())
            results = []
            for key in keys[:5]:
                memories = await self._redis.lrange(key, -top_k, -1)
                for mem_str in memories:
                    try:
                        mem = json.loads(
                            mem_str if isinstance(mem_str, str) else mem_str.decode()
                        )
                        results.append(
                            {
                                "memory_id": mem["id"],
                                "content": mem["content"],
                                "relevance": 0.8,
                                "created_at": mem.get("created_at", ""),
                                "memory_type": "short_term",
                                "metadata": mem.get("metadata", {}),
                            }
                        )
                    except (json.JSONDecodeError, KeyError):
                        continue
            return results

    async def _query_long_term(
        self, tenant_id: str, user_id: str, query: str, top_k: int
    ) -> list[Any]:
        """查询长期记忆"""
        # 计算查询向量
        resp = await self._gateway.embed(query, settings.embedding_model)
        if not resp.embedding:
            return []

        # 过滤必须同时含 tenant 与 user：写入侧明明存了 user_id，只按 tenant 过滤
        # 会让同租户的其他用户检索到本用户的长期记忆原文并注入自己的上下文 ——
        # 字段有、查询没用，是漏写而不是设计取舍。
        if not user_id:
            # 宁可查不到，也不要退化成"整个租户可见"
            logger.warning(
                "long-term memory query without user_id — refusing unscoped search (tenant=%s)",
                tenant_id,
            )
            return []
        # 转义引号，避免 tenant/user 中的引号破坏 Milvus 表达式
        safe_tenant = str(tenant_id).replace('"', '\\"')
        safe_user = str(user_id).replace('"', '\\"')
        scope_expr = f'tenant_id == "{safe_tenant}" and user_id == "{safe_user}"'

        if self._vector_store:
            # 使用 VectorStore 接口
            collection = "memory_store"
            results = await self._vector_store.search(
                collection=collection,
                query_vector=resp.embedding,
                top_k=top_k,
                threshold=0.5,
                filter_expr=scope_expr,
            )
            memories = []
            for r in results:
                memories.append(
                    {
                        "memory_id": r["id"],
                        "content": r.get("content", ""),
                        "relevance": r.get("score", 0),
                        "created_at": r.get("created_at", ""),
                        "memory_type": "long_term",
                        "metadata": json.loads(r.get("metadata_json", "{}")),
                    }
                )
            return memories
        else:
            # 直接使用 Milvus
            try:
                collection = self._get_milvus_collection()
                results = collection.search(
                    data=[resp.embedding],
                    anns_field="embedding",
                    param={"metric_type": "COSINE", "params": {"nprobe": 10}},
                    limit=top_k,
                    expr=scope_expr,
                    output_fields=["content", "created_at", "metadata_json"],
                )
                memories = []
                for hits in results:
                    for hit in hits:
                        if hit.score >= 0.5:
                            memories.append(
                                {
                                    "memory_id": hit.id,
                                    "content": hit.entity.get("content", ""),
                                    "relevance": hit.score,
                                    "created_at": hit.entity.get("created_at", ""),
                                    "memory_type": "long_term",
                                    "metadata": json.loads(
                                        hit.entity.get("metadata_json", "{}")
                                    ),
                                }
                            )
                return memories
            except Exception as e:
                logger.error("长期记忆查询失败: %s", e)
                return []
