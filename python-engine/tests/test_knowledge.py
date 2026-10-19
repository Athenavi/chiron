"""知识库 API 单元测试（PageIndex 版）。

只覆盖**不依赖外部服务**的部分：字段序列化、权限与 provider 一致性 gate、上传前校验。
真实的索引/检索需要 LLM 凭据（P0 结论），由部署环境验证。

旧的 wiki/rag 双分支用例整体退场：那两个分支连同 ``app/rag`` 一起被本轮重构删除。
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.knowledge import (
    _content_type,
    _serialize_doc,
    _serialize_kb,
    build_knowledge_base,
    create_knowledge_base,
    list_knowledge_bases,
    put_user_settings,
    query_knowledge_base,
    upload_document,
)
from app.knowledge.providers.base import STATUS_READY, HealthResult
from app.knowledge.settings_store import UserKnowledgeSettings


def _kb_row(**overrides):
    base = {
        "id": "kb-1",
        "name": "KB",
        "description": "d",
        "provider": "selfhosted",
        "visibility": "private",
        "status": "disabled",
        "document_count": 0,
        "total_size_bytes": 0,
        "credits_consumed": 0,
        "config": None,
        "error_message": None,
        "created_at": datetime(2026, 1, 1),
        "updated_at": datetime(2026, 1, 1),
        "tenant_id": "t1",
        "user_id": "u1",
        "index_ref": "",
    }
    base.update(overrides)
    return base


def _doc_row(**overrides):
    base = {
        "id": "doc-1",
        "knowledge_base_id": "kb-1",
        "name": "a.pdf",
        "file_type": "pdf",
        "file_size_bytes": 10,
        "page_count": 3,
        "status": "ready",
        "pageindex_doc_id": "pi-1",
        "error_message": None,
        "metadata": None,
        "storage_key": "t1/kb-1/doc-1/a.pdf",
        "created_at": datetime(2026, 1, 1),
        "updated_at": datetime(2026, 1, 1),
    }
    base.update(overrides)
    return base


def _settings(provider: str = "selfhosted") -> UserKnowledgeSettings:
    return UserKnowledgeSettings(user_id="u1", tenant_id="t1", provider=provider)


# ── 序列化 ──


def test_serialize_kb_exposes_provider_semantics() -> None:
    out = _serialize_kb(_kb_row())
    assert out["provider"] == "selfhosted"
    assert out["status"] == "disabled"
    assert out["error_message"] == ""
    assert "type" not in out  # 旧字段必须消失，否则前端会继续按 wiki/rag 分流
    assert out["created_at"].startswith("2026-01-01")


def test_serialize_doc_exposes_pageindex_fields() -> None:
    out = _serialize_doc(_doc_row())
    assert out["pageindex_doc_id"] == "pi-1"
    assert out["page_count"] == 3
    assert "chunk_count" not in out


def test_content_type_mapping() -> None:
    assert _content_type("a.pdf", "") == "application/pdf"
    assert _content_type("a.unknownext", "") == "application/octet-stream"


# ── 列表 / 可用性 ──


async def test_list_without_pool_returns_empty() -> None:
    with patch("app.api.knowledge.get_pool", side_effect=RuntimeError("no pool")):
        assert await list_knowledge_bases("u1") == {"knowledge_bases": [], "count": 0}


# ── 创建：provider 二选一 ──


async def test_create_requires_provider_choice() -> None:
    with patch("app.api.knowledge.settings_store.load", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await create_knowledge_base(user_id="u1", tenant_id="t1", name="KB")
    assert exc.value.status_code == 400
    assert "provider" in exc.value.detail


async def test_create_rejects_unknown_provider() -> None:
    with patch("app.api.knowledge.settings_store.load", AsyncMock(return_value=_settings())):
        with pytest.raises(HTTPException) as exc:
            await create_knowledge_base(
                user_id="u1", tenant_id="t1", name="KB", provider="pinecone"
            )
    assert exc.value.status_code == 400


async def test_create_rejects_provider_mismatch() -> None:
    with patch(
        "app.api.knowledge.settings_store.load", AsyncMock(return_value=_settings("selfhosted"))
    ):
        with pytest.raises(HTTPException) as exc:
            await create_knowledge_base(
                user_id="u1", tenant_id="t1", name="KB", provider="cloud"
            )
    assert exc.value.status_code == 409


async def test_create_rejects_bad_visibility() -> None:
    with patch("app.api.knowledge.settings_store.load", AsyncMock(return_value=_settings())):
        with pytest.raises(HTTPException) as exc:
            await create_knowledge_base(
                user_id="u1", tenant_id="t1", name="KB", visibility="everyone"
            )
    assert exc.value.status_code == 400


async def test_create_persists_user_provider() -> None:
    pool = AsyncMock()
    pool.execute = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=_kb_row(provider="cloud"))
    with (
        patch("app.api.knowledge.settings_store.load", AsyncMock(return_value=_settings("cloud"))),
        patch("app.api.knowledge.get_pool", return_value=pool),
    ):
        result = await create_knowledge_base(user_id="u1", tenant_id="t1", name="KB")
    assert result["provider"] == "cloud"
    # 落库的 provider 必须是用户当前设置，而不是请求里可能被篡改的值
    assert "cloud" in pool.execute.await_args.args


# ── 上传：类型在入库前就被拒绝 ──


async def test_upload_rejects_unsupported_type() -> None:
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=_kb_row())
    provider = MagicMock()
    provider.supports = MagicMock(return_value=False)
    with (
        patch("app.api.knowledge.get_pool", return_value=pool),
        patch("app.api.knowledge.settings_store.load", AsyncMock(return_value=_settings())),
        patch(
            "app.api.knowledge.registry.resolve_for_user",
            AsyncMock(return_value=(provider, _settings(), HealthResult(True, STATUS_READY))),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await upload_document(kb_id="kb-1", user_id="u1", name="a.docx", content=b"x")
    assert exc.value.status_code == 400


async def test_upload_rejects_oversize_payload() -> None:
    from app.api.knowledge import MAX_UPLOAD_BYTES

    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=_kb_row())
    provider = MagicMock()
    provider.supports = MagicMock(return_value=True)
    with (
        patch("app.api.knowledge.get_pool", return_value=pool),
        patch("app.api.knowledge.settings_store.load", AsyncMock(return_value=_settings())),
        patch(
            "app.api.knowledge.registry.resolve_for_user",
            AsyncMock(return_value=(provider, _settings(), HealthResult(True, STATUS_READY))),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await upload_document(
                kb_id="kb-1",
                user_id="u1",
                name="a.pdf",
                content=b"0" * (MAX_UPLOAD_BYTES + 1),
            )
    assert exc.value.status_code == 413


# ── 索引与查询 gate ──


async def test_build_requires_documents() -> None:
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=_kb_row(document_count=0))
    with patch("app.api.knowledge.get_pool", return_value=pool):
        with pytest.raises(HTTPException) as exc:
            await build_knowledge_base("kb-1", "u1")
    assert exc.value.status_code == 400


async def test_query_rejects_provider_mismatch() -> None:
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=_kb_row(provider="selfhosted"))
    with (
        patch("app.api.knowledge.get_pool", return_value=pool),
        patch("app.api.knowledge.settings_store.load", AsyncMock(return_value=_settings("cloud"))),
    ):
        with pytest.raises(HTTPException) as exc:
            await query_knowledge_base(kb_id="kb-1", user_id="u1", query="q")
    assert exc.value.status_code == 409


async def test_query_rejects_empty_question() -> None:
    with pytest.raises(HTTPException) as exc:
        await query_knowledge_base(kb_id="kb-1", user_id="u1", query="   ")
    assert exc.value.status_code == 400


# ── 用户设置 ──


async def test_switch_provider_blocked_when_kbs_exist() -> None:
    pool = AsyncMock()
    pool.fetchval = AsyncMock(return_value=2)
    with patch("app.api.knowledge.get_pool", return_value=pool):
        with pytest.raises(HTTPException) as exc:
            await put_user_settings(
                user_id="u1", tenant_id="t1", provider="cloud", cloud_api_key="k"
            )
    assert exc.value.status_code == 409


async def test_switch_provider_rejects_unknown_name() -> None:
    with pytest.raises(HTTPException) as exc:
        await put_user_settings(
            user_id="u1", tenant_id="t1", provider="milvus", cloud_api_key="k"
        )
    assert exc.value.status_code == 400
