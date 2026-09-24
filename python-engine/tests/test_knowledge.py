"""Tests for knowledge base API endpoints."""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_pool():
    """Mock asyncpg pool."""
    pool = AsyncMock()
    return pool


@pytest.fixture
def mock_user_id():
    return "test-user-123"


# ── Shared row factories ──

def _make_kb_row(**overrides):
    """Build a knowledge_bases row dict with sensible defaults."""
    base = {
        "id": "kb-1",
        "name": "My KB",
        "description": "desc",
        "type": "wiki",
        "visibility": "private",
        "status": "active",
        "document_count": 0,
        "total_size_bytes": 0,
        "credits_consumed": 0,
        "config": None,
        "created_at": datetime(2026, 1, 1),
        "updated_at": datetime(2026, 1, 1),
    }
    base.update(overrides)
    return base


# ═══════════════════════════════════════════════════
# LIST tests (existing)
# ═══════════════════════════════════════════════════

class TestKnowledgeList:
    """Tests for GET /v1/kb endpoint."""

    async def test_list_returns_empty_when_no_pool(self):
        with patch("app.api.knowledge.get_pool", side_effect=RuntimeError("no pool")):
            from app.api.knowledge import list_knowledge_bases
            result = await list_knowledge_bases(user_id="u1")
            assert result == {"knowledge_bases": [], "count": 0}

    async def test_list_returns_user_and_public_kbs(self, mock_pool):
        mock_rows = [
            _make_kb_row(id="kb-1", name="My KB", visibility="private"),
            _make_kb_row(id="kb-2", name="Public KB", type="rag", visibility="public"),
        ]
        mock_pool.fetch = AsyncMock(return_value=mock_rows)

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from app.api.knowledge import list_knowledge_bases
            result = await list_knowledge_bases(user_id="u1")

        assert result["count"] == 2
        assert result["knowledge_bases"][0]["id"] == "kb-1"
        assert result["knowledge_bases"][1]["id"] == "kb-2"

    async def test_list_queries_with_correct_params(self, mock_pool):
        mock_pool.fetch = AsyncMock(return_value=[])

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from app.api.knowledge import list_knowledge_bases
            await list_knowledge_bases(user_id="user-abc")

        call_args = mock_pool.fetch.call_args
        query = call_args[0][0]
        assert "user_id = $1" in query
        assert "visibility = 'public'" in query
        # 租户共享（visibility='tenant'）必须带租户条件：少了它，"租户内共享"
        # 就退化成"全站共享" —— 任何租户的用户都能列出并读取别人的知识库。
        assert "tenant_id = $2" in query, "tenant 可见性缺少租户条件"
        assert call_args[0][1] == "user-abc"
        # 未显式传租户时回落到默认租户（保守），而不是"不过滤"。
        from app.api.knowledge import DEFAULT_TENANT_ID

        assert call_args[0][2] == DEFAULT_TENANT_ID


class TestKnowledgeTenantScope:
    """SEC-11 回归：租户共享的可见范围必须限定在同租户内。"""

    async def test_list_uses_caller_tenant(self):
        mock_pool = AsyncMock()
        mock_pool.fetch = AsyncMock(return_value=[])

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from app.api.knowledge import list_knowledge_bases

            await list_knowledge_bases(user_id="user-abc", tenant_id="tenant-xyz")

        assert mock_pool.fetch.call_args[0][2] == "tenant-xyz"

    async def test_get_requires_tenant_match(self):
        mock_pool = AsyncMock()
        mock_pool.fetchrow = AsyncMock(return_value=None)

        from fastapi import HTTPException

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from app.api.knowledge import get_knowledge_base

            with pytest.raises(HTTPException) as exc:
                await get_knowledge_base("kb-1", "user-abc", tenant_id="tenant-xyz")

        assert exc.value.status_code == 404
        query = mock_pool.fetchrow.call_args[0][0]
        assert "tenant_id = $3" in query, "按 id 直读也必须校验租户"

    async def test_create_records_caller_tenant(self):
        mock_pool = AsyncMock()
        mock_pool.execute = AsyncMock(return_value=None)

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from app.api.knowledge import create_knowledge_base

            await create_knowledge_base(
                user_id="user-abc", name="KB", tenant_id="tenant-xyz"
            )

        # INSERT 的 tenant_id 必须是真实租户，否则查询端永远匹配不上
        assert "tenant-xyz" in mock_pool.execute.call_args[0]


