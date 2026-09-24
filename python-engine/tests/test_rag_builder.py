# RAGBuilder 测试 — pgvector 存取 + 本地嵌入 fallback
# 不依赖真实 Milvus/pgvector，mock 网关与数据库层
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.rag.builder import RAGBuilder, VectorDBType


def make_builder(**kwargs):
    return RAGBuilder(llm_gateway=None, **kwargs)


def fake_pg_conn(conn=None):
    """mock _pg_conn：返回 (conn, release) 契约，conn 支持 async with transaction()"""
    conn = conn or AsyncMock()
    conn.transaction = MagicMock(return_value=AsyncMock())
    return AsyncMock(return_value=(conn, AsyncMock()))


class TestDefaults:
    """默认配置行为"""

    def test_default_vector_db_type_from_settings(self):
        assert VectorDBType(settings.vector_db_type) == VectorDBType.MILVUS
        builder = make_builder()
        assert builder._vector_db_type == VectorDBType(settings.vector_db_type)

    def test_invalid_vector_db_type_falls_back_to_milvus(self):
        # 非法配置不抛异常、不崩溃，回退默认
        builder = make_builder(vector_db_type="qdrant")
        assert builder._vector_db_type == VectorDBType.MILVUS

    async def test_local_embedding_disabled_by_default(self):
        builder = make_builder()
        assert await builder._get_local_embedding("hello") is None


class TestEmbedText:
    """统一嵌入器：本地优先、API 回退"""

    async def test_local_first_then_api_fallback(self):
        builder = make_builder()
        builder._get_local_embedding = AsyncMock(return_value=None)
        builder._get_api_embedding = AsyncMock(return_value=[0.1, 0.2])
        assert await builder._embed_text("hi") == [0.1, 0.2]
        builder._get_api_embedding.assert_awaited_once()

    async def test_local_result_skips_api(self):
        builder = make_builder()
        builder._get_local_embedding = AsyncMock(return_value=[0.5])
        builder._get_api_embedding = AsyncMock()
        assert await builder._embed_text("hi") == [0.5]
        builder._get_api_embedding.assert_not_awaited()


class TestLocalEmbedding:
    """本地嵌入可插拔：配置启用、失败回退"""

    async def test_enabled_uses_sentence_transformer(self, monkeypatch):
        monkeypatch.setattr(settings, "local_embedding_model", "mock-bge")
        builder = make_builder()

        fake_encoder = MagicMock()
        fake_encoder.encode.return_value = MagicMock(tolist=lambda: [0.1, 0.2, 0.3])
        fake_st = MagicMock()
        fake_st.SentenceTransformer.return_value = fake_encoder

        with patch.dict(sys.modules, {"sentence_transformers": fake_st}):
            result = await builder._get_local_embedding("hello")
            await builder._get_local_embedding("again")

        fake_st.SentenceTransformer.assert_called_once_with("mock-bge")  # 惰性加载只一次
        assert result == [0.1, 0.2, 0.3]

    async def test_failure_falls_back_to_none(self, monkeypatch):
        monkeypatch.setattr(settings, "local_embedding_model", "mock-bge")
        builder = make_builder()

        with patch.dict(sys.modules, {"sentence_transformers": None}):  # 依赖缺失
            result = await builder._get_local_embedding("hello")

        assert result is None  # 依赖缺失/失败 → 回退 API


class TestStorePgvector:
    """pgvector 存储"""

    def test_invalid_table_name_rejected(self, monkeypatch):
        monkeypatch.setattr(settings, "pgvector_table", "kb; DROP TABLE users")
        builder = make_builder()
        with pytest.raises(ValueError, match="非法表名"):
            builder._validate_table_name(settings.pgvector_table)

    async def test_dimension_mismatch_all_skipped_raises(self):
        builder = make_builder()
        with patch.object(builder, "_pg_conn", new=fake_pg_conn()) as pg:
            with pytest.raises(ValueError, match="维度"):
                await builder._store_pgvector(
                    "kb-1", "doc-1", "tenant-1",
                    [{"index": 0, "content": "a"}],
                    [[0.1, 0.2]],  # 2 维 != embedding_dim
                )
        pg.assert_not_awaited()

    async def test_dimension_mismatch_partial_skipped(self, monkeypatch):
        monkeypatch.setattr(settings, "embedding_dim", 2)
        builder = make_builder()
        conn = AsyncMock()
        with patch.object(builder, "_pg_conn", new=fake_pg_conn(conn)):
            await builder._store_pgvector(
                "kb-1", "doc-1", "tenant-1",
                [{"index": 0, "content": "a"}, {"index": 1, "content": "b"}],
                [[0.1, 0.2], [0.3]],  # 第二个维度不符被跳过
            )
        # INSERT 只有 1 行；先 DELETE 再 INSERT
        sqls = [c.args[0] for c in conn.execute.await_args_list]
        assert sqls[0].startswith("DELETE FROM")
        assert "INSERT INTO" in sqls[1]
        insert_args = conn.execute.await_args_list[1].args
        assert len(insert_args[1]) == 1  # 只插入维度匹配的 1 行

    async def test_skips_empty_embeddings(self, monkeypatch):
        monkeypatch.setattr(settings, "embedding_dim", 2)
        builder = make_builder()
        conn = AsyncMock()
        with patch.object(builder, "_pg_conn", new=fake_pg_conn(conn)):
            await builder._store_pgvector(
                "kb-1", "doc-1", "tenant-1",
                [{"index": 0, "content": "a"}, {"index": 1, "content": "b"}],
                [None, [0.1, 0.2]],
            )

        # 空向量被跳过：先 DELETE（按文档清理）再 INSERT 1 行
        sqls = [c.args[0] for c in conn.execute.await_args_list]
        assert len(sqls) == 2
        assert sqls[0].startswith("DELETE FROM") and "document_id" in sqls[0]
        insert_args = conn.execute.await_args_list[1].args
        assert len(insert_args[1]) == 1  # id 数组
        assert len(insert_args[5]) == 1  # chunk_index 数组
        assert insert_args[7][0] == "[0.10000000,0.20000000]"  # 向量文本 (.8f)

    async def test_no_valid_vectors_raises(self):
        """嵌入全失败时 fail loud，避免 worker 误标 completed"""
        builder = make_builder()
        with patch.object(builder, "_pg_conn", new=fake_pg_conn()) as pg:
            with pytest.raises(ValueError, match="无有效向量"):
                await builder._store_pgvector(
                    "kb-1", "doc-1", "tenant-1",
                    [{"index": 0, "content": "a"}],
                    [None],
                )
        pg.assert_not_awaited()

    async def test_sql_uses_configured_table(self, monkeypatch):
        monkeypatch.setattr(settings, "embedding_dim", 2)
        builder = make_builder()
        conn = AsyncMock()
        with patch.object(builder, "_pg_conn", new=fake_pg_conn(conn)):
            await builder._store_pgvector(
                "kb-1", "doc-1", "tenant-1",
                [{"index": 0, "content": "a"}],
                [[0.5, 0.25]],
            )
        insert_sql = conn.execute.await_args_list[1].args[0]
        assert settings.pgvector_table in insert_sql
        assert "embedding::vector" in insert_sql
        assert "DELETE FROM" in conn.execute.await_args_list[0].args[0]  # 重建幂等


