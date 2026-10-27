"""本地知识库最小闭环（不碰真实 PG / 不起子进程）。

覆盖四层：

- ``chunk`` / ``search`` / ``store`` 的纯逻辑（分块、打分、路径与 LIKE 转义）
- ``pageindex`` 的组件探测（真造目录，不做任何 install/spawn）
- HTTP 层：默认禁用 → 403；开启但没 PG → 503；用内存 ``FakeStore`` 走完整的
  建库 / 上传 / 检索 / 删除 / build 流程

**真实 asyncpg 连接与真实 PageIndex 构建都不在测试范围内**，这里不假装验证过。
"""

from __future__ import annotations

import base64
import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.api import knowledge as knowledge_api
from app.knowledge import ENV_FLAG, PgKnowledgeStore, StoreUnavailable, is_enabled
from app.knowledge.chunk import DEFAULT_CHUNK_SIZE, DEFAULT_OVERLAP, chunk_text, normalize_text
from app.knowledge.pageindex import find_pageindex_dir, pageindex_status
from app.knowledge.search import build_answer, clip, keywords, rank_chunks, search_payload
from app.knowledge.store import document_path, like_pattern, new_id, safe_segment, search_sql
from app.main import create_app
from fastapi.testclient import TestClient

from tests.conftest import make_settings

# --------------------------------------------------------------------- 夹具


class FakeStore:
    """内存版存储：接口与 ``PgKnowledgeStore`` 一致，用来测 HTTP 层而不碰 PG。"""

    def __init__(self) -> None:
        self.kbs: dict[str, dict[str, Any]] = {}
        self.docs: dict[str, dict[str, Any]] = {}
        self.chunks: dict[str, dict[str, Any]] = {}

    async def create_kb(self, name: str, description: str = "") -> dict[str, Any]:
        kb = {
            "id": new_id("kb"),
            "name": name,
            "description": description,
            "created_at": "2024-01-01T00:00:00+00:00",
        }
        self.kbs[kb["id"]] = kb
        return dict(kb)

    async def list_kbs(self) -> list[dict[str, Any]]:
        return [dict(kb) for kb in self.kbs.values()]

    async def get_kb(self, kb_id: str) -> dict[str, Any] | None:
        kb = self.kbs.get(kb_id)
        return dict(kb) if kb is not None else None

    async def delete_kb(self, kb_id: str) -> bool:
        existed = self.kbs.pop(kb_id, None) is not None
        for doc_id in [key for key, doc in self.docs.items() if doc["kb_id"] == kb_id]:
            self.docs.pop(doc_id, None)
        self.chunks = {key: row for key, row in self.chunks.items() if row["kb_id"] != kb_id}
        return existed

    async def add_document(
        self,
        *,
        kb_id: str,
        name: str,
        data: bytes,
        mime_type: str,
        chunks: Any,
    ) -> dict[str, Any]:
        doc_id = new_id("doc")
        bodies = list(chunks)
        doc = {
            "doc_id": doc_id,
            "kb_id": kb_id,
            "name": name,
            "path": f"{kb_id}/{doc_id}.txt",
            "size_bytes": len(data),
            "mime_type": mime_type,
            "chunks": len(bodies),
            "created_at": "2024-01-01T00:00:00+00:00",
        }
        self.docs[doc_id] = doc
        for seq, body in enumerate(bodies):
            chunk_id = new_id("chk")
            self.chunks[chunk_id] = {
                "id": chunk_id,
                "kb_id": kb_id,
                "doc_id": doc_id,
                "seq": seq,
                "content": body,
            }
        return dict(doc)

    async def search(self, *, kb_id: str, terms: Any, limit: int = 200) -> list[dict[str, Any]]:
        hit_terms = [term.lower() for term in terms if term]
        rows = [
            row
            for row in self.chunks.values()
            if row["kb_id"] == kb_id and any(term in row["content"].lower() for term in hit_terms)
        ]
        return [
            {"id": row["id"], "doc_id": row["doc_id"], "seq": row["seq"], "content": row["content"]}
            for row in rows[:limit]
        ]