# ═══════════════════════════════════════════════════
# CREATE tests
# ═══════════════════════════════════════════════════

class TestKnowledgeCreate:
    """Tests for POST /v1/kb endpoint."""

    async def test_create_kb_returns_id(self, mock_pool):
        mock_pool.execute = AsyncMock(return_value="INSERT 0 1")

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from app.api.knowledge import create_knowledge_base
            result = await create_knowledge_base(
                user_id="u1",
                name="New KB",
                description="some desc",
                kb_type="wiki",
                visibility="private",
            )

        assert "id" in result
        assert result["name"] == "New KB"
        assert result["type"] == "wiki"
        assert result["visibility"] == "private"
        # Verify pool.execute was called with the INSERT
        mock_pool.execute.assert_called_once()
        call_args = mock_pool.execute.call_args
        assert "INSERT INTO knowledge_bases" in call_args[0][0]

    async def test_create_rejects_empty_name(self, mock_pool):
        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from fastapi import HTTPException

            from app.api.knowledge import create_knowledge_base
            with pytest.raises(HTTPException) as exc_info:
                await create_knowledge_base(user_id="u1", name="")
            assert exc_info.value.status_code == 400
            assert "name" in exc_info.value.detail.lower()

    async def test_create_rejects_invalid_type(self, mock_pool):
        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from fastapi import HTTPException

            from app.api.knowledge import create_knowledge_base
            with pytest.raises(HTTPException) as exc_info:
                await create_knowledge_base(user_id="u1", name="Valid", kb_type="invalid")
            assert exc_info.value.status_code == 400
            assert "type" in exc_info.value.detail.lower()


# ═══════════════════════════════════════════════════
# GET tests
# ═══════════════════════════════════════════════════

class TestKnowledgeGet:
    """Tests for GET /v1/kb/{kb_id} endpoint."""

    async def test_get_kb_by_id(self, mock_pool):
        mock_pool.fetchrow = AsyncMock(return_value=_make_kb_row(id="kb-42", name="Found KB"))

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from app.api.knowledge import get_knowledge_base
            result = await get_knowledge_base(kb_id="kb-42", user_id="u1")

        assert result["id"] == "kb-42"
        assert result["name"] == "Found KB"
        mock_pool.fetchrow.assert_called_once()

    async def test_get_kb_not_found(self, mock_pool):
        mock_pool.fetchrow = AsyncMock(return_value=None)

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from fastapi import HTTPException

            from app.api.knowledge import get_knowledge_base
            with pytest.raises(HTTPException) as exc_info:
                await get_knowledge_base(kb_id="nonexistent", user_id="u1")
            assert exc_info.value.status_code == 404


# ═══════════════════════════════════════════════════
# UPDATE tests
# ═══════════════════════════════════════════════════

class TestKnowledgeUpdate:
    """Tests for PUT /v1/kb/{kb_id} endpoint."""

    async def test_update_kb_name(self, mock_pool):
        mock_pool.fetchrow = AsyncMock(return_value=_make_kb_row(id="kb-1", user_id="u1", visibility="private"))
        mock_pool.execute = AsyncMock(return_value="UPDATE 1")

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from app.api.knowledge import update_knowledge_base
            result = await update_knowledge_base(
                kb_id="kb-1",
                user_id="u1",
                name="Updated Name",
            )

        assert result["id"] == "kb-1"
        assert result["updated"] is True
        # Verify the UPDATE query was executed
        mock_pool.execute.assert_called_once()
        call_args = mock_pool.execute.call_args
        assert "UPDATE knowledge_bases" in call_args[0][0]
        assert "name = $1" in call_args[0][0]


# ═══════════════════════════════════════════════════
# DELETE tests
# ═══════════════════════════════════════════════════

