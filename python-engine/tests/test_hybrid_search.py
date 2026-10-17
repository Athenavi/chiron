# HybridRetriever — 集合名与租户过滤口径（不依赖 Milvus / PG）
from unittest.mock import AsyncMock

from app.rag.hybrid_search import HybridRetriever
from app.rag.stores.base import VectorStoreBase


class FakeMilvus(VectorStoreBase):
    """记录 search 入参，返回固定命中"""

    def __init__(self, hits=None):
        self.calls: list[dict] = []
        self._hits = hits or []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return list(self._hits)

    async def insert(self, *a, **k):  # pragma: no cover - 协议占位
        return 0

    async def delete(self, *a, **k):  # pragma: no cover
        return 0

    async def delete_by_document(self, *a, **k):  # pragma: no cover
        return 0

    async def delete_by_kb(self, *a, **k):  # pragma: no cover
        return 0

    async def ensure_collection(self, *a, **k):  # pragma: no cover
        return None

    async def close(self):  # pragma: no cover
        return None


def _retriever(store):
    return HybridRetriever(vector_store=store, pg_pool=None)


class TestVectorLeg:
    async def test_collection_name_matches_write_side(self, monkeypatch):
        """向量腿必须打到写入侧的 kb_<id 下划线> 集合：裸 uuid 过不了表名校验，
        异常被吞成空结果后混合检索实际只剩全文腿。"""
        store = FakeMilvus()
        fake_llm = AsyncMock(return_value=[0.1, 0.2])
        monkeypatch.setattr("app.llm.client.llm_client.embed", fake_llm)

        hits = [{"id": "v1", "content": "c", "doc_id": "d1", "chunk_index": 0, "score": 0.9}]
        store._hits = hits
        out = await _retriever(store).hybrid_search(
            query="q", kb_id="5f2a-9c1b", tenant_id="t-1", top_k=5
        )

        assert store.calls[0]["collection"] == "kb_5f2a_9c1b"
        assert store.calls[0]["filter_expr"] == 'kb_id == "5f2a-9c1b" AND tenant_id == "t-1"'
        # 向量腿命中要真的进入融合结果（此前恒为空）
        assert [r["id"] for r in out] == ["v1"]
        assert out[0]["source"] == "vector"

    async def test_tenant_value_escaped_in_expr(self, monkeypatch):
        store = FakeMilvus()
        monkeypatch.setattr("app.llm.client.llm_client.embed", AsyncMock(return_value=[0.1]))

        await _retriever(store)._vector_search("q", 'a"b', 't"1', 5)

        expr = store.calls[0]["filter_expr"]
        assert expr == 'kb_id == "a\\"b" AND tenant_id == "t\\"1"'

    async def test_embed_failure_returns_empty(self, monkeypatch):
        store = FakeMilvus()
        monkeypatch.setattr("app.llm.client.llm_client.embed", AsyncMock(return_value=None))

        assert await _retriever(store)._vector_search("q", "kb-1", "t-1", 5) == []
        assert store.calls == []
