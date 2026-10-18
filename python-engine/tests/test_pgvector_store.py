"""向量过滤表达式测试 —— PgvectorStore 的 Milvus 口径翻译 + 参数化下推

不连真实 PG：用一个假连接池记录 fetch 的 SQL 与参数。
"""

from typing import Any

import pytest

from app.rag.context_injector import RAGContextInjector
from app.rag.stores.base import VectorStoreBase
from app.rag.stores.pgvector_store import PgvectorStore, _translate_filter


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]] | None = None, error: Exception | None = None):
        self.fetch_calls: list[tuple[str, tuple]] = []
        self._rows = rows if rows is not None else []
        self._error = error

    async def fetch(self, query: str, *params: Any):
        self.fetch_calls.append((query, params))
        if self._error:
            raise self._error
        return self._rows


class _FakeAcquire:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self.conn = conn
        self.acquired = 0

    def acquire(self) -> _FakeAcquire:
        self.acquired += 1
        return _FakeAcquire(self.conn)


def _store(rows=None, error=None) -> tuple[PgvectorStore, _FakePool, _FakeConn]:
    conn = _FakeConn(rows=rows, error=error)
    pool = _FakePool(conn)
    return PgvectorStore(pool, embedding_dim=2), pool, conn


class TestTranslateFilter:
    """表达式子集 → (列名, 值)"""

    def test_tenant_and_doc_use_pg_column_names(self):
        # Milvus 侧叫 doc_id，本表列名是 document_id；不翻译就是查不存在的列
        out = _translate_filter('tenant_id == "t-1" AND doc_id == "d-2"')
        assert out == [("tenant_id", "t-1"), ("document_id", "d-2")]

    def test_kb_id_is_not_pushed_down(self):
        # KB 约束由 search(collection=...) 承担；再拼一次会和它冲突（写入侧裸 kb_id、
        # HybridRetriever 传 kb_<id>），历史上就是恒空结果
        assert _translate_filter('kb_id == "kb-1" AND tenant_id == "t-1"') == [("tenant_id", "t-1")]

    def test_lowercase_and_and_single_quotes(self):
        # memory/manager 产出的是小写 and
        assert _translate_filter("tenant_id == 't-1' and kb_id == 'k'") == [("tenant_id", "t-1")]

    def test_milvus_escape_is_reversed_into_the_value(self):
        assert _translate_filter(r'tenant_id == "t\"1"') == [("tenant_id", 't"1')]

    def test_and_inside_a_literal_does_not_split(self):
        assert _translate_filter('tenant_id == "a and b"') == [("tenant_id", "a and b")]


class TestSearchPushdown:
    async def test_filter_becomes_parameters_not_sql_text(self):
        store, _pool, conn = _store()
        await store.search("kb-1", [0.1, 0.2], top_k=5, threshold=0.5,
                           filter_expr='kb_id == "kb-1" AND tenant_id == "t-1"')

        sql, params = conn.fetch_calls[0]
        assert "knowledge_base_id = $1" in sql
        assert "tenant_id = $2" in sql
        assert "==" not in sql  # Milvus 语法绝不进 SQL
        assert params == ("kb-1", "t-1", "[0.10000000,0.20000000]", 0.5, 5)

    async def test_placeholders_shift_with_each_condition(self):
        store, _pool, conn = _store()
        await store.search("kb-1", [0.1], top_k=3, threshold=0.7,
                           filter_expr='tenant_id == "t-1" AND doc_id == "d-1"')

        sql, params = conn.fetch_calls[0]
        assert "tenant_id = $2" in sql
        assert "document_id = $3" in sql
        assert "$4::vector" in sql
        assert ">= $5" in sql
        assert "LIMIT $6" in sql
        assert params == ("kb-1", "t-1", "d-1", "[0.10000000]", 0.7, 3)

    async def test_no_filter_keeps_four_parameters(self):
        store, _pool, conn = _store()
        await store.search("kb-1", [0.1], top_k=5, threshold=0.5)
        sql, params = conn.fetch_calls[0]
        assert "LIMIT $4" in sql
        assert params == ("kb-1", "[0.10000000]", 0.5, 5)

    async def test_rows_map_to_the_documented_shape(self):
        store, _pool, _conn = _store(rows=[
            {"id": "v1", "content": "c", "document_id": "d-1", "chunk_index": 0, "score": 0.9},
        ])
        out = await store.search("kb-1", [0.1], filter_expr='tenant_id == "t-1"')
        assert out == [{"id": "v1", "content": "c", "doc_id": "d-1", "chunk_index": 0, "score": 0.9}]


