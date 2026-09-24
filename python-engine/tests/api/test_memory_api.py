"""PR-4 Task 39: API 集成测试 - 记忆管理 REST 端点。

覆盖：
1. L2 Profile CRUD API（list/upsert/update/delete/clear/search/organize）
2. L3 Summary API（list summaries）
3. Conflict API（list/resolve/delete）
4. 错误处理（缺失 user_id、无效参数、服务不可用）
5. 多租户隔离
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.memory.conflict_manager import ConflictManager
from app.memory.layers import (
    SlotType,
    SourceType,
)
from app.memory.profile_card import ProfileCard
from app.memory.service import MemoryService
from app.memory.session_meta import SessionMetaStore

# ── Mock 基础设施 ────────────────────────────────────────────────────


class MockDatabasePool:
    def __init__(self):
        self._items: dict[tuple, dict[str, Any]] = {}
        self._next_id = 1

    def _key(self, tenant_id, user_id, slot, item_key):
        return (tenant_id, user_id, slot, item_key)

    def _make_row(self, item):
        ts = item.get("created_at", time.time())
        return {
            "id": item.get("id", f"mem_{self._next_id}"),
            "slot": item["slot"],
            "item_key": item["item_key"],
            "item_value": item["item_value"],
            "confidence": item["confidence"],
            "source": item["source"],
            "version": item.get("version", 1),
            "confirmed_at": datetime.fromtimestamp(item["confirmed_at"], tz=UTC)
            if item.get("confirmed_at")
            else None,
            "last_referenced_at": datetime.fromtimestamp(item["last_referenced_at"], tz=UTC)
            if item.get("last_referenced_at")
            else None,
            "created_at": datetime.fromtimestamp(ts, tz=UTC),
            "updated_at": datetime.fromtimestamp(item["updated_at"], tz=UTC),
            "embedding": item.get("embedding"),
            "access_count": item.get("access_count", 0),
            "status": item.get("status", "active"),
        }

    async def fetch(self, query, *args):
        if "FROM user_memory_profile" in query:
            if "WHERE tenant_id = $1 AND user_id = $2" in query:
                results = []
                for (tid, uid, _slot, _key), item in self._items.items():
                    if tid == args[0] and uid == args[1]:
                        results.append(self._make_row(item))
                results.sort(key=lambda r: (r["slot"], r["item_key"]))
                return results
        return []

    async def fetchrow(self, query, *args):
        if "slot = $3 AND item_key = $4" in query:
            item = self._items.get(self._key(args[0], args[1], args[2], args[3]))
            return self._make_row(item) if item else None
        return None

    async def execute(self, query, *args):
        if "INSERT INTO user_memory_profile" in query:
            tenant_id, user_id, slot, item_key = args[0], args[1], args[2], args[3]
            self._next_id += 1
            now = time.time()
            self._items[self._key(tenant_id, user_id, slot, item_key)] = {
                "id": f"mem_{self._next_id}",
                "slot": slot,
                "item_key": item_key,
                "item_value": args[4],
                "confidence": args[5],
                "source": args[6],
                "version": 1,
                "confirmed_at": now if args[6] == "user_confirmed" else None,
                "created_at": now,
                "updated_at": now,
                "access_count": 0,
                "status": "active",
            }
            return "INSERT 0 1"
        elif "UPDATE user_memory_profile" in query:
            tenant_id, user_id, slot, item_key = args[0], args[1], args[2], args[3]
            key = self._key(tenant_id, user_id, slot, item_key)
            if key in self._items:
                item = self._items[key]
                item["item_value"] = args[4]
                item["confidence"] = args[5]
                item["source"] = args[6]
                item["version"] = args[7]
                item["updated_at"] = time.time()
                if args[6] == "user_confirmed":
                    item["confirmed_at"] = time.time()
                return "UPDATE 1"
            return "UPDATE 0"
        elif "DELETE FROM user_memory_profile" in query:
            if "slot = $3 AND item_key = $4" in query:
                key = self._key(args[0], args[1], args[2], args[3])
                if key in self._items:
                    del self._items[key]
                    return "DELETE 1"
                return "DELETE 0"
            elif "id = $3" in query:
                for k, v in self._items.items():
                    if v.get("id") == args[2]:
                        del self._items[k]
                        return "DELETE 1"
                return "DELETE 0"
        return "OK"


class MockRedis:
    def __init__(self):
        self._store: dict[str, str] = {}
        self._expiry: dict[str, float] = {}
        self._sets: dict[str, set[str]] = {}

    async def get(self, key):
        if key in self._expiry and time.time() > self._expiry[key]:
            self._store.pop(key, None)
            self._expiry.pop(key, None)
            return None
        return self._store.get(key)

    async def set(self, key, value, ex=None):
        self._store[key] = value
        return True

    async def setex(self, key, ttl, value):
        self._store[key] = value
        self._expiry[key] = time.time() + ttl
        return True

    async def delete(self, *keys):
        for key in keys:
            if key in self._store:
                self._store.pop(key, None)
                self._expiry.pop(key, None)
                return True
        return False

    async def sadd(self, key, value):
        if key not in self._sets:
            self._sets[key] = set()
        self._sets[key].add(value)
        return True

    async def srem(self, key, value):
        if key in self._sets:
            self._sets[key].discard(value)
        return True

    async def smembers(self, key):
        return self._sets.get(key, set())

    async def expire(self, key, ttl):
        self._expiry[key] = time.time() + ttl
        return True

    async def incr(self, key):
        count = int(self._store.get(key, "0"))
        count += 1
        self._store[key] = str(count)
        return count


class MockSummaryStore:
    def __init__(self):
        self._summaries: list[dict[str, Any]] = []

    async def list_active(self, tenant_id, user_id, limit=50):
        return [s for s in self._summaries
                if s["tenant_id"] == tenant_id and s["user_id"] == user_id][:limit]

    async def recall(self, scope, query="", top_k=5):
        return []

    async def insert(self, entry, embedding=None):
        self._summaries.append({
            "id": entry.id,
            "tenant_id": entry.tenant_id,
            "user_id": entry.user_id,
            "session_id": entry.session_id,
            "content": entry.content,
            "topics": entry.topics,
            "status": "active",
            "created_at": datetime.now(UTC),
        })
        return entry

    async def delete_all(self, tenant_id, user_id):
        count = len([s for s in self._summaries
                     if s["tenant_id"] == tenant_id and s["user_id"] == user_id])
        self._summaries = [s for s in self._summaries
                           if not (s["tenant_id"] == tenant_id and s["user_id"] == user_id)]
        return count


# ── Fixtures ──────────────────────────────────────────────────────────


def _make_service():
    """创建完整的 MemoryService 实例。

    L2 记忆条目走内存 ``ProfileStore``（``user_memory_entries`` 的契约），
    个性化设置仍走被 mock 的 ``ProfileCard``（``user_memory_profile``）——
    两者职责不同，各用各的替身。
    """
    from tests.fakes import InMemoryProfileStore

    mock_redis = MockRedis()
    mock_pool = MockDatabasePool()
    session_meta_store = SessionMetaStore()
    profile_card = ProfileCard.__new__(ProfileCard)
    profile_card._redis = mock_redis
    profile_card._pool = mock_pool
    profile_card._conflict_manager = ConflictManager(mock_redis)
    summary_store = MockSummaryStore()

    svc = MemoryService(
        store=InMemoryProfileStore(),
        session_meta_store=session_meta_store,
        profile_card=profile_card,
        summary_store=summary_store,
    )
    return svc


def _make_app(svc):
    """创建 FastAPI 测试应用。"""
    from fastapi import FastAPI

    from app.api.memory import router
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def svc():
    return _make_service()


@pytest.fixture
def app(svc, monkeypatch):
    """创建已注入 service 的测试 app。"""
    import app.api.memory as mem_api
    monkeypatch.setattr(mem_api, "get_service", lambda: svc)
    return _make_app(svc)


@pytest.fixture
def seeded_svc(svc):
    """预置数据的 service。"""
    import asyncio
    loop = asyncio.new_event_loop()

    async def _seed():
        await svc.upsert(
            tenant_id="t1", user_id="u1",
            slot=SlotType.FACT, key="pref_lang",
            value="Python", confidence=90,
            source=SourceType.USER_CONFIRMED,
        )
        await svc.upsert(
            tenant_id="t1", user_id="u1",
            slot=SlotType.PREFERENCE, key="theme",
            value="dark", confidence=80,
            source=SourceType.DERIVED,
        )

    try:
        loop.run_until_complete(_seed())
    finally:
        loop.close()
    return svc


@pytest.fixture
def seeded_app(seeded_svc, monkeypatch):
    import app.api.memory as mem_api
    monkeypatch.setattr(mem_api, "get_service", lambda: seeded_svc)
    return _make_app(seeded_svc)


# ── L2 Profile API ────────────────────────────────────────────────────


class TestProfileAPI:
    async def test_list_profile_empty(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/v1/memory/profile?user_id=u1&tenant_id=t1")
            assert r.status_code == 200
            body = r.json()
            assert body["success"] is True
            assert body["total"] == 0

    async def test_list_profile_with_data(self, seeded_app):
        async with httpx.AsyncClient(transport=ASGITransport(app=seeded_app), base_url="http://t") as c:
            r = await c.get("/v1/memory/profile?user_id=u1&tenant_id=t1")
            assert r.status_code == 200
            body = r.json()
            assert body["success"] is True
            assert body["total"] >= 2

    async def test_list_profile_missing_user_id(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/v1/memory/profile")
            assert r.status_code == 400
            assert "user_id is required" in r.json()["error"]

    async def test_upsert_profile(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/v1/memory/profile?user_id=u1&tenant_id=t1", json={
                "slot": "fact", "key": "birthday", "value": "Jan 1",
                "confidence": 95, "source": "user_confirmed",
            })
            assert r.status_code == 200
            body = r.json()
            assert body["success"] is True

    async def test_upsert_profile_missing_user_id(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/v1/memory/profile", json={"slot": "fact", "key": "k", "value": "v"})
            assert r.status_code == 400

    async def test_update_profile(self, seeded_app):
        async with httpx.AsyncClient(transport=ASGITransport(app=seeded_app), base_url="http://t") as c:
            r_list = await c.get("/v1/memory/profile?user_id=u1&tenant_id=t1")
            entry_id = r_list.json()["entries"][0]["id"]

            r = await c.put("/v1/memory/profile?user_id=u1&tenant_id=t1", json={
                "id": entry_id, "value": "Rust",
            })
            assert r.status_code == 200
            body = r.json()
            assert body["success"] is True

    async def test_update_profile_missing_id(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.put("/v1/memory/profile?user_id=u1&tenant_id=t1", json={})
            assert r.status_code == 400
            assert "id is required" in r.json()["error"]

    async def test_update_profile_not_found(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.put("/v1/memory/profile?user_id=u1&tenant_id=t1", json={
                "id": "nonexistent", "value": "test",
            })
            assert r.status_code == 404

    async def test_delete_profile(self, seeded_app):
        async with httpx.AsyncClient(transport=ASGITransport(app=seeded_app), base_url="http://t") as c:
            r_list = await c.get("/v1/memory/profile?user_id=u1&tenant_id=t1")
            entry_id = r_list.json()["entries"][0]["id"]

            r = await c.delete(f"/v1/memory/profile/{entry_id}?user_id=u1&tenant_id=t1")
            assert r.status_code == 200
            body = r.json()
            assert body["success"] is True

    async def test_delete_profile_not_found(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.delete("/v1/memory/profile/nonexistent?user_id=u1&tenant_id=t1")
            assert r.status_code == 404

    async def test_clear_profile(self, seeded_app):
        async with httpx.AsyncClient(transport=ASGITransport(app=seeded_app), base_url="http://t") as c:
            r = await c.post("/v1/memory/profile/clear?user_id=u1&tenant_id=t1", json={"confirm": True})
            assert r.status_code == 200
            body = r.json()
            assert body["success"] is True

    async def test_clear_profile_requires_confirm(self, seeded_app):
        async with httpx.AsyncClient(transport=ASGITransport(app=seeded_app), base_url="http://t") as c:
            r = await c.post("/v1/memory/profile/clear?user_id=u1&tenant_id=t1", json={})
            assert r.status_code == 400
            assert "confirm=true" in r.json()["error"]

    async def test_search_profile(self, seeded_app):
        async with httpx.AsyncClient(transport=ASGITransport(app=seeded_app), base_url="http://t") as c:
            r = await c.post("/v1/memory/search?user_id=u1&tenant_id=t1", json={
                "query": "Python", "top_k": 5,
            })
            assert r.status_code == 200
            body = r.json()
            assert body["success"] is True

    async def test_organize_profile(self, seeded_app):
        async with httpx.AsyncClient(transport=ASGITransport(app=seeded_app), base_url="http://t") as c:
            r = await c.post("/v1/memory/organize?user_id=u1&tenant_id=t1")
            assert r.status_code == 200
            body = r.json()
            assert body["success"] is True

    async def test_organize_status(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/v1/memory/organize/status?user_id=u1&tenant_id=t1")
            assert r.status_code == 200
            body = r.json()
            assert body["success"] is True


# ── L3 Summary API ───────────────────────────────────────────────────


class TestSummaryAPI:
    async def test_list_summaries_empty(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/v1/memory/summaries?user_id=u1&tenant_id=t1")
            assert r.status_code == 200
            body = r.json()
            assert body["success"] is True
            assert "summaries" in body

    async def test_list_summaries_missing_user_id(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/v1/memory/summaries")
            assert r.status_code == 400


# ── Conflict API ─────────────────────────────────────────────────────


class TestConflictAPI:
    async def test_list_conflicts_empty(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/v1/memory/conflicts?user_id=u1&tenant_id=t1")
            assert r.status_code == 200
            body = r.json()
            assert body["success"] is True
            assert body["count"] == 0

    async def test_list_conflicts_with_data(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            # First create a user_confirmed entry
            await c.post("/v1/memory/profile?user_id=u1&tenant_id=t1", json={
                "slot": "fact", "key": "city", "value": "上海",
                "confidence": 95, "source": "user_confirmed",
            })
            # Then overwrite with derived to trigger conflict
            await c.post("/v1/memory/profile?user_id=u1&tenant_id=t1", json={
                "slot": "fact", "key": "city", "value": "北京",
                "confidence": 50, "source": "derived",
            })
            r = await c.get("/v1/memory/conflicts?user_id=u1&tenant_id=t1")
            assert r.status_code == 200
            body = r.json()
            assert body["count"] == 1
            assert body["conflicts"][0]["key"] == "city"

    async def test_list_conflicts_missing_user_id(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/v1/memory/conflicts")
            assert r.status_code == 400

    async def test_resolve_conflict_use_new(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            # Create conflict scenario
            await c.post("/v1/memory/profile?user_id=u1&tenant_id=t1", json={
                "slot": "fact", "key": "lang", "value": "Python",
                "confidence": 95, "source": "user_confirmed",
            })
            await c.post("/v1/memory/profile?user_id=u1&tenant_id=t1", json={
                "slot": "fact", "key": "lang", "value": "Go",
                "confidence": 50, "source": "derived",
            })
            # Get conflict ID
            r_list = await c.get("/v1/memory/conflicts?user_id=u1&tenant_id=t1")
            conflict_id = r_list.json()["conflicts"][0]["conflict_id"]

            # Resolve with use_new
            r = await c.post(
                f"/v1/memory/conflicts/{conflict_id}/resolve?user_id=u1&tenant_id=t1",
                json={"resolution": "use_new"},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["success"] is True

    async def test_resolve_conflict_keep_old(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post("/v1/memory/profile?user_id=u1&tenant_id=t1", json={
                "slot": "fact", "key": "color", "value": "red",
                "confidence": 95, "source": "user_confirmed",
            })
            await c.post("/v1/memory/profile?user_id=u1&tenant_id=t1", json={
                "slot": "fact", "key": "color", "value": "blue",
                "confidence": 50, "source": "derived",
            })
            r_list = await c.get("/v1/memory/conflicts?user_id=u1&tenant_id=t1")
            conflict_id = r_list.json()["conflicts"][0]["conflict_id"]

            r = await c.post(
                f"/v1/memory/conflicts/{conflict_id}/resolve?user_id=u1&tenant_id=t1",
                json={"resolution": "keep_old"},
            )
            assert r.status_code == 200

    async def test_resolve_conflict_manual(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post("/v1/memory/profile?user_id=u1&tenant_id=t1", json={
                "slot": "fact", "key": "os", "value": "Windows",
                "confidence": 95, "source": "user_confirmed",
            })
            await c.post("/v1/memory/profile?user_id=u1&tenant_id=t1", json={
                "slot": "fact", "key": "os", "value": "Linux",
                "confidence": 50, "source": "derived",
            })
            r_list = await c.get("/v1/memory/conflicts?user_id=u1&tenant_id=t1")
            conflict_id = r_list.json()["conflicts"][0]["conflict_id"]

            r = await c.post(
                f"/v1/memory/conflicts/{conflict_id}/resolve?user_id=u1&tenant_id=t1",
                json={"resolution": "manual", "manual_value": "macOS"},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["conflict"]["resolved_value"] == "macOS"

    async def test_resolve_conflict_invalid_resolution(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                "/v1/memory/conflicts/any/resolve?user_id=u1&tenant_id=t1",
                json={"resolution": "invalid"},
            )
            assert r.status_code == 400

    async def test_resolve_conflict_not_found(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                "/v1/memory/conflicts/nonexistent/resolve?user_id=u1&tenant_id=t1",
                json={"resolution": "keep_old"},
            )
            assert r.status_code == 404

    async def test_delete_conflict(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            # Create a conflict
            await c.post("/v1/memory/profile?user_id=u1&tenant_id=t1", json={
                "slot": "fact", "key": "vehicle", "value": "car",
                "confidence": 95, "source": "user_confirmed",
            })
            await c.post("/v1/memory/profile?user_id=u1&tenant_id=t1", json={
                "slot": "fact", "key": "vehicle", "value": "bike",
                "confidence": 50, "source": "derived",
            })
            r_list = await c.get("/v1/memory/conflicts?user_id=u1&tenant_id=t1")
            conflict_id = r_list.json()["conflicts"][0]["conflict_id"]

            # Delete the conflict
            r = await c.delete(f"/v1/memory/conflicts/{conflict_id}?user_id=u1&tenant_id=t1")
            assert r.status_code == 200
            body = r.json()
            assert body["success"] is True
            assert body["deleted"] == conflict_id

            # Verify it's gone
            r_after = await c.get("/v1/memory/conflicts?user_id=u1&tenant_id=t1")
            assert r_after.json()["count"] == 0

    async def test_delete_conflict_not_found(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.delete("/v1/memory/conflicts/nonexistent?user_id=u1&tenant_id=t1")
            assert r.status_code == 404


# ── 错误处理 & 多租户 ────────────────────────────────────────────────


class TestErrorHandling:
    async def test_service_unavailable(self, monkeypatch):
        import app.api.memory as mem_api
        monkeypatch.setattr(mem_api, "get_service", lambda: None)
        app = _make_app(None)
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/v1/memory/profile?user_id=u1&tenant_id=t1")
            assert r.status_code == 503

    async def test_service_unavailable_conflicts(self, monkeypatch):
        import app.api.memory as mem_api
        monkeypatch.setattr(mem_api, "get_service", lambda: None)
        app = _make_app(None)
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/v1/memory/conflicts?user_id=u1&tenant_id=t1")
            assert r.status_code == 503

    async def test_tenant_isolation(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            # User u1 in tenant t1 creates entries
            await c.post("/v1/memory/profile?user_id=u1&tenant_id=t1", json={
                "slot": "fact", "key": "k1", "value": "v_t1",
                "confidence": 90, "source": "user_confirmed",
            })
            # Same user in different tenant t2 sees nothing
            r = await c.get("/v1/memory/profile?user_id=u1&tenant_id=t2")
            assert r.json()["total"] == 0

    async def test_user_isolation(self, app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            # User u1 creates entries
            await c.post("/v1/memory/profile?user_id=u1&tenant_id=t1", json={
                "slot": "fact", "key": "k1", "value": "v_u1",
                "confidence": 90, "source": "user_confirmed",
            })
            # User u2 in same tenant sees nothing
            r = await c.get("/v1/memory/profile?user_id=u2&tenant_id=t1")
            assert r.json()["total"] == 0
