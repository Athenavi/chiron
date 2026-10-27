"""本地媒体库 + 记忆层的最小闭环（契约 §4）。

本文件**不碰真实 PostgreSQL**：``asyncpg.connect`` 被换成 :class:`FakeConnection`，它只认识
``files.py`` / ``memory_store.py`` 实际发出的那几条语句，遇到不认识的语句直接 ``RuntimeError``
（SQL 一改测试立刻红，而不是静默通过）。真实 PG 上的行为仍由 ``scripts/verify_p0.py`` 与
第二批的集成测试负责 —— 这里不假装验证过。

覆盖点：JSON base64 上传落盘 / 列表 / 详情 / 原始内容 / 删除、文件名穿越清洗、大小上限、
PG 不可用时 503、记忆条目的增列表检索删、标签归一化。
"""

from __future__ import annotations

import base64
import hashlib
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from app.api import media, memory
from app.config import Settings
from app.main import create_app
from app.storage import files, memory_store
from fastapi.testclient import TestClient

FAKE_DSN = "postgresql://fake:fake@127.0.0.1:5432/chiron"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"chiron-local-media"
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _normalize(sql: str) -> str:
    return " ".join(sql.split()).upper()


# --------------------------------------------------------------------- fake asyncpg
class FakeDatabase:
    """内存表 + 递增时间戳（保证 ``ORDER BY created_at DESC`` 稳定可断言）。"""

    def __init__(self) -> None:
        self.media: dict[str, dict[str, Any]] = {}
        self.entries: dict[str, dict[str, Any]] = {}
        self.ddl: list[str] = []
        self.dsns: list[str] = []
        self._tick = 0

    def next_timestamp(self) -> datetime:
        self._tick += 1
        return _EPOCH + timedelta(microseconds=self._tick)

    @staticmethod
    def _ordered(table: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(table.values(), key=lambda row: (row["created_at"], row["id"]), reverse=True)


class FakeConnection:
    """asyncpg 连接的替身：只实现本批用到的 ``execute`` / ``fetchrow`` / ``fetch`` / ``close``。"""

    def __init__(self, db: FakeDatabase) -> None:
        self.db = db
        self.closed = False

    async def execute(self, sql: str, *args: Any) -> str:
        statement = _normalize(sql)
        if statement.startswith("CREATE TABLE IF NOT EXISTS"):
            self.db.ddl.append(statement)
            return "CREATE TABLE"
        raise RuntimeError(f"fake asyncpg 不认识的 execute 语句：{statement[:120]}")

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        statement = _normalize(sql)
        if "INSERT INTO LOCAL_MEDIA" in statement:
            media_id, name, safe_name, mime_type, size_bytes, sha256 = args
            row: dict[str, Any] = {
                "id": media_id,
                "name": name,
                "safe_name": safe_name,
                "mime_type": mime_type,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "created_at": self.db.next_timestamp(),
            }
            self.db.media[media_id] = row
            return dict(row)
        if statement.startswith("SELECT") and "FROM LOCAL_MEDIA" in statement:
            return self.db.media.get(args[0])
        if statement.startswith("DELETE FROM LOCAL_MEDIA"):
            removed = self.db.media.pop(args[0], None)
            return None if removed is None else {"safe_name": removed["safe_name"]}
        if "INSERT INTO LOCAL_MEMORY" in statement:
            entry_id, kind, title, content, tags, source = args
            entry: dict[str, Any] = {
                "id": entry_id,
                "kind": kind,
                "title": title,
                "content": content,
                "tags": tags,
                "source": source,
                "created_at": self.db.next_timestamp(),
            }
            self.db.entries[entry_id] = entry
            return dict(entry)
        if statement.startswith("DELETE FROM LOCAL_MEMORY"):
            removed_entry = self.db.entries.pop(args[0], None)
            return None if removed_entry is None else {"id": args[0]}
        raise RuntimeError(f"fake asyncpg 不认识的 fetchrow 语句：{statement[:120]}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        statement = _normalize(sql)
        if "FROM LOCAL_MEDIA" in statement and "ORDER BY" in statement:
            limit, offset = args
            return [dict(row) for row in self.db._ordered(self.db.media)[offset : offset + limit]]
        if "FROM LOCAL_MEMORY" in statement and "ILIKE" in statement:
            pattern, limit = args
            # 真实 PG 走 ILIKE；这里只做等价的子串包含（测试用例不使用 % / _ 元字符）
            keyword = str(pattern).replace("%", "").lower()
            hits = [
                row
                for row in self.db._ordered(self.db.entries)
                if keyword in str(row["title"]).lower()
                or keyword in str(row["content"]).lower()
                or keyword in str(row["tags"]).lower()
            ]
            return hits[:limit]
        if "FROM LOCAL_MEMORY" in statement and "ORDER BY" in statement:
            limit, offset = args
            return [dict(row) for row in self.db._ordered(self.db.entries)[offset : offset + limit]]
        raise RuntimeError(f"fake asyncpg 不认识的 fetch 语句：{statement[:120]}")

    async def close(self) -> None:
        self.closed = True


class StubPg:
    """路由层只从 ``app.state.pg.dsn`` 取连接串，真实连接由 ``asyncpg.connect`` 的 fake 顶替。"""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn


# --------------------------------------------------------------------- fixtures
@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    db = FakeDatabase()

    async def _connect(*, dsn: str = "", **kwargs: Any) -> FakeConnection:
        db.dsns.append(dsn)
        return FakeConnection(db)

    monkeypatch.setattr(asyncpg, "connect", _connect)
    return db


@pytest.fixture
def api_client(settings: Settings, fake_pg: FakeDatabase) -> Iterator[TestClient]:
    """把 media / memory 两个 router 挂到一个临时 app 上（生产注册由集成方做）。"""
    app = create_app(settings)
    app.include_router(media.router, tags=["media"])
    app.include_router(memory.router, tags=["memory"])
    app.state.pg = StubPg(FAKE_DSN)
    with TestClient(app) as test_client:
        yield test_client


def _upload(
    client: TestClient,
    *,
    name: str = "note.png",
    mime_type: str = "image/png",
    data: bytes = PNG_BYTES,
) -> dict[str, Any]:
    response = client.post(
        "/v1/media/upload",
        json={"name": name, "mime_type": mime_type, "content_base64": _b64(data)},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"data"}
    return body["data"]


# --------------------------------------------------------------------- 媒体：命名与路径
def test_safe_name_keeps_only_a_basename() -> None:
    assert files.safe_name("../../etc/passwd") == "passwd"
    assert files.safe_name("C:\\Windows\\system32\\evil.dll") == "evil.dll"
    assert files.safe_name("a:b?.png") == "a_b_.png"
    with pytest.raises(files.MediaError):
        files.safe_name("..")
    with pytest.raises(files.MediaError):
        files.safe_name("   ")


def test_safe_name_is_idempotent_and_bounded() -> None:
    long_name = "x" * 300 + ".png"
    cleaned = files.safe_name(long_name)
    assert cleaned == files.safe_name(cleaned)
    assert len(cleaned) <= files.MAX_NAME_CHARS
    assert cleaned.endswith(".png")


def test_resolve_media_path_rejects_bad_id(settings: Settings) -> None:
    with pytest.raises(files.MediaError):
        files.resolve_media_path(settings, "../escape", "note.png")
    with pytest.raises(files.MediaError):
        files.resolve_media_path(settings, "A" * 32, "note.png")
    resolved = files.resolve_media_path(settings, "a" * 32, "note.png")
    assert resolved == (settings.data_dir / "media" / ("a" * 32) / "note.png").resolve()


def test_decode_base64_accepts_data_url_and_rejects_garbage() -> None:
    assert files.decode_base64(_b64(PNG_BYTES)) == PNG_BYTES
    assert files.decode_base64("data:image/png;base64," + _b64(PNG_BYTES)) == PNG_BYTES
    assert files.decode_base64("iVBO\nRw0K") == b"\x89PNG\r\n"
    with pytest.raises(files.MediaError):
        files.decode_base64("not base64 !!!")
    with pytest.raises(files.MediaTooLargeError):
        files.decode_base64(_b64(b"0123456789"), max_bytes=4)


def test_normalize_mime_type_falls_back(settings: Settings) -> None:
    assert files.normalize_mime_type("IMAGE/PNG; charset=binary") == "image/png"
    assert files.normalize_mime_type("nonsense") == files.DEFAULT_MIME_TYPE
    assert files.normalize_mime_type(None) == files.DEFAULT_MIME_TYPE


# --------------------------------------------------------------------- 媒体：HTTP 闭环
def test_upload_writes_file_and_metadata(api_client: TestClient, settings: Settings, fake_pg: FakeDatabase) -> None:
    data = _upload(api_client)
    media_id = data["id"]
    assert len(media_id) == 32
    assert data["sha256"] == hashlib.sha256(PNG_BYTES).hexdigest()
    assert data["size_bytes"] == len(PNG_BYTES)
    assert data["mime_type"] == "image/png"
    assert data["content_url"] == f"/v1/media/{media_id}/content"
    assert "safe_name" not in data
    assert isinstance(data["created_at"], str) and data["created_at"]

    stored = settings.data_dir / "media" / media_id / "note.png"
    assert stored.is_file()
    assert stored.read_bytes() == PNG_BYTES
    assert media_id in fake_pg.media
    assert fake_pg.dsns == [FAKE_DSN]
    assert any("LOCAL_MEDIA" in statement for statement in fake_pg.ddl)


def test_upload_sanitizes_traversal_name(api_client: TestClient, settings: Settings) -> None:
    data = _upload(api_client, name="../../etc/passwd")
    media_id = data["id"]
    assert (settings.data_dir / "media" / media_id / "passwd").is_file()
    assert not (settings.data_dir / "etc").exists()
    assert (settings.data_dir / "media" / media_id).parent == (settings.data_dir / "media")


def test_list_get_content_and_delete(api_client: TestClient, settings: Settings) -> None:
    media_id = _upload(api_client)["id"]

    listing = api_client.get("/v1/media").json()["data"]
    assert [item["id"] for item in listing] == [media_id]
    assert "content_base64" not in listing[0]

    detail = api_client.get(f"/v1/media/{media_id}").json()["data"]
    assert base64.b64decode(detail["content_base64"]) == PNG_BYTES

    metadata_only = api_client.get(f"/v1/media/{media_id}", params={"include_content": False}).json()["data"]
    assert metadata_only["content_base64"] is None
    assert metadata_only["name"] == "note.png"

    raw = api_client.get(f"/v1/media/{media_id}/content")
    assert raw.status_code == 200
    assert raw.content == PNG_BYTES
    assert raw.headers["content-type"] == "image/png"
    assert "filename*=UTF-8''note.png" in raw.headers["content-disposition"]

    removed = api_client.delete(f"/v1/media/{media_id}")
    assert removed.status_code == 200
    assert removed.json()["data"] == {"id": media_id, "deleted": True}
    assert not (settings.data_dir / "media" / media_id).exists()
    assert api_client.get(f"/v1/media/{media_id}").status_code == 404
    assert api_client.delete(f"/v1/media/{media_id}").status_code == 404


def test_list_media_respects_pagination(api_client: TestClient) -> None:
    first = _upload(api_client, name="a.txt", mime_type="text/plain", data=b"aaaa")["id"]
    second = _upload(api_client, name="b.txt", mime_type="text/plain", data=b"bbbb")["id"]
    page = api_client.get("/v1/media", params={"limit": 1, "offset": 0}).json()["data"]
    assert [item["id"] for item in page] == [second]
    page2 = api_client.get("/v1/media", params={"limit": 1, "offset": 1}).json()["data"]
    assert [item["id"] for item in page2] == [first]
    assert api_client.get("/v1/media", params={"limit": 0}).status_code == 422


def test_upload_rejects_oversized_payload(api_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files, "MAX_MEDIA_BYTES", 4)
    response = api_client.post(
        "/v1/media/upload",
        json={"name": "big.bin", "content_base64": _b64(b"0123456789")},
    )
    assert response.status_code == 413
    assert "上限" in response.json()["detail"]


def test_upload_rejects_invalid_payload(api_client: TestClient) -> None:
    bad_base64 = api_client.post("/v1/media/upload", json={"name": "x.png", "content_base64": "不是 base64"})
    assert bad_base64.status_code == 400
    missing = api_client.post("/v1/media/upload", json={"name": "x.png"})
    assert missing.status_code == 422


def test_media_without_postgres_returns_503(settings: Settings) -> None:
    """PG 没起来时如实 503（而不是 500 / 或悄悄写盘）。"""
    app = create_app(settings)
    app.include_router(media.router)
    with TestClient(app) as client:
        response = client.get("/v1/media")
        missing = client.get("/v1/media/" + "a" * 32)
    assert response.status_code == 503
    assert "PostgreSQL" in response.json()["detail"]
    assert missing.status_code == 503


def test_download_reports_missing_file_as_404(api_client: TestClient, settings: Settings) -> None:
    media_id = _upload(api_client)["id"]
    shutil.rmtree(settings.data_dir / "media" / media_id)
    response = api_client.get(f"/v1/media/{media_id}/content")
    assert response.status_code == 404
    assert "不在磁盘" in response.json()["detail"]


# --------------------------------------------------------------------- 记忆层
def test_normalize_and_split_tags() -> None:
    assert memory_store.normalize_tags(["  pg , 存储 ", "pg 存储", "  "]) == "pg 存储"
    assert memory_store.normalize_tags(None) == ""
    assert memory_store.split_tags("pg 存储") == ["pg 存储"]
    assert memory_store.split_tags("") == []


def test_memory_create_list_search_delete(api_client: TestClient, fake_pg: FakeDatabase) -> None:
    first = api_client.post(
        "/v1/memory/entries",
        json={"content": "第一条笔记：关于本地存储", "title": "笔记", "tags": ["存储", "pg"]},
    )
    assert first.status_code == 201, first.text
    first_data = first.json()["data"]
    assert len(first_data["id"]) == 32
    assert first_data["kind"] == "note"
    assert first_data["tags"] == ["存储", "pg"]
    assert first_data["source"] == ""

    second = api_client.post("/v1/memory/entries", json={"content": "第二条：记忆层检索", "kind": "snippet"})
    assert second.status_code == 201
    second_data = second.json()["data"]

    listing = api_client.get("/v1/memory/entries").json()["data"]
    assert [item["id"] for item in listing] == [second_data["id"], first_data["id"]]
    assert listing[0]["kind"] == "snippet"

    hits = api_client.post("/v1/memory/search", json={"query": "本地存储"}).json()["data"]
    assert [item["id"] for item in hits] == [first_data["id"]]

    by_tag = api_client.post("/v1/memory/search", json={"query": "pg"}).json()["data"]
    assert [item["id"] for item in by_tag] == [first_data["id"]]

    assert api_client.post("/v1/memory/search", json={"query": "无此关键词"}).json()["data"] == []

    removed = api_client.delete(f"/v1/memory/entries/{first_data['id']}")
    assert removed.status_code == 200
    assert removed.json()["data"] == {"id": first_data["id"], "deleted": True}
    assert api_client.delete(f"/v1/memory/entries/{first_data['id']}").status_code == 404
    assert [item["id"] for item in api_client.get("/v1/memory/entries").json()["data"]] == [second_data["id"]]
    assert any("LOCAL_MEMORY" in statement for statement in fake_pg.ddl)


def test_memory_rejects_blank_and_invalid_payload(api_client: TestClient) -> None:
    assert api_client.post("/v1/memory/entries", json={"content": "   "}).status_code == 400
    assert api_client.post("/v1/memory/entries", json={}).status_code == 422
    assert api_client.post("/v1/memory/search", json={"query": "   "}).status_code == 400
    assert api_client.post("/v1/memory/search", json={}).status_code == 422
    assert api_client.post("/v1/memory/search", json={"query": "x" * 500}).status_code == 422


def test_memory_without_postgres_returns_503(settings: Settings) -> None:
    app = create_app(settings)
    app.include_router(memory.router)
    with TestClient(app) as client:
        response = client.get("/v1/memory/entries")
    assert response.status_code == 503