class TestKnowledgeDelete:
    """Tests for DELETE /v1/kb/{kb_id} endpoint."""

    async def test_delete_own_kb(self, mock_pool):
        mock_pool.fetchrow = AsyncMock(return_value={"id": "kb-1", "user_id": "u1"})
        mock_pool.execute = AsyncMock(return_value="DELETE 1")

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from app.api.knowledge import delete_knowledge_base
            result = await delete_knowledge_base(kb_id="kb-1", user_id="u1")

        assert result["id"] == "kb-1"
        assert result["deleted"] is True
        mock_pool.execute.assert_called_once()

    async def test_delete_not_found(self, mock_pool):
        mock_pool.fetchrow = AsyncMock(return_value=None)

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from fastapi import HTTPException

            from app.api.knowledge import delete_knowledge_base
            with pytest.raises(HTTPException) as exc_info:
                await delete_knowledge_base(kb_id="nonexistent", user_id="u1")
            assert exc_info.value.status_code == 404


# ═══════════════════════════════════════════════════
# DOCUMENT UPLOAD tests
# ═══════════════════════════════════════════════════

class TestDocumentUpload:
    """Tests for POST /v1/kb/{kb_id}/documents endpoint."""

    async def test_upload_document(self, mock_pool):
        """Uploading a document inserts it and updates KB stats."""
        mock_pool.fetchrow = AsyncMock(return_value={
            "id": "kb-1",
            "status": "active",
            "user_id": "u1",
        })
        mock_pool.execute = AsyncMock(return_value="INSERT 0 1")

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from app.api.knowledge import upload_document
            result = await upload_document(
                kb_id="kb-1",
                user_id="u1",
                name="readme.pdf",
                file_type="pdf",
                file_size_bytes=1024,
            )

        assert "id" in result
        assert result["knowledge_base_id"] == "kb-1"
        assert result["name"] == "readme.pdf"
        assert result["file_type"] == "pdf"
        assert result["file_size_bytes"] == 1024
        assert result["status"] == "pending"

        # Two SQL calls: INSERT doc + UPDATE KB stats
        assert mock_pool.execute.call_count == 2
        insert_call = mock_pool.execute.call_args_list[0]
        assert "INSERT INTO knowledge_documents" in insert_call[0][0]
        update_call = mock_pool.execute.call_args_list[1]
        assert "UPDATE knowledge_bases" in update_call[0][0]
        assert "document_count" in update_call[0][0]

    async def test_upload_rejects_building_kb(self, mock_pool):
        """Upload must be rejected when KB status is 'building'."""
        mock_pool.fetchrow = AsyncMock(return_value={
            "id": "kb-1",
            "status": "building",
            "user_id": "u1",
        })

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from fastapi import HTTPException

            from app.api.knowledge import upload_document
            with pytest.raises(HTTPException) as exc_info:
                await upload_document(
                    kb_id="kb-1",
                    user_id="u1",
                    name="doc.txt",
                    file_type="txt",
                    file_size_bytes=100,
                )
            assert exc_info.value.status_code == 409
            assert "building" in exc_info.value.detail.lower()


# ═══════════════════════════════════════════════════
# DOCUMENT LIST tests
# ═══════════════════════════════════════════════════

class TestDocumentList:
    """Tests for GET /v1/kb/{kb_id}/documents endpoint."""

    async def test_list_documents(self, mock_pool):
        """Listing documents returns all docs for the KB."""
        now = datetime(2026, 1, 1)
        mock_pool.fetchrow = AsyncMock(return_value={
            "id": "kb-1",
            "user_id": "u1",
            "visibility": "private",
        })
        mock_pool.fetch = AsyncMock(return_value=[
            {
                "id": "doc-1",
                "knowledge_base_id": "kb-1",
                "name": "readme.pdf",
                "file_url": "",
                "file_type": "pdf",
                "file_size_bytes": 1024,
                "chunk_count": 0,
                "status": "pending",
                "error_message": "",
                "metadata": None,
                "created_at": now,
            },
            {
                "id": "doc-2",
                "knowledge_base_id": "kb-1",
                "name": "notes.txt",
                "file_url": "",
                "file_type": "txt",
                "file_size_bytes": 512,
                "chunk_count": 0,
                "status": "completed",
                "error_message": "",
                "metadata": None,
                "created_at": now,
            },
        ])

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from app.api.knowledge import list_documents
            result = await list_documents(kb_id="kb-1", user_id="u1")

        assert result["count"] == 2
        assert result["documents"][0]["id"] == "doc-1"
        assert result["documents"][0]["name"] == "readme.pdf"
        assert result["documents"][1]["id"] == "doc-2"
        assert result["documents"][1]["name"] == "notes.txt"


