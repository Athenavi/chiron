# RAG 检索测试
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.knowledge.enhanced_kb import EnhancedKnowledgeBase
from app.rag.retriever import RAGRetriever


class TestRAGRetriever:
    """测试 RAG 检索器"""

    def test_split_text_basic(self):
        """测试基本文本分块"""
        retriever = RAGRetriever()
        text = "Hello World " * 100  # 1200 chars
        chunks = retriever._split_text(text, chunk_size=500, chunk_overlap=100)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 500

    def test_split_text_short(self):
        """测试短文本分块"""
        retriever = RAGRetriever()
        text = "Hello World"
        chunks = retriever._split_text(text, chunk_size=500, chunk_overlap=100)
        assert len(chunks) == 1
        assert chunks[0] == "Hello World"

    def test_split_text_empty(self):
        """测试空文本分块"""
        retriever = RAGRetriever()
        chunks = retriever._split_text("", chunk_size=500, chunk_overlap=100)
        assert len(chunks) == 0

    def test_split_text_overlap(self):
        """测试分块重叠"""
        retriever = RAGRetriever()
        text = "A" * 1000
        chunks = retriever._split_text(text, chunk_size=500, chunk_overlap=200)
        assert len(chunks) == 3
        # 验证重叠
        assert chunks[0][-200:] == chunks[1][:200]

    @pytest.mark.asyncio
    async def test_index_document_milvus_unavailable(self):
        """测试 Milvus 不可用时的文档索引"""
        retriever = RAGRetriever()
        # Milvus 未连接，应返回降级结果
        result = await retriever.index_document(
            tenant_id="test",
            document_id="doc1",
            content="Hello World",
        )
        assert result["status"] == "indexed"  # 跳过但标记为 indexed
        assert result["chunks_count"] > 0

    @pytest.mark.asyncio
    async def test_retrieve_milvus_unavailable(self):
        """测试 Milvus 不可用时的检索"""
        retriever = RAGRetriever()
        results = await retriever.retrieve(
            tenant_id="test",
            query="Hello",
        )
        assert results == []


class TestTextChunking:
    """测试文本分块算法"""

    def test_markdown_splitting(self):
        """测试 Markdown 文本分块"""
        retriever = RAGRetriever()
        md_text = """# Title

## Section 1

Content 1

## Section 2

Content 2"""
        chunks = retriever._split_text(md_text, chunk_size=50, chunk_overlap=10)
        assert len(chunks) > 1

    def test_code_splitting(self):
        """测试代码分块"""
        retriever = RAGRetriever()
        code = """def hello():
    print("Hello")

def world():
    print("World")"""
        chunks = retriever._split_text(code, chunk_size=40, chunk_overlap=10)
        assert len(chunks) > 1


class TestKnowledgeBaseFailLoud:
    """知识库 PG 依赖接口必须有真实数据源。

    list_documents / get_tenant_stats 已实现 (PG knowledge_documents 表),
    但依赖数据库连接池: 未初始化时必须显式报错,
    绝不允许返回硬编码 0/空列表伪装成真实数据 ——
    否则调用方无法区分"真的 0 条"和"连接未就绪"。
    """

    @pytest.mark.asyncio
    async def test_tenant_stats_without_db_fails_loud(self):
        kb = EnhancedKnowledgeBase()
        with pytest.raises(RuntimeError, match="not initialized"):
            await kb.get_tenant_stats(tenant_id="tenant-x")

    @pytest.mark.asyncio
    async def test_list_documents_without_db_fails_loud(self):
        kb = EnhancedKnowledgeBase()
        with pytest.raises(RuntimeError, match="not initialized"):
            await kb.list_documents(tenant_id="tenant-x")

    @pytest.mark.asyncio
    async def test_list_documents_queries_pg_with_tenant_filter(self):
        """list_documents 必须按 tenant_id 过滤查询 PG。"""
        kb = EnhancedKnowledgeBase()

        fake_pool = MagicMock()
        fake_row = {
            "id": "doc-1",
            "knowledge_base_id": "kb-1",
            "name": "测试文档",
            "file_type": "txt",
            "file_size_bytes": 1024,
            "file_url": None,
            "status": "indexed",
            "chunk_count": 5,
            "created_at": None,
            "updated_at": None,
        }
        fake_pool.fetch = AsyncMock(return_value=[fake_row])

        with patch("app.db.get_pool", return_value=fake_pool):
            docs = await kb.list_documents(tenant_id="tenant-x")

        assert len(docs) == 1
        assert docs[0]["document_id"] == "doc-1"
        assert docs[0]["name"] == "测试文档"
        # 验证 SQL 带 tenant_id 参数
        call_args = fake_pool.fetch.call_args
        assert call_args.args[1] == "tenant-x"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
