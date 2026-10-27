"""本地媒体库 + 记忆层端到端，跑在**真实 PostgreSQL + 真实文件系统**上。

覆盖契约 §4 的两块最小闭环的**读路径**与检索：

- ``/v1/media``：列表 / 详情 / 原始字节 / 删除，分页顺序来自真实 PG 的
  ``ORDER BY created_at DESC, id DESC``；文件名穿越清洗与路径越界靠真实文件系统验证。
- ``/v1/memory``：列表 / ILIKE 检索（含 ``%`` / ``_`` 元字符转义）/ 删除；标签归一化后
  真的以逗号串形式落库。

**两个写入端点现在会 500**（报告里的 app bug #4）：``app/storage/files.py::insert_media``
与 ``app/storage/memory_store.py::insert_entry`` 都拿 "含 ``created_at`` 的列清单"
（7 列）去配 6 个占位符，真实 PG 直接报
``PostgresSyntaxError: INSERT 的指定字段数多于表达式``。因此这里用
``python/psycopg2`` 直插行 + 真实模块函数落盘，再走 HTTP 验证读路径；两个写入端点各留一个
``xfail`` 用例钉住现状（修好后自动 XPASS）。

``tests/test_media_memory.py`` 用 ``FakeConnection`` 按语句形状匹配，所以看不到这个 bug ——
这也是这几份集成测试存在的理由。
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from pathlib import Path

import pytest
from app.storage import files, memory_store
from app.storage.pg import run_sql
from fastapi.testclient import TestClient

from .conftest import PNG_BYTES

#: 建表（幂等）由 app 自己做：先打一次 GET，让 ``_connection`` 里的 ``ensure_*_schema`` 跑一遍
MEDIA_INSERT_SQL = (
    "INSERT INTO local_media (id, name, safe_name, mime_type, size_bytes, sha256, created_at)"
    " VALUES (%s, %s, %s, %s, %s, %s, now() - make_interval(secs => %s))"
)
MEMORY_INSERT_SQL = (
    "INSERT INTO local_memory (id, kind, title, content, tags, source, created_at)"
    " VALUES (%s, %s, %s, %s, %s, %s, now() - make_interval(secs => %s))"
)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _ensure_schemas(client: TestClient) -> None:
    """打两次列表请求，让 app 在真实 PG 上建好 ``local_media`` / ``local_memory``。"""
    assert client.get("/v1/media").status_code == 200
    assert client.get("/v1/memory/entries").status_code == 200


def _insert_media(
    pg_dsn: str,
    settings,
    *,
    name: str = "note.png",
    mime: str = "image/png",
    data: bytes = PNG_BYTES,
    age_seconds: float = 0.0,
) -> str:
    """直插一行媒体（落盘用真实 ``files.write_media_bytes``，入库用真实表）。"""
    media_id = files.new_media_id()
    safe = files.safe_name(name)
    files.write_media_bytes(settings, media_id, safe, data)
    run_sql(
        pg_dsn,
        MEDIA_INSERT_SQL,
        (
            media_id,
            name,
            safe,
            files.normalize_mime_type(mime),
            len(data),
            files.sha256_of(data),
            float(age_seconds),
        ),
    )
    return media_id


def _insert_memory(
    pg_dsn: str,
    *,
    content: str,
    title: str = "",
    kind: str = "note",
    tags: tuple[str, ...] = (),
    source: str = "",
    age_seconds: float = 0.0,
) -> str:
    """直插一条记忆（标签走真实 ``normalize_tags`` 归一化）。"""
    entry_id = uuid.uuid4().hex
    run_sql(
        pg_dsn,
        MEMORY_INSERT_SQL,
        (entry_id, kind, title, content, memory_store.normalize_tags(list(tags)), source, float(age_seconds)),
    )
    return entry_id


def _cleanup_media(client: TestClient, *media_ids: str) -> None:
    for media_id in media_ids:
        client.delete(f"/v1/media/{media_id}")


# --------------------------------------------------------------------- 媒体：读路径
def test_media_read_paths_on_real_pg(app_client: TestClient, api_settings, pg_dsn: str) -> None:
    """直插一行后：列表 / 详情 / 原始字节 / 删除四条读路径都通，元数据与文件一致。"""
    _ensure_schemas(app_client)
    media_id = _insert_media(pg_dsn, api_settings)

    rows = run_sql(pg_dsn, "SELECT name, size_bytes, sha256 FROM local_media WHERE id = %s", (media_id,))
    assert rows == [("note.png", len(PNG_BYTES), hashlib.sha256(PNG_BYTES).hexdigest())]

    listing = app_client.get("/v1/media", params={"limit": 200}).json()["data"]
    item = next(entry for entry in listing if entry["id"] == media_id)
    assert item["sha256"] == hashlib.sha256(PNG_BYTES).hexdigest()
    assert item["content_url"] == f"/v1/media/{media_id}/content"
    assert "safe_name" not in item, "响应不能泄露落盘用的文件名"
    assert "content_base64" not in item, "列表不带正文"

    detail = app_client.get(f"/v1/media/{media_id}").json()["data"]
    assert base64.b64decode(detail["content_base64"]) == PNG_BYTES
    metadata_only = app_client.get(f"/v1/media/{media_id}", params={"include_content": False}).json()["data"]
    assert metadata_only["content_base64"] is None
    assert metadata_only["name"] == "note.png"

    raw = app_client.get(f"/v1/media/{media_id}/content")
    assert raw.status_code == 200
    assert raw.content == PNG_BYTES
    assert raw.headers["content-type"] == "image/png"
    assert "filename*=UTF-8''note.png" in raw.headers["content-disposition"]

    stored = Path(api_settings.data_dir) / "media" / media_id / "note.png"
    assert stored.read_bytes() == PNG_BYTES

    removed = app_client.delete(f"/v1/media/{media_id}")
    assert removed.status_code == 200
    assert removed.json()["data"] == {"id": media_id, "deleted": True}
    assert not stored.parent.exists(), "删记录时目录也要删掉"
    assert run_sql(pg_dsn, "SELECT count(*) FROM local_media WHERE id = %s", (media_id,))[0][0] == 0
    assert app_client.get(f"/v1/media/{media_id}").status_code == 404
    assert app_client.delete(f"/v1/media/{media_id}").status_code == 404


def test_media_pagination_uses_real_sql_order(app_client: TestClient, api_settings, pg_dsn: str) -> None:
    """分页顺序由真实 PG 决定：``offset`` 窗口 = 全量列表的对应切片。"""
    _ensure_schemas(app_client)
    newest = _insert_media(pg_dsn, api_settings, name="newest.txt", mime="text/plain", data=b"ccc", age_seconds=0)
    middle = _insert_media(pg_dsn, api_settings, name="middle.txt", mime="text/plain", data=b"bbb", age_seconds=10)
    oldest = _insert_media(pg_dsn, api_settings, name="oldest.txt", mime="text/plain", data=b"aaa", age_seconds=20)
    try:
        everything = app_client.get("/v1/media", params={"limit": 200}).json()["data"]
        ids = [item["id"] for item in everything]
        assert ids.index(newest) < ids.index(middle) < ids.index(oldest), "created_at DESC 排序没生效"

        start = ids.index(newest)
        page = app_client.get("/v1/media", params={"limit": 2, "offset": start}).json()["data"]
        assert [item["id"] for item in page] == ids[start : start + 2]

        assert app_client.get("/v1/media", params={"limit": 0}).status_code == 422
        assert app_client.get("/v1/media", params={"offset": -1}).status_code == 422
    finally:
        _cleanup_media(app_client, newest, middle, oldest)


def test_media_name_safety_on_real_filesystem(api_settings) -> None:
    """穿越名只留 basename，且 ``resolve_media_path`` 挡得住越界（真实路径解析）。"""
    media_id = files.new_media_id()
    for raw in ("../../etc/passwd", "C:\\Windows\\system32\\evil.dll", "a:b?.png"):
        cleaned = files.safe_name(raw)
        assert "/" not in cleaned and "\\" not in cleaned
    files.write_media_bytes(api_settings, media_id, files.safe_name("../../etc/passwd"), b"passwd-bytes")
    assert (Path(api_settings.data_dir) / "media" / media_id / "passwd").read_bytes() == b"passwd-bytes"
    assert not (Path(api_settings.data_dir) / "etc").exists()

    with pytest.raises(files.MediaError):
        files.resolve_media_path(api_settings, "../escape", "note.png")
    with pytest.raises(files.MediaError):
        files.resolve_media_path(api_settings, "A" * 32, "note.png")
    files.delete_media_dir(api_settings, media_id)
    assert not (Path(api_settings.data_dir) / "media" / media_id).exists()


def test_media_rejects_oversized_and_invalid_payload(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, pg_dsn: str
) -> None:
    """校验在写盘之前完成：413 / 400 / 422 都不该留下孤儿行或孤儿文件。"""
    _ensure_schemas(app_client)
    monkeypatch.setattr(files, "MAX_MEDIA_BYTES", 4)
    oversized = app_client.post(
        "/v1/media/upload",
        json={"name": "big.bin", "content_base64": _b64(b"0123456789")},
    )
    assert oversized.status_code == 413, oversized.text
    assert "上限" in oversized.json()["detail"]

    bad_base64 = app_client.post("/v1/media/upload", json={"name": "x.png", "content_base64": "不是 base64"})
    assert bad_base64.status_code == 400
    assert app_client.post("/v1/media/upload", json={"name": "x.png"}).status_code == 422

    assert run_sql(pg_dsn, "SELECT count(*) FROM local_media WHERE name IN ('big.bin', 'x.png')")[0][0] == 0


@pytest.mark.xfail(
    reason=(
        "app bug #4：app/storage/files.py::insert_media 用 MEDIA_COLUMNS（7 列，含 created_at）"
        "配 6 个占位符 → 真实 PG 报 PostgresSyntaxError: INSERT 的指定字段数多于表达式"
    ),
    strict=False,
)
def test_media_upload_endpoint_should_persist(app_client: TestClient, api_settings, pg_dsn: str) -> None:
    """期望契约行为：``POST /v1/media/upload`` → 201 + 元数据，且列表里能看到。"""
    _ensure_schemas(app_client)
    response = app_client.post(
        "/v1/media/upload",
        json={
            "name": "截图.png",
            "mime_type": "IMAGE/PNG; charset=binary",
            "content_base64": "data:image/png;base64," + _b64(PNG_BYTES),
        },
    )
    assert response.status_code == 201, f"{response.status_code} {response.text[:300]}"
    data = response.json()["data"]
    assert data["mime_type"] == "image/png"
    assert data["sha256"] == hashlib.sha256(PNG_BYTES).hexdigest()
    assert app_client.get("/v1/media", params={"limit": 200}).json()["data"][0]["id"] == data["id"]
    assert (Path(api_settings.data_dir) / "media" / data["id"] / "截图.png").is_file()
    app_client.delete(f"/v1/media/{data['id']}")


# --------------------------------------------------------------------- 记忆层：读路径
def test_memory_list_search_delete_on_real_pg(app_client: TestClient, pg_dsn: str) -> None:
    """列表（倒序）/ 关键词检索 / 标签命中 / 删除，全部打在真实表上。"""
    _ensure_schemas(app_client)
    first = _insert_memory(
        pg_dsn, content="第一条笔记：关于本地存储", title="笔记", tags=(" 存储 ", "pg", "存储"), age_seconds=20
    )
    second = _insert_memory(pg_dsn, content="第二条：记忆层检索", kind="snippet", source="test", age_seconds=10)
    try:
        # 标签归一化后真的以逗号串落库
        assert run_sql(pg_dsn, "SELECT tags, kind FROM local_memory WHERE id = %s", (first,)) == [("存储,pg", "note")]

        listing = app_client.get("/v1/memory/entries", params={"limit": 200}).json()["data"]
        by_id = {item["id"]: item for item in listing}
        assert by_id[first]["tags"] == ["存储", "pg"]
        assert by_id[first]["source"] == ""
        assert by_id[second]["kind"] == "snippet"
        assert by_id[second]["source"] == "test"
        ids = [item["id"] for item in listing]
        assert ids.index(second) < ids.index(first), "created_at DESC 排序没生效"

        hits = app_client.post("/v1/memory/search", json={"query": "本地存储"}).json()["data"]
        assert [item["id"] for item in hits] == [first]
        by_tag = app_client.post("/v1/memory/search", json={"query": "pg"}).json()["data"]
        assert [item["id"] for item in by_tag] == [first]
        by_title = app_client.post("/v1/memory/search", json={"query": "笔记"}).json()["data"]
        assert [item["id"] for item in by_title] == [first]
        assert app_client.post("/v1/memory/search", json={"query": "无此关键词"}).json()["data"] == []

        removed = app_client.delete(f"/v1/memory/entries/{first}")
        assert removed.status_code == 200
        assert removed.json()["data"] == {"id": first, "deleted": True}
        assert run_sql(pg_dsn, "SELECT count(*) FROM local_memory WHERE id = %s", (first,))[0][0] == 0
        assert app_client.delete(f"/v1/memory/entries/{first}").status_code == 404
    finally:
        for entry_id in (first, second):
            app_client.delete(f"/v1/memory/entries/{entry_id}")


def test_memory_search_escapes_like_metacharacters(app_client: TestClient, pg_dsn: str) -> None:
    """``%`` 与 ``_`` 被转义成字面量：搜 ``100%`` 不会命中 ``1000``。

    这条只有在**真实 PG** 上才有意义（替身实现里没有 LIKE 语义）。
    """
    _ensure_schemas(app_client)
    literal_percent = _insert_memory(pg_dsn, content="折扣 100% 生效", title="促销")
    no_percent = _insert_memory(pg_dsn, content="售价 1000 元", title="标价")
    literal_underscore = _insert_memory(pg_dsn, content="字段名 a_b", title="标识符")
    wildcard_bait = _insert_memory(pg_dsn, content="字段名 axb", title="干扰项")
    try:
        percent_ids = [
            item["id"] for item in app_client.post("/v1/memory/search", json={"query": "100%"}).json()["data"]
        ]
        assert literal_percent in percent_ids
        assert no_percent not in percent_ids, "% 被当成通配符了"

        underscore_ids = [
            item["id"] for item in app_client.post("/v1/memory/search", json={"query": "a_b"}).json()["data"]
        ]
        assert literal_underscore in underscore_ids
        assert wildcard_bait not in underscore_ids, "_ 被当成通配符了"

        plain = app_client.post("/v1/memory/search", json={"query": "1000"}).json()["data"]
        assert [item["id"] for item in plain] == [no_percent]
    finally:
        for entry_id in (literal_percent, no_percent, literal_underscore, wildcard_bait):
            app_client.delete(f"/v1/memory/entries/{entry_id}")


def test_memory_validation(app_client: TestClient) -> None:
    """空白正文 / 空白查询 → 400；超长查询与越界 limit → 422；不存在删除 → 404。"""
    assert app_client.post("/v1/memory/entries", json={"content": "   "}).status_code == 400
    assert app_client.post("/v1/memory/entries", json={}).status_code == 422
    assert app_client.post("/v1/memory/search", json={"query": "   "}).status_code == 400
    assert app_client.post("/v1/memory/search", json={"query": "x" * 500}).status_code == 422
    assert app_client.post("/v1/memory/search", json={"query": "ok", "limit": 0}).status_code == 422
    assert app_client.get("/v1/memory/entries", params={"limit": 0}).status_code == 422
    assert app_client.get("/v1/memory/entries", params={"limit": 201}).status_code == 422
    assert app_client.delete("/v1/memory/entries/does-not-exist").status_code == 404


@pytest.mark.xfail(
    reason=(
        "app bug #4：app/storage/memory_store.py::insert_entry 用 MEMORY_COLUMNS（7 列，含 created_at）"
        "配 6 个占位符 → 真实 PG 报 PostgresSyntaxError: INSERT 的指定字段数多于表达式"
    ),
    strict=False,
)
def test_memory_entry_endpoint_should_persist(app_client: TestClient, pg_dsn: str) -> None:
    """期望契约行为：``POST /v1/memory/entries`` → 201，且能立刻检索到。"""
    _ensure_schemas(app_client)
    response = app_client.post(
        "/v1/memory/entries",
        json={"content": "本地存储笔记", "title": "笔记", "tags": [" 存储 ", "pg", "存储"], "source": "api"},
    )
    assert response.status_code == 201, f"{response.status_code} {response.text[:300]}"
    data = response.json()["data"]
    assert data["tags"] == ["存储", "pg"]
    assert data["kind"] == "note"
    assert run_sql(pg_dsn, "SELECT tags FROM local_memory WHERE id = %s", (data["id"],)) == [("存储,pg",)]
    hits = app_client.post("/v1/memory/search", json={"query": "本地存储"}).json()["data"]
    assert [item["id"] for item in hits] == [data["id"]]
    app_client.delete(f"/v1/memory/entries/{data['id']}")


# --------------------------------------------------------------------- 存储故障路径
def test_chat_reports_503_when_pg_unreachable(down_client: TestClient) -> None:
    """对话 / Agent 链路把"连不上 PG"包成 ``StorageUnavailableError`` → 503，而不是 500。"""
    response = down_client.post("/v1/chat/submit", json={"message": "hi", "model": "mock"})
    assert response.status_code == 503, response.text
    assert "PostgreSQL" in response.json()["detail"]
    assert down_client.get("/v1/agents").status_code == 503
    assert down_client.get("/v1/chat/sessions").status_code == 503


@pytest.mark.xfail(
    reason=(
        "app bug #3：app/storage/files.py::connect 与 app/storage/memory_store.py::connect "
        "直接 await asyncpg.connect(...)，没把连接失败翻成 503 —— "
        "PG 掉线时 /v1/media、/v1/memory 返回 500，而 /v1/chat 同场景是 503"
    ),
    strict=False,
)
def test_media_and_memory_report_503_when_pg_unreachable(down_client: TestClient) -> None:
    """期望三个端点对"库没了"给同一个答案（503）。修好两个 ``connect`` 后本测试会 XPASS。"""
    media = down_client.get("/v1/media")
    memory = down_client.get("/v1/memory/entries")
    assert media.status_code == 503, f"media 实际 {media.status_code}"
    assert memory.status_code == 503, f"memory 实际 {memory.status_code}"


@pytest.mark.xfail(
    reason="app bug #3 的连带效应：PG 掉线时 /v1/media/{id} 与 /content 也是 500 而不是 503",
    strict=False,
)
def test_media_detail_reports_503_when_pg_unreachable(down_client: TestClient) -> None:
    assert down_client.get(f"/v1/media/{'a' * 32}").status_code == 503
    assert down_client.get(f"/v1/media/{'a' * 32}/content").status_code == 503
