"""pageindex-service 验收测试：接口形状、鉴权、越界守卫、索引状态机（不触网、不需凭据）。

SDK 本身由 ``test_sdk_contract.py`` 单独钉住（需要装了 pageindex 的环境）。
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app import search_api
from app.adapter import AnswerResult, OutOfScopeCitations, PageIndexError, SubmitResult, doc_prefix

TOKEN = "t0ken"
HEADERS = {"X-Internal-Token": TOKEN}


class FakeAdapter:
    """替掉真实 SDK：只验证"服务怎么用它"，不验证 SDK 行为。"""

    def __init__(self, *, mode: str, cfg) -> None:
        self.mode = mode

    def health(self):
        return {"ok": True, "status": "ready"}

    def storage_path(self, ctx):
        return f"/root/{ctx.tenant_id}/{ctx.kb_id}"

    def supports(self, filename):
        return filename.endswith(".pdf")

    def ensure_kb_folder(self, ctx):
        return "kb-folder", "user-folder"

    def answer(self, ctx, question):
        if not ctx.doc_ids:
            raise PageIndexError("该知识库还没有已索引的文档")
        if question == "越界":
            raise OutOfScopeCitations("检索结果包含知识库范围外的文档，已拒绝返回")
        return AnswerResult(
            answer="答案",
            citations=[
                {
                    "doc_name": f"{doc_prefix(ctx.doc_ids[0])}-ab_报告.pdf",
                    "page": 3,
                    "block_id": "",
                    "snippet": "片段",
                }
            ],
        )

    def delete_document(self, ctx, pageindex_doc_id):
        return None

    def submit_bytes(self, ctx, *, doc_id, filename, data):
        return SubmitResult(doc_id="pi-1", page_count=7)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN", TOKEN)
    monkeypatch.setenv("KNOWLEDGE_CLOUD_CHAT_MODEL", "openai/gpt-x")
    monkeypatch.setattr(search_api, "_cfg", None)
    monkeypatch.setattr(search_api, "Adapter", FakeAdapter)
    return TestClient(search_api.app)


# ── 服务接口 ──


def test_healthz_is_public_and_reports_engine(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "pageindex-search"
    assert body["engine"].startswith("pageindex==")


def test_answer_requires_internal_token(client):
    assert client.post("/v1/answer", json={"kb_id": "kb1", "question": "q"}).status_code == 401


def test_answer_rejects_wrong_token(client):
    resp = client.post(
        "/v1/answer", json={"kb_id": "kb1", "question": "q"}, headers={"X-Internal-Token": "nope"}
    )
    assert resp.status_code == 401


def test_answer_without_indexed_docs_is_rejected(client):
    """没有已索引文档 → 502，**绝不**放宽到整个账号/索引目录。"""
    resp = client.post("/v1/answer", json={"kb_id": "kb1", "question": "q"}, headers=HEADERS)
    assert resp.status_code == 502


def test_answer_rejects_out_of_scope_citations(client):
    """越界引用 → 整条拒绝（不是"删掉引用照常返回"）。"""
    resp = client.post(
        "/v1/answer",
        json={"kb_id": "kb1", "question": "越界", "doc_ids": ["doc-1"]},
        headers=HEADERS,
    )
    assert resp.status_code == 502


def test_answer_returns_citations(client):
    resp = client.post(
        "/v1/answer",
        json={"kb_id": "kb1", "question": "q", "doc_ids": ["doc-1"]},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["citations"][0]["page"] == 3


def test_prepare_local_returns_storage_path(client):
    resp = client.post(
        "/v1/prepare",
        json={"kb_id": "kb1", "tenant_id": "t1", "mode": "selfhosted"},
        headers=HEADERS,
    )
    assert resp.json()["index_ref"] == "/root/t1/kb1"


def test_prepare_cloud_returns_both_folders(client):
    resp = client.post(
        "/v1/prepare", json={"kb_id": "kb1", "user_id": "u1", "mode": "cloud"}, headers=HEADERS
    )
    body = resp.json()
    assert body["index_ref"] == "kb-folder"
    assert body["user_folder_id"] == "user-folder"


# ── 索引 worker 的状态机 ──


def _store_for(**overrides):
    store = AsyncMock()
    row = {
        "id": "kb1",
        "tenant_id": "t1",
        "user_id": "u1",
        "provider": "selfhosted",
        "index_ref": "",
        "status": "draft",
        "index_engine": None,
    }
    row.update(overrides)
    store.get_kb.return_value = row
    store.get_user_settings.return_value = None
    store.pending_docs.return_value = [
        {"id": "doc-1", "name": "a.pdf", "file_type": "pdf", "storage_key": "t1/kb1/doc-1/a.pdf"}
    ]
    return store


def _cfg():
    cfg = MagicMock()
    cfg.app_secret = "s3cret"
    cfg.llm_backend.return_value = {}
    return cfg


@pytest.mark.asyncio
async def test_handle_kb_index_marks_ready_and_records_engine(monkeypatch):
    from app import index_worker

    monkeypatch.setattr(index_worker, "Adapter", FakeAdapter)
    store = _store_for()
    reader = MagicMock()
    reader.get.return_value = b"%PDF-1.4"

    out = await index_worker.handle_kb_index(
        cfg=_cfg(), store=store, reader=reader, kb_id="kb1", user_id="u1"
    )

    assert out == {"status": "ready", "indexed": 1, "failed": 0}
    store.set_doc_ready.assert_awaited()
    store.set_kb_index_engine.assert_awaited()  # 记录 SDK 版本，便于升级后判断重建
    store.set_kb_status.assert_awaited_with("kb1", "ready", "")


@pytest.mark.asyncio
async def test_handle_kb_index_is_idempotent_when_ready(monkeypatch):
    from app import index_worker

    monkeypatch.setattr(index_worker, "Adapter", FakeAdapter)
    store = _store_for(status="ready")

    out = await index_worker.handle_kb_index(
        cfg=_cfg(), store=store, reader=MagicMock(), kb_id="kb1", user_id="u1"
    )

    assert out["status"] == "skipped"
    store.pending_docs.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_kb_index_marks_error_on_partial_failure(monkeypatch):
    from app import index_worker

    class FailingAdapter(FakeAdapter):
        def submit_bytes(self, ctx, *, doc_id, filename, data):
            raise PageIndexError("解析失败")

    monkeypatch.setattr(index_worker, "Adapter", FailingAdapter)
    store = _store_for()
    reader = MagicMock()
    reader.get.return_value = b"%PDF-1.4"

    out = await index_worker.handle_kb_index(
        cfg=_cfg(), store=store, reader=reader, kb_id="kb1", user_id="u1"
    )

    assert out["status"] == "error" and out["failed"] == 1
    store.set_doc_error.assert_awaited()
    store.set_kb_index_engine.assert_not_awaited()  # 有失败就不记版本（下次重建）


@pytest.mark.asyncio
async def test_cloud_mode_without_key_marks_error(monkeypatch):
    from app import index_worker

    monkeypatch.setattr(index_worker, "Adapter", FakeAdapter)
    store = _store_for(provider="cloud")
    store.get_user_settings.return_value = {"cloud_api_key_enc": None, "cloud_folder_id": None}

    out = await index_worker.handle_kb_index(
        cfg=_cfg(), store=store, reader=MagicMock(), kb_id="kb1", user_id="u1"
    )

    assert out["status"] == "error"
    assert "Cloud" in out["detail"]