@contextlib.contextmanager
def build_client(data_dir: Path, store: Any = None) -> Iterator[TestClient]:
    """临时 app：``create_app`` + 手动 ``include_router``（本批不改 api/__init__.py）。"""
    app = create_app(make_settings(data_dir.parent, data_dir=data_dir))
    app.include_router(knowledge_api.router)
    if store is not None:
        app.state.knowledge_store = store
    with TestClient(app) as client:
        yield client


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")


@pytest.fixture
def disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)


# --------------------------------------------------------------------- 分块


def test_normalize_text_unifies_newlines_and_drops_nul() -> None:
    assert normalize_text("a\r\nb\rc\x00  ") == "a\nb\nc"


def test_chunk_text_empty_input_has_no_chunks() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\t ") == []


def test_chunk_text_short_document_is_single_chunk() -> None:
    assert chunk_text("x" * 120) == ["x" * 120]


def test_chunk_text_uses_800_with_100_overlap() -> None:
    assert (DEFAULT_CHUNK_SIZE, DEFAULT_OVERLAP) == (800, 100)
    body = "".join(str(index % 10) for index in range(2000))
    chunks = chunk_text(body)
    assert len(chunks) == 3
    assert all(len(chunk) <= DEFAULT_CHUNK_SIZE for chunk in chunks)
    # 重叠 100 字：下一片开头正好是上一片结尾
    assert chunks[0][-DEFAULT_OVERLAP :] == chunks[1][:DEFAULT_OVERLAP]
    assert chunks[1][-DEFAULT_OVERLAP :] == chunks[2][:DEFAULT_OVERLAP]
    assert chunks[0] + chunks[1][DEFAULT_OVERLAP :] == body[:1500]


def test_chunk_text_rejects_impossible_overlap() -> None:
    with pytest.raises(ValueError):
        chunk_text("abc", size=10, overlap=10)
    with pytest.raises(ValueError):
        chunk_text("abc", size=0)


# --------------------------------------------------------------------- 检索


def test_keywords_expands_chinese_bigrams_and_lowercases_ascii() -> None:
    terms = keywords("Chiron 知识库怎么建？")
    assert terms[0] == "chiron"
    assert "知识" in terms and "怎么" in terms
    assert all(term == term.lower() for term in terms)


def test_keywords_dedupes_and_caps() -> None:
    terms = keywords("检索 检索 检索 检索 检索 检索 检索 检索 检索 检索")
    assert terms == ["检索"]

    many = keywords("一 二 三 四 五 六 七 八 九 十", max_terms=3)
    assert len(many) == 3


def test_rank_chunks_sorts_by_hit_count_and_drops_misses() -> None:
    rows = [
        {"id": "chk_1", "doc_id": "doc_1", "seq": 0, "content": "本地知识库支持关键词检索。"},
        {"id": "chk_2", "doc_id": "doc_2", "seq": 0, "content": "检索检索：重复出现的词得分更高。"},
        {"id": "chk_3", "doc_id": "doc_3", "seq": 0, "content": "完全无关的内容。"},
    ]
    hits = rank_chunks(rows, ["检索"], top_k=5)
    assert [hit["chunk_id"] for hit in hits] == ["chk_2", "chk_1"]
    assert hits[0]["score"] == 2


def test_search_payload_shape_and_citation_fields() -> None:
    rows = [{"id": "chk_9", "doc_id": "doc_9", "seq": 3, "content": "  分块  重叠 100 字。\n"}]
    payload = search_payload(rows, "分块", top_k=3)
    assert set(payload) == {"answer", "citations"}
    assert payload["citations"] == [
        {"doc_id": "doc_9", "chunk_id": "chk_9", "snippet": "分块 重叠 100 字。"}
    ]
    assert "分块 重叠 100 字。" in payload["answer"]


def test_search_payload_without_hits_says_so() -> None:
    payload = search_payload([], "不存在", top_k=3)
    assert payload["citations"] == []
    assert payload["answer"] == build_answer([])


def test_clip_collapses_whitespace_and_truncates() -> None:
    assert clip("a\n\nb   c", limit=20) == "a b c"
    clipped = clip("字" * 300, limit=10)
    assert len(clipped) == 10
    assert clipped.endswith("…")