# ═══════════════════════════════════════════════════
# BUILD tests
# ═══════════════════════════════════════════════════

class TestKnowledgeBuild:
    """Tests for POST /v1/kb/{kb_id}/build endpoint."""

    async def test_build_calculates_cost(self, mock_pool):
        """Building a wiki KB calculates cost with coefficient 0.5."""
        mock_pool.fetchrow = AsyncMock(side_effect=[
            # First call: KB row
            _make_kb_row(
                id="kb-1", type="wiki", document_count=3,
                total_size_bytes=10000, user_id="u1",
            ),
            # Second call: user credits
            {"credits": 500},
        ])
        mock_pool.execute = AsyncMock(return_value="UPDATE 1")

        # Mock async context managers for acquire() and transaction()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        class _AcquireCM:
            async def __aenter__(self):
                return mock_conn
            async def __aexit__(self, *a):
                return False

        class _TxnCM:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False

        mock_conn.transaction = MagicMock(return_value=_TxnCM())
        mock_pool.acquire = MagicMock(return_value=_AcquireCM())

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from app.api.knowledge import build_knowledge_base
            result = await build_knowledge_base(kb_id="kb-1", user_id="u1")

        assert result["status"] == "active"
        assert result["doc_count"] == 3
        assert result["type"] == "wiki"
        # wiki: 10000 * 0.5 * 0.001 = 5.0
        assert result["estimated_cost"] == 5.0

        # Verify building→active lifecycle
        build_call = mock_pool.execute.call_args
        assert "building" in build_call[0][0]

    async def test_build_rejects_empty_docs(self, mock_pool):
        """Building a KB with no documents is rejected."""
        mock_pool.fetchrow = AsyncMock(return_value=_make_kb_row(
            id="kb-1", type="wiki", document_count=0,
            total_size_bytes=0, user_id="u1",
        ))

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from fastapi import HTTPException

            from app.api.knowledge import build_knowledge_base
            with pytest.raises(HTTPException) as exc_info:
                await build_knowledge_base(kb_id="kb-1", user_id="u1")
            assert exc_info.value.status_code == 400
            assert "no documents" in exc_info.value.detail.lower()


# ═══════════════════════════════════════════════════
# QUERY tests
# ═══════════════════════════════════════════════════

class TestKnowledgeQuery:
    """Tests for POST /v1/kb/{kb_id}/query endpoint."""

    async def test_query_wiki(self, mock_pool):
        """Querying a wiki KB performs full-text search with Chinese analyzer."""
        mock_pool.fetchrow = AsyncMock(return_value={
            "id": "kb-1",
            "type": "wiki",
            "user_id": "u1",
            "visibility": "private",
        })
        mock_pool.fetch = AsyncMock(return_value=[
            {
                "id": "doc-1", "knowledge_base_id": "kb-1",
                "name": "readme.pdf", "file_type": "pdf",
                "status": "completed", "rank": 0.85,
            },
            {
                "id": "doc-2", "knowledge_base_id": "kb-1",
                "name": "guide.txt", "file_type": "txt",
                "status": "completed", "rank": 0.42,
            },
        ])

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from app.api.knowledge import query_knowledge_base
            result = await query_knowledge_base(
                kb_id="kb-1", user_id="u1", query="test query", top_k=5,
            )

        assert result["type"] == "wiki"
        assert len(result["results"]) == 2
        assert result["results"][0]["id"] == "doc-1"
        assert result["results"][0]["rank"] == 0.85
        assert result["results"][1]["id"] == "doc-2"

        # Verify the SQL uses Chinese full-text search
        call_args = mock_pool.fetch.call_args
        query_sql = call_args[0][0]
        assert "plainto_tsquery('chinese'" in query_sql
        assert "ts_rank" in query_sql

    async def test_query_wiki_converts_bytea_content_to_text(self, mock_pool):
        """wiki 全文搜索必须先把 BYTEA 的 content 转回文本，否则真实库上 to_tsvector 报错。

        knowledge_documents.content 是 BYTEA 列（异步 RAG 构建存原始字节），
        直接对其调用 to_tsvector('chinese', content) 在 PostgreSQL 会报
        'function to_tsvector(unknown, bytea) does not exist' 导致查询 500。
        """
        mock_pool.fetchrow = AsyncMock(return_value={
            "id": "kb-1",
            "type": "wiki",
            "user_id": "u1",
            "visibility": "private",
        })
        mock_pool.fetch = AsyncMock(return_value=[])

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from app.api.knowledge import query_knowledge_base
            await query_knowledge_base(
                kb_id="kb-1", user_id="u1", query="test", top_k=5,
            )

        call_args = mock_pool.fetch.call_args
        query_sql = call_args[0][0]
        # 不再把 bytea 直接传给 to_tsvector
        assert "to_tsvector('chinese', COALESCE(content, ''))" not in query_sql
        assert "convert_from(content, 'UTF8')" in query_sql

    async def test_query_wiki_falls_back_to_empty_on_db_error(self, mock_pool):
        """二进制文档无法按 UTF-8 解码时 to_tsvector 抛错，应降级返回空结果而非 500。"""
        mock_pool.fetchrow = AsyncMock(return_value={
            "id": "kb-1",
            "type": "wiki",
            "user_id": "u1",
            "visibility": "private",
        })
        mock_pool.fetch = AsyncMock(
            side_effect=Exception('function to_tsvector(unknown, bytea) does not exist')
        )

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from app.api.knowledge import query_knowledge_base
            result = await query_knowledge_base(
                kb_id="kb-1", user_id="u1", query="test", top_k=5,
            )

        assert result["type"] == "wiki"
        assert result["results"] == []