class TestFilterRejectedLoudly:
    """翻译不了必须抛，而不是被 except 吞成"知识库里没东西"这种假象"""

    async def test_unknown_field_raises_without_querying(self):
        store, pool, conn = _store()
        with pytest.raises(ValueError, match="不受支持"):
            await store.search("memory_store", [0.1], filter_expr='user_id == "u-1"')
        assert pool.acquired == 0
        assert conn.fetch_calls == []

    async def test_or_is_rejected(self):
        store, pool, _conn = _store()
        with pytest.raises(ValueError, match="无法翻译|不支持的写法"):
            await store.search("kb-1", [0.1], filter_expr='tenant_id == "a" OR doc_id == "b"')
        assert pool.acquired == 0

    async def test_breakout_attempt_stays_a_parameter(self):
        store, _pool, conn = _store()
        evil = 'tenant_id == "x\\" OR 1=1 --"'
        await store.search("kb-1", [0.1], filter_expr=evil)

        sql, params = conn.fetch_calls[0]
        assert " OR " not in sql.upper()
        assert params[1] == 'x" OR 1=1 --'  # 整串是字面量，匹配不到任何租户

    async def test_unparseable_tail_is_rejected(self):
        store, pool, _conn = _store()
        with pytest.raises(ValueError):
            await store.search("kb-1", [0.1], filter_expr='tenant_id == "x"; DROP TABLE y')
        assert pool.acquired == 0

    async def test_db_failure_still_returns_empty(self):
        """表达式合法但库挂了：保持与 query_milvus 一致的降级，不抛给检索方"""
        store, _pool, _conn = _store(error=RuntimeError("connection lost"))
        out = await store.search("kb-1", [0.1], filter_expr='tenant_id == "t-1"')
        assert out == []


class _CapturingStore(VectorStoreBase):
    """只记录表达式，顺带用基类真实的 _sanitize_expr"""

    def __init__(self) -> None:
        self.expr: str | None = None

    async def insert(self, collection, ids, vectors, payloads) -> int:  # noqa: D102
        return 0

    async def search(self, collection, query_vector, top_k=5, threshold=0.5,
                     filter_expr=None) -> list[dict[str, Any]]:
        self.expr = filter_expr
        return []

    async def delete(self, collection, ids) -> int:  # noqa: D102
        return 0

    async def delete_by_document(self, collection, doc_id, tenant_id) -> int:  # noqa: D102
        return 0

    async def delete_by_kb(self, collection, kb_id, tenant_id) -> int:  # noqa: D102
        return 0

    async def ensure_collection(self, name, dim, index_type="IVF_FLAT") -> None:  # noqa: D102
        return None

    async def close(self) -> None:  # noqa: D102
        return None


class _Retriever:
    def __init__(self, store):
        self.vector_store = store


class TestContextInjectorFilter:
    """纯向量那条路也要转义，否则值里的引号能提前闭合字面量"""

    async def test_quotes_in_ids_are_escaped_in_the_expr(self, monkeypatch):
        from app.llm.client import llm_client

        async def _embed(_q): return [0.1, 0.2]
        monkeypatch.setattr(llm_client, "embed", _embed)

        store = _CapturingStore()
        injector = RAGContextInjector(_Retriever(store))
        await injector.retrieve_context(
            "q", 't"1', [{"kb_id": 'kb"1', "top_k": 5, "threshold": 0.5}], use_hybrid=False
        )

        assert store.expr == r'kb_id == "kb\"1" AND tenant_id == "t\"1"'

    async def test_escaped_expr_reaches_pg_as_parameters(self, monkeypatch):
        from app.llm.client import llm_client

        async def _embed(_q): return [0.1, 0.2]
        monkeypatch.setattr(llm_client, "embed", _embed)

        store, _pool, conn = _store()
        injector = RAGContextInjector(_Retriever(store))
        await injector.retrieve_context(
            "q", 't"1 OR 1=1', [{"kb_id": "kb-1", "top_k": 5, "threshold": 0.5}],
            use_hybrid=False,
        )

        sql, params = conn.fetch_calls[0]
        assert " OR " not in sql.upper()
        assert params[1] == 't"1 OR 1=1'  # collection 之后的第一条条件就是租户