class TestQueryPgvector:
    """pgvector 查询"""

    async def test_query_entry_invalid_vector_db_falls_back(self):
        """用户可控 vector_db 非法时不抛 500，回退默认类型"""
        builder = make_builder()  # 默认 milvus
        builder.query_milvus = AsyncMock(return_value=[{"id": "x"}])
        builder.query_pgvector = AsyncMock()
        result = await builder.query("kb-1", "query", vector_db="bogus")
        assert result == [{"id": "x"}]
        builder.query_milvus.assert_awaited_once()
        builder.query_pgvector.assert_not_awaited()

    async def test_returns_milvus_compatible_shape(self):
        builder = make_builder()
        builder._get_local_embedding = AsyncMock(return_value=[0.1, 0.2])

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[
            {"id": "v1", "document_id": "doc-1", "chunk_index": 2, "content": "chunk", "score": 0.87},
        ])
        release = AsyncMock()
        with patch.object(builder, "_pg_conn", new=AsyncMock(return_value=(conn, release))):
            result = await builder.query_pgvector("kb-1", "query", top_k=5, threshold=0.5)

        assert result == [{"id": "v1", "doc_id": "doc-1", "chunk_index": 2,
                           "content": "chunk", "score": 0.87}]
        sql, *params = conn.fetch.await_args.args
        assert settings.pgvector_table in sql
        assert "<=>" in sql  # 余弦距离
        assert params[0] == "kb-1"
        assert params[1] == "[0.10000000,0.20000000]"
        assert params[2] == 0.5
        assert params[3] == 5
        release.assert_awaited_once()

    async def test_no_query_embedding_returns_empty(self):
        builder = make_builder()
        builder._get_local_embedding = AsyncMock(return_value=None)
        builder._get_api_embedding = AsyncMock(return_value=None)
        with patch.object(builder, "_pg_conn", new=fake_pg_conn()) as pg:
            result = await builder.query_pgvector("kb-1", "query")
        assert result == []
        pg.assert_not_awaited()

    async def test_db_error_returns_empty(self):
        builder = make_builder()
        builder._get_local_embedding = AsyncMock(return_value=[0.1])

        conn = AsyncMock()
        conn.fetch = AsyncMock(side_effect=RuntimeError("connection lost"))
        release = AsyncMock()
        with patch.object(builder, "_pg_conn", new=AsyncMock(return_value=(conn, release))):
            result = await builder.query_pgvector("kb-1", "query")

        assert result == []  # 与 query_milvus 失败行为一致
        release.assert_awaited_once()

    async def test_connect_failure_returns_empty(self):
        builder = make_builder()
        builder._get_local_embedding = AsyncMock(return_value=[0.1])
        with patch.object(builder, "_pg_conn", new=AsyncMock(side_effect=RuntimeError("no pg"))):
            result = await builder.query_pgvector("kb-1", "query")
        assert result == []

    async def test_invalid_table_name_rejected(self, monkeypatch):
        monkeypatch.setattr(settings, "pgvector_table", "bad name; DROP")
        builder = make_builder()
        builder._get_local_embedding = AsyncMock(return_value=[0.1])
        with patch.object(builder, "_pg_conn", new=fake_pg_conn()) as pg:
            with pytest.raises(ValueError, match="非法表名"):
                await builder.query_pgvector("kb-1", "query")
        pg.assert_not_awaited()