# --------------------------------------------------------------------- 存储纯逻辑


def test_document_path_is_confined_under_knowledge_root(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    path = document_path(root, "kb_0123456789abcdef", "doc_0123456789abcdef", "../../etc/passwd.md")
    assert path == root / "kb_0123456789abcdef" / "doc_0123456789abcdef.md"
    assert root in path.parents


def test_safe_segment_rejects_traversal() -> None:
    assert safe_segment("kb_0123456789abcdef") == "kb_0123456789abcdef"
    for bad in ("../evil", "a/b", "a\\b", "", "x" * 65):
        with pytest.raises(ValueError):
            safe_segment(bad)


def test_like_pattern_escapes_wildcards() -> None:
    assert like_pattern("100%_x") == "%100\\%\\_x%"
    assert like_pattern("普通词") == "%普通词%"


async def test_pg_store_without_dsn_reports_unavailable(tmp_path: Path) -> None:
    store = PgKnowledgeStore(dsn="", root=tmp_path)
    with pytest.raises(StoreUnavailable):
        async with store.connect():
            pass


def test_search_sql_aligns_placeholders_with_params() -> None:
    sql, params = search_sql(["分块", "abc"], 50)
    assert sql.count("ILIKE") == 2
    # 两个关键词 + LIMIT；kb_id 走 $1 由调用方单独传
    assert params == ["%分块%", "%abc%", 50]
    assert "$3" in sql and "$4" in sql
    assert "ESCAPE E'\\\\'" in sql, "E-string 才不依赖 standard_conforming_strings"


def test_search_sql_without_terms_scans_the_kb() -> None:
    sql, params = search_sql([], 7)
    assert "ILIKE" not in sql
    assert "$2" in sql
    assert params == [7]


# --------------------------------------------------------------------- pageindex


def test_pageindex_missing(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    assert find_pageindex_dir(settings) is None
    status = pageindex_status(settings)
    assert status["installed"] is False
    assert status["path"] == ""
    assert status["candidates"]  # 报错时要把"该放哪"告诉用户


def test_pageindex_found_in_userdata_components(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)  # data_dir = <tmp>/data
    target = settings.data_dir.parent / "components" / "pageindex"
    target.mkdir(parents=True)
    (target / "manifest.json").write_text("{}", encoding="utf-8")

    assert find_pageindex_dir(settings) == target
    status = pageindex_status(settings)
    assert status["installed"] is True
    assert status["manifest"] is True


# --------------------------------------------------------------------- HTTP 层


def test_knowledge_disabled_by_default(tmp_path: Path, disabled: None) -> None:
    with build_client(data_dir=tmp_path / "data") as client:
        for response in (
            client.get("/v1/kb"),
            client.post("/v1/kb", json={"name": "笔记"}),
            client.post("/v1/kb/kb_0123456789abcdef/build"),
        ):
            assert response.status_code == 403
            assert response.json()["error"] == "knowledge_disabled"


def test_is_enabled_flag_comes_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.delenv(ENV_FLAG, raising=False)
    assert is_enabled(settings) is False
    monkeypatch.setenv(ENV_FLAG, "true")
    assert is_enabled(settings) is True
    monkeypatch.setenv(ENV_FLAG, "0")
    assert is_enabled(settings) is False


def test_enabled_without_pg_reports_storage_unavailable(tmp_path: Path, enabled: None) -> None:
    with build_client(data_dir=tmp_path / "data") as client:
        response = client.get("/v1/kb")
    assert response.status_code == 503
    assert response.json()["error"] == "storage_unavailable"


def test_kb_crud_round_trip_with_injected_store(tmp_path: Path, enabled: None) -> None:
    store = FakeStore()
    with build_client(store=store, data_dir=tmp_path / "data") as client:
        created = client.post("/v1/kb", json={"name": "笔记", "description": "本地"})
        assert created.status_code == 201
        kb = created.json()["data"]
        assert kb["name"] == "笔记" and kb["id"].startswith("kb_")

        listed = client.get("/v1/kb")
        assert [item["id"] for item in listed.json()["data"]] == [kb["id"]]

        deleted = client.delete(f"/v1/kb/{kb['id']}")
        assert deleted.status_code == 200
        assert deleted.json()["data"] == {"id": kb["id"], "deleted": True}

        assert client.delete(f"/v1/kb/{kb['id']}").json()["error"] == "kb_not_found"


def test_upload_then_query_returns_citations(tmp_path: Path, enabled: None) -> None:
    store = FakeStore()
    body = "Chiron 的本地知识库把文档切成 800 字的分块，重叠 100 字。" * 40
    encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")

    with build_client(store=store, data_dir=tmp_path / "data") as client:
        kb_id = client.post("/v1/kb", json={"name": "笔记"}).json()["data"]["id"]

        uploaded = client.post(
            f"/v1/kb/{kb_id}/documents",
            json={"name": "notes.md", "content_base64": encoded},
        )
        assert uploaded.status_code == 201
        doc = uploaded.json()["data"]
        assert doc["name"] == "notes.md"
        assert doc["chunks"] == len(chunk_text(body))

        queried = client.post(f"/v1/kb/{kb_id}/query", json={"query": "分块", "top_k": 3})
        assert queried.status_code == 200
        payload = queried.json()
        assert payload["answer"] == payload["data"]["answer"]
        assert payload["citations"], "命中的片段必须出现"
        for citation in payload["citations"]:
            assert set(citation) == {"doc_id", "chunk_id", "snippet"}
            assert citation["doc_id"] == doc["doc_id"]
            assert "分块" in citation["snippet"]
        assert len(payload["citations"]) <= 3


def test_upload_rejects_bad_payloads(tmp_path: Path, enabled: None) -> None:
    store = FakeStore()
    with build_client(store=store, data_dir=tmp_path / "data") as client:
        kb_id = client.post("/v1/kb", json={"name": "笔记"}).json()["data"]["id"]

        bad_base64 = client.post(
            f"/v1/kb/{kb_id}/documents",
            json={"name": "a.txt", "content_base64": "not base64!!"},
        )
        assert bad_base64.status_code == 400
        assert bad_base64.json()["error"] == "invalid_base64"

        bad_name = client.post(
            f"/v1/kb/{kb_id}/documents",
            json={"name": "../etc/passwd", "content_base64": "aGk="},
        )
        assert bad_name.status_code == 400
        assert bad_name.json()["error"] == "invalid_document_name"

        missing_kb = client.post(
            "/v1/kb/kb_ffffffffffffffff/documents",
            json={"name": "a.txt", "content_base64": "aGk="},
        )
        assert missing_kb.status_code == 404
        assert missing_kb.json()["error"] == "kb_not_found"


def test_query_on_missing_kb_is_404(tmp_path: Path, enabled: None) -> None:
    with build_client(store=FakeStore(), data_dir=tmp_path / "data") as client:
        response = client.post("/v1/kb/kb_ffffffffffffffff/query", json={"query": "任何"})
    assert response.status_code == 404
    assert response.json()["error"] == "kb_not_found"


def test_build_without_component_is_409(tmp_path: Path, enabled: None) -> None:
    with build_client(store=FakeStore(), data_dir=tmp_path / "data") as client:
        kb_id = client.post("/v1/kb", json={"name": "笔记"}).json()["data"]["id"]
        response = client.post(f"/v1/kb/{kb_id}/build")

    assert response.status_code == 409
    payload = response.json()
    assert payload["error"] == "pageindex_not_installed"
    assert payload["candidates"], "409 要带出候选目录，否则用户不知道该装到哪"


def test_build_with_component_is_202_placeholder(tmp_path: Path, enabled: None) -> None:
    data_dir = tmp_path / "data"
    component = data_dir / "components" / "pageindex"
    component.mkdir(parents=True)

    with build_client(store=FakeStore(), data_dir=data_dir) as client:
        kb_id = client.post("/v1/kb", json={"name": "笔记"}).json()["data"]["id"]
        response = client.post(f"/v1/kb/{kb_id}/build")

    assert response.status_code == 202
    data = response.json()["data"]
    assert data["status"] == "queued"
    assert data["kb_id"] == kb_id
    assert data["component"] == str(component)
    assert data["job_id"].startswith("job_")
