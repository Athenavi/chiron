"""本地知识库端到端（``/v1/kb*``），跑在**真实 PostgreSQL** 上。

覆盖契约 §4 的扩展端点：建库 / 列库 / 删库、上传文档（原文落盘 + 分块入库）、关键词
检索（``{answer, citations}``）、PageIndex 构建占位。

与 ``tests/test_knowledge.py`` 的分工：那份用内存 ``FakeStore`` 只测 HTTP 形状；
这份用 ``PgKnowledgeStore`` + 真实 ``local_kb`` / ``local_kb_docs`` / ``local_kb_chunks``
三张表 + 真实文件系统，``chunks`` 计数与 ``citations`` 都从库里读回来核对。

开关（``CHIRON_LOCAL_KNOWLEDGE_ENABLED``）默认关闭，端点必须明确回 403 而不是空列表。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from app.main import create_app
from app.storage.pg import run_sql
from fastapi.testclient import TestClient

from .conftest import KNOWLEDGE_ENV_FLAG

#: 关键词 ``分块策略`` 在第 1 块里；总长度 > 800 字，保证不止一块
DOC_TEXT = "Chiron 本地知识库的分块策略：按字符切，默认 800 字一片，相邻片重叠 100 字。\n" + "".join(
    f"第 {index} 段：这是用于验证分块与检索的填充文本，不含别的关键词。\n" for index in range(60)
)

#: 第二篇文档：内容与关键词无关，用来验证 citations 只来自命中的那一篇
OTHER_TEXT = "这篇文档讲的是媒体库与记忆层。\n" + "".join(
    f"第 {index} 行：媒体上传走 JSON base64，记忆层按标签检索。\n" for index in range(20)
)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _create_kb(client: TestClient, name: str = "本地手册", description: str = "集成测试") -> dict:
    response = client.post("/v1/kb", json={"name": name, "description": description})
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"data"}
    return body["data"]


def _upload(client: TestClient, kb_id: str, name: str, text: str, mime: str = "text/plain") -> dict:
    response = client.post(
        f"/v1/kb/{kb_id}/documents",
        json={"name": name, "content_base64": _b64(text.encode("utf-8")), "mime_type": mime},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


# --------------------------------------------------------------------- 开关
def test_knowledge_is_disabled_by_default(app_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """默认关闭：连 PG 都不碰，直接 403 + 机器可读码（不是静默 200 空列表）。"""
    monkeypatch.delenv(KNOWLEDGE_ENV_FLAG, raising=False)
    response = app_client.get("/v1/kb")
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"] == "knowledge_disabled"
    assert KNOWLEDGE_ENV_FLAG in body["message"]


# --------------------------------------------------------------------- 建库 / 列库 / 删库
def test_create_list_delete_kb(knowledge_client: TestClient, knowledge_settings, pg_dsn: str) -> None:
    kb = _create_kb(knowledge_client, name="手册 A")
    kb_id = kb["id"]
    assert kb_id.startswith("kb_")
    assert kb["name"] == "手册 A"
    assert kb["description"] == "集成测试"
    assert kb["created_at"]

    # 原文目录随建库创建（契约 §4：数据目录就是用户可见的"我的文件"）
    root = Path(knowledge_settings.data_dir) / "knowledge"
    assert (root / kb_id).is_dir()

    # 真实表里有这一行
    rows = run_sql(pg_dsn, "SELECT name, description FROM local_kb WHERE id = %s", (kb_id,))
    assert rows == [("手册 A", "集成测试")]

    listed = knowledge_client.get("/v1/kb").json()["data"]
    assert any(item["id"] == kb_id for item in listed)
    assert set(listed[0]) == {"id", "name", "description", "created_at"}

    removed = knowledge_client.delete(f"/v1/kb/{kb_id}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["data"] == {"id": kb_id, "deleted": True}

    assert not (root / kb_id).exists()
    assert run_sql(pg_dsn, "SELECT count(*) FROM local_kb WHERE id = %s", (kb_id,))[0][0] == 0
    assert all(item["id"] != kb_id for item in knowledge_client.get("/v1/kb").json()["data"])

    again = knowledge_client.delete(f"/v1/kb/{kb_id}")
    assert again.status_code == 404
    assert again.json()["error"] == "kb_not_found"


def test_kb_id_and_name_validation(knowledge_client: TestClient) -> None:
    """非法 id / 空名字走明确错误码，而不是 500。"""
    bad_id = knowledge_client.delete("/v1/kb/bad!id")
    assert bad_id.status_code == 400
    assert bad_id.json()["error"] == "invalid_kb_id"

    blank = knowledge_client.post("/v1/kb", json={"name": "   "})
    assert blank.status_code == 400
    assert blank.json()["error"] == "invalid_name"

    too_long = knowledge_client.post("/v1/kb", json={"name": "x" * 121})
    assert too_long.status_code == 422


# --------------------------------------------------------------------- 上传 + 检索
def test_document_upload_then_query(knowledge_client: TestClient, knowledge_settings, pg_dsn: str) -> None:
    """上传两篇文档 → 分块入库 → 检索只命中相关那一篇，并给出 ``{answer, citations}``。"""
    kb_id = _create_kb(knowledge_client)["id"]

    doc = _upload(knowledge_client, kb_id, "分块说明.md", DOC_TEXT, mime="text/markdown")
    other = _upload(knowledge_client, kb_id, "媒体说明.txt", OTHER_TEXT)

    assert doc["kb_id"] == kb_id
    assert doc["doc_id"].startswith("doc_")
    assert doc["name"] == "分块说明.md"
    assert doc["mime_type"] == "text/markdown"
    assert doc["size_bytes"] == len(DOC_TEXT.encode("utf-8"))
    assert doc["chunks"] >= 2, "1600+ 字的文档按 800 字切应该不止一块"
    assert doc["path"] and not Path(doc["path"]).is_absolute(), "响应里不能出现绝对路径"

    # 原文真的落盘（相对 <data-dir>/knowledge/<kb_id>/<doc_id>.md）
    root = Path(knowledge_settings.data_dir) / "knowledge"
    stored = root / doc["path"]
    assert stored.is_file()
    assert stored.read_text(encoding="utf-8") == DOC_TEXT
    assert (root / other["path"]).is_file()

    # 分块真的进了 local_kb_chunks，且顺序从 0 开始
    count = run_sql(pg_dsn, "SELECT count(*) FROM local_kb_chunks WHERE kb_id = %s", (kb_id,))[0][0]
    assert count == doc["chunks"] + other["chunks"]
    seqs = [
        row[0]
        for row in run_sql(pg_dsn, "SELECT seq FROM local_kb_chunks WHERE doc_id = %s ORDER BY seq", (doc["doc_id"],))
    ]
    assert seqs == list(range(doc["chunks"]))

    # 检索
    response = knowledge_client.post(f"/v1/kb/{kb_id}/query", json={"query": "分块策略", "top_k": 3})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"data", "answer", "citations"}
    assert body["data"]["answer"] == body["answer"]
    assert body["data"]["citations"] == body["citations"]

    citations = body["citations"]
    assert citations, "关键词 分块策略 明明在文档里"
    assert all(set(item) == {"doc_id", "chunk_id", "snippet"} for item in citations)
    assert citations[0]["doc_id"] == doc["doc_id"]
    assert "分块策略" in citations[0]["snippet"]
    assert "分块策略" in body["answer"]
    assert all(item["doc_id"] != other["doc_id"] for item in citations[:1])

    # 无命中：明确"没有检索到"，不是编内容
    nothing = knowledge_client.post(f"/v1/kb/{kb_id}/query", json={"query": "zzzz", "top_k": 3})
    assert nothing.status_code == 200
    assert nothing.json()["citations"] == []
    assert "没有检索到" in nothing.json()["answer"]

    # 关键词为空（全是标点）→ 400
    empty = knowledge_client.post(f"/v1/kb/{kb_id}/query", json={"query": "!!!", "top_k": 3})
    assert empty.status_code == 400
    assert empty.json()["error"] == "empty_query"

    # 删库连带清掉行、分块与原文目录
    assert knowledge_client.delete(f"/v1/kb/{kb_id}").status_code == 200
    assert run_sql(pg_dsn, "SELECT count(*) FROM local_kb_chunks WHERE kb_id = %s", (kb_id,))[0][0] == 0
    assert run_sql(pg_dsn, "SELECT count(*) FROM local_kb_docs WHERE kb_id = %s", (kb_id,))[0][0] == 0
    assert not (root / kb_id).exists()
    assert knowledge_client.post(f"/v1/kb/{kb_id}/query", json={"query": "分块"}).status_code == 404


def test_document_validation(knowledge_client: TestClient) -> None:
    """文件名 / base64 / 空内容各自给明确错误码。"""
    kb_id = _create_kb(knowledge_client, name="校验库")["id"]
    try:
        for bad_name in ("../evil.md", "a/b.md", "..", "with\x00nul.txt"):
            response = knowledge_client.post(
                f"/v1/kb/{kb_id}/documents",
                json={"name": bad_name, "content_base64": _b64(b"hello")},
            )
            assert response.status_code == 400, (bad_name, response.text)
            assert response.json()["error"] == "invalid_document_name"

        bad_base64 = knowledge_client.post(
            f"/v1/kb/{kb_id}/documents",
            json={"name": "ok.txt", "content_base64": "这不是 base64 !!!"},
        )
        assert bad_base64.status_code == 400
        assert bad_base64.json()["error"] == "invalid_base64"

        # 只有 NUL：解码后去控制字符 → 没有可索引文本
        empty_doc = knowledge_client.post(
            f"/v1/kb/{kb_id}/documents",
            json={"name": "nul.txt", "content_base64": _b64(b"\x00\x00")},
        )
        assert empty_doc.status_code == 400
        assert empty_doc.json()["error"] == "empty_document"

        missing = knowledge_client.post(
            "/v1/kb/kb_0000000000000000/documents",
            json={"name": "ok.txt", "content_base64": _b64(b"hello")},
        )
        assert missing.status_code == 404
        assert missing.json()["error"] == "kb_not_found"
    finally:
        knowledge_client.delete(f"/v1/kb/{kb_id}")


def test_build_reports_real_pageindex_state(knowledge_client: TestClient) -> None:
    """``/build`` 只回答"组件装了没有"，不假装在构建：没装 409，装了 202 占位。"""
    kb_id = _create_kb(knowledge_client, name="构建库")["id"]
    try:
        response = knowledge_client.post(f"/v1/kb/{kb_id}/build")
        assert response.status_code in {202, 409}, response.text
        body = response.json()
        if response.status_code == 409:
            assert body["error"] == "pageindex_not_installed"
            assert isinstance(body["candidates"], list) and body["candidates"]
        else:
            assert body["data"]["status"] == "queued"
            assert body["data"]["kb_id"] == kb_id
            assert body["data"]["job_id"].startswith("job_")
    finally:
        knowledge_client.delete(f"/v1/kb/{kb_id}")


def test_knowledge_needs_postgres(tmp_path: Path, unreachable_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """开关开了但 PG 连不上 → 503 ``storage_unavailable``（不是 200 空列表）。"""
    monkeypatch.setenv(KNOWLEDGE_ENV_FLAG, "1")
    from .conftest import make_settings  # 局部导入：只在需要构造"坏 DSN"的用例里用

    settings = make_settings(tmp_path / "no-pg", postgres_dsn=unreachable_dsn)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.get("/v1/kb")
    assert response.status_code == 503, response.text
    assert response.json()["error"] == "storage_unavailable"