# ═══════════════════════════════════════════════════
# ADMIN LIST tests
# ═══════════════════════════════════════════════════

class TestAdminList:
    """Tests for GET /v1/kb/admin/list endpoint."""

    async def test_admin_list_returns_public_kbs(self, mock_pool):
        """Admin list returns only public knowledge bases."""
        mock_rows = [
            _make_kb_row(id="kb-1", name="Public Wiki", visibility="public", type="wiki"),
            _make_kb_row(id="kb-2", name="Public RAG", visibility="public", type="rag"),
        ]
        mock_pool.fetch = AsyncMock(return_value=mock_rows)

        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            from app.api.knowledge import admin_list_knowledge_bases
            result = await admin_list_knowledge_bases()

        assert result["count"] == 2
        assert result["knowledge_bases"][0]["id"] == "kb-1"
        assert result["knowledge_bases"][0]["name"] == "Public Wiki"
        assert result["knowledge_bases"][0]["visibility"] == "public"
        assert result["knowledge_bases"][1]["id"] == "kb-2"
        assert result["knowledge_bases"][1]["name"] == "Public RAG"
        assert result["knowledge_bases"][1]["visibility"] == "public"

        # Verify the SQL filters only public
        call_args = mock_pool.fetch.call_args
        query_sql = call_args[0][0]
        assert "visibility = 'public'" in query_sql
        assert "user_id" not in query_sql  # no user filter for admin

    async def test_admin_list_returns_empty_when_no_pool(self):
        """Gracefully returns empty when no DB pool."""
        with patch("app.api.knowledge.get_pool", side_effect=RuntimeError("no pool")):
            from app.api.knowledge import admin_list_knowledge_bases
            result = await admin_list_knowledge_bases()
            assert result == {"knowledge_bases": [], "count": 0}

    async def test_admin_list_rejects_non_admin_role(self):
        """Route returns 403 when X-User-Role is not admin/owner."""
        from fastapi import FastAPI
        from starlette.testclient import TestClient

        from app.api.knowledge import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        # No role header
        response = client.get("/v1/kb/admin/list")
        assert response.status_code == 403
        assert "admin" in response.json()["detail"].lower()

        # Wrong role
        response = client.get("/v1/kb/admin/list", headers={"X-User-Role": "viewer"})
        assert response.status_code == 403

    async def test_admin_list_allows_admin_role(self, mock_pool):
        """Route succeeds when X-User-Role is 'admin'."""
        mock_pool.fetch = AsyncMock(return_value=[])

        from fastapi import FastAPI

        from app.api.knowledge import router

        app = FastAPI()
        app.include_router(router)

        # Use httpx async client via starlette
        from starlette.testclient import TestClient
        with patch("app.api.knowledge.get_pool", return_value=mock_pool):
            client = TestClient(app)
            response = client.get("/v1/kb/admin/list", headers={"X-User-Role": "admin"})

        assert response.status_code == 200
        assert response.json()["count"] == 0
