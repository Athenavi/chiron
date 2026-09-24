# 记忆系统 L2 档案卡测试 — 数据结构 / 服务 / API / 工具
import asyncio
import math
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import ASGITransport

from app.memory.layers import (
    SLOT_LABELS,
    cosine_similarity,
    recency_decay,
    rerank_score,
)
from app.memory.service import MemoryService
from app.tools import context as tool_context
from tests.fakes import InMemoryProfileStore

# ── 测试基建 ──────────────────────────────────────────────



class FakeEmbedder:
    """确定性 embedder：按精确文本映射向量；未映射文本抛错（fail-soft 路径可测）。"""

    def __init__(self):
        self.mapping: dict[str, list[float]] = {}
        self.calls: list[str] = []

    def set(self, text: str, vec: list[float]) -> None:
        self.mapping[text] = vec

    async def __call__(self, text: str) -> list[float]:
        self.calls.append(text)
        if text in self.mapping:
            return list(self.mapping[text])
        raise RuntimeError(f"no embedding for: {text!r}")


def _service(embedder=None, store=None):
    return MemoryService(store=store or InMemoryProfileStore(), embedder=embedder)


# ── layers 纯函数 ─────────────────────────────────────────

class TestLayers:
    def test_cosine_identical_orthogonal(self):
        assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
        assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_cosine_dimension_mismatch_and_empty(self):
        assert cosine_similarity([1, 0], [1, 0, 0]) == 0.0
        assert cosine_similarity([], [1.0]) == 0.0
        assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_recency_decay(self):
        now = datetime.now(UTC)
        assert recency_decay(now) == pytest.approx(1.0)
        old = now - timedelta(days=60)
        assert recency_decay(old, half_life_days=60) == pytest.approx(math.exp(-1), rel=0.05)
        assert recency_decay(None) == 0.5

    def test_rerank_prefers_high_confidence(self):
        recent = datetime.now(UTC)
        low = rerank_score(0.9, 10, recent, False)
        high = rerank_score(0.9, 100, recent, False)
        assert high > low

    def test_slot_labels_cover_all(self):
        from app.memory.layers import SLOTS
        assert set(SLOT_LABELS) == set(SLOTS)


# ── MemoryService CRUD ────────────────────────────────────

class TestServiceUpsert:
    async def test_upsert_creates_and_embeds(self):
        emb = FakeEmbedder()
        emb.set("timezone: 上海时区 UTC+8", [1.0, 0.0])
        svc = _service(embedder=emb)
        result = await svc.upsert("t1", "u1", "preference", "timezone", "上海时区 UTC+8",
                                  confidence=80, source="user_confirmed")
        assert result["created"] is True
        assert result["entry"]["confidence"] == 80
        assert result["entry"]["has_embedding"] is True

    async def test_upsert_same_key_updates(self):
        svc = _service()
        r1 = await svc.upsert("t1", "u1", "fact", "team_size", "5 人")
        r2 = await svc.upsert("t1", "u1", "fact", "team_size", "6 人")
        assert r1["created"] is True and r2["created"] is False
        assert r2["entry"]["value"] == "6 人"

    async def test_upsert_invalid_slot_fail_loud(self):
        svc = _service()
        with pytest.raises(ValueError, match="invalid slot"):
            await svc.upsert("t1", "u1", "hobby", "k", "v")

    async def test_upsert_empty_value_fail_loud(self):
        svc = _service()
        with pytest.raises(ValueError, match="value required"):
            await svc.upsert("t1", "u1", "fact", "k", "  ")

    async def test_upsert_embedding_failure_still_saves(self):
        """embedder 不可用 → 条目照常入库（embedding=NULL），fail-soft。"""
        svc = _service(embedder=FakeEmbedder())
        result = await svc.upsert("t1", "u1", "fact", "k", "v")
        assert result["created"] is True
        assert result["entry"]["has_embedding"] is False

    async def test_capacity_evicts_derived_not_confirmed(self):
        from app.config import settings
        svc = _service()
        for i in range(settings.memory_profile_max_items):
            await svc.upsert("t1", "u1", "fact", f"k{i}", f"v{i}", confidence=10)
        # 已达上限：再插入一条 → 淘汰一条 derived
        result = await svc.upsert("t1", "u1", "fact", "k_new", "v_new")
        assert result.get("evicted") == 1
        data = await svc.list_entries("t1", "u1")
        assert data["total"] == settings.memory_profile_max_items

    async def test_near_duplicate_hint(self):
        vec = [1.0, 0.0]
        emb = FakeEmbedder()
        emb.set("city: 上海", vec)
        emb.set("city2: 上海（中国）", [0.99, 0.02])
        svc = _service(embedder=emb)
        await svc.upsert("t1", "u1", "fact", "city", "上海")
        r = await svc.upsert("t1", "u1", "fact", "city2", "上海（中国）")
        assert r.get("duplicate_of", {}).get("key") == "city"


class TestServiceSearch:
    async def test_semantic_search_ranking(self):
        emb = FakeEmbedder()
        emb.set("timezone: 上海时区", [1.0, 0.0])
        emb.set("editor: VSCode", [0.0, 1.0])
        svc = _service(embedder=emb)
        await svc.upsert("t1", "u1", "preference", "timezone", "上海时区", confidence=90)
        await svc.upsert("t1", "u1", "preference", "editor", "VSCode", confidence=30)
        emb.set("上海时区", [0.95, 0.05])  # 查询向量贴近 timezone
        data = await svc.search("t1", "u1", query="上海时区")
        assert data["mode"] == "semantic"
        assert data["results"][0]["key"] == "timezone"
        assert data["results"][0]["similarity"] > 0.9
        # 命中即引用
        entries = await svc._store.list("t1", "u1")
        assert entries[0].access_count == 1

    async def test_search_keyword_fallback_when_embed_fails(self):
        svc = _service(embedder=FakeEmbedder())
        await svc.upsert("t1", "u1", "fact", "language", "用 Python 写后端")
        data = await svc.search("t1", "u1", query="Python")
        assert data["mode"] == "keyword"
        assert data["results"][0]["key"] == "language"

    async def test_search_filters_below_threshold(self):
        emb = FakeEmbedder()
        emb.set("a: aaa", [1.0, 0.0])
        emb.set("query", [0.0, 1.0])  # 与条目正交
        svc = _service(embedder=emb)
        await svc.upsert("t1", "u1", "fact", "a", "aaa")
        data = await svc.search("t1", "u1", query="query")
        assert data["count"] == 0

    async def test_search_empty_query_fail_loud(self):
        svc = _service()
        with pytest.raises(ValueError, match="query is required"):
            await svc.search("t1", "u1", query="  ")

    async def test_search_tenant_isolation(self):
        svc = _service()
        await svc.upsert("t1", "u1", "fact", "k", "v")
        data = await svc.search("t2", "u1", query="v")
        assert data["count"] == 0


class TestServiceOrganize:
    async def test_organize_backfill_merge_archive(self):
        from app.config import settings
        emb = FakeEmbedder()
        emb.set("city: 上海", [1.0, 0.0])
        emb.set("city2: 上海市", [0.99, 0.01])  # 近重复（cosine>0.95）
        svc = _service(embedder=emb)
        await svc.upsert("t1", "u1", "fact", "city", "上海", confidence=90)
        await svc.upsert("t1", "u1", "fact", "city2", "上海市", confidence=30)
        # lang 此时未映射嵌入文本 → upsert 走 fail-soft 存 NULL；整理时 mapping 已就绪则补齐
        await svc.upsert("t1", "u1", "fact", "lang", "Python", confidence=50)
        emb.set("lang: Python", [0.0, 1.0])
        # 构造衰退条目：低置信 + 200 天未引用
        stale = await svc._store.get_by_key("t1", "u1", "fact", "city2")
        stale.last_accessed_at = datetime.now(UTC) - timedelta(
            days=settings.memory_archive_days + 20)

        result = await svc.organize_now("t1", "u1")
        assert result.merged == 1      # city/city2 近重复合并
        assert result.backfilled == 1  # lang 补嵌入
        data = await svc.list_entries("t1", "u1")
        keys = {e["key"] for e in data["entries"]}
        assert keys == {"city", "lang"}  # city2 已被合并删除

    async def test_organize_keeps_higher_confidence_on_merge(self):
        emb = FakeEmbedder()
        emb.set("a: same", [1.0, 0.0])
        emb.set("b: same-ish", [0.99, 0.02])
        svc = _service(embedder=emb)
        await svc.upsert("t1", "u1", "fact", "a", "same", confidence=20)
        await svc.upsert("t1", "u1", "fact", "b", "same-ish", confidence=90)
        result = await svc.organize_now("t1", "u1")
        assert result.merged == 1
        entries = await svc._store.list("t1", "u1")
        assert entries[0].item_key == "b"  # 保留置信度高的

    async def test_start_organize_async_and_status(self):
        svc = _service()
        await svc.upsert("t1", "u1", "fact", "k", "v")
        r = await svc.start_organize("t1", "u1")
        assert r["started"] is True
        # 等待后台任务完成（以 finished_at 判定，avoid running=False 在「未启动」与「已完成」二义）
        for _ in range(100):
            status = svc.organize_status("t1", "u1")
            if status["finished_at"] or status["result"] or status["error"]:
                break
            await asyncio.sleep(0.01)
        status = svc.organize_status("t1", "u1")
        assert status["running"] is False
        assert status["result"] is not None
        assert status["finished_at"] > 0

    async def test_start_organize_single_flight(self):
        svc = _service()
        await svc.upsert("t1", "u1", "fact", "k", "v")
        await svc.start_organize("t1", "u1")
        r = await svc.start_organize("t1", "u1")
        # 第二次触发：可能已完成（started=True）或单飞行拒绝（already_running）——二者皆合法
        assert r["started"] is True or r["reason"] == "already_running"


class TestServiceDelete:
    async def test_clear_all(self):
        svc = _service()
        await svc.upsert("t1", "u1", "fact", "k1", "v1")
        await svc.upsert("t1", "u1", "preference", "k2", "v2")
        assert await svc.clear_all("t1", "u1") == 2
        assert (await svc.list_entries("t1", "u1"))["total"] == 0

    async def test_forget_by_key_scoped(self):
        svc = _service()
        await svc.upsert("t1", "u1", "fact", "k", "v")
        await svc.upsert("t1", "u1", "preference", "k", "v")
        assert await svc.forget_by_key("t1", "u1", "k", slot="fact") == 1
        data = await svc.list_entries("t1", "u1")
        assert data["counts"]["fact"] == 0 and data["counts"]["preference"] == 1


# ── API 层（httpx ASGI）──────────────────────────────────

def _api_app(svc):
    from fastapi import FastAPI

    from app.api.memory import router
    app = FastAPI()
    app.include_router(router)
    return app


class TestMemoryAPI:
    async def test_endpoints_503_when_service_unbound(self):
        app = _api_app(None)
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/v1/memory/profile?user_id=u1")
            assert r.status_code == 503

    async def test_profile_roundtrip(self, monkeypatch):
        svc = _service()
        app = _api_app(svc)
        import app.api.memory as mem_api
        monkeypatch.setattr(mem_api, "get_service", lambda: svc)
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/v1/memory/profile?user_id=u1", json={
                "slot": "identity", "key": "role", "value": "后端工程师",
                "confidence": 90, "source": "user_confirmed",
            })
            assert r.status_code == 200
            body = r.json()
            assert body["success"] is True and body["created"] is True

            r = await c.get("/v1/memory/profile?user_id=u1")
            body = r.json()
            assert body["total"] == 1
            assert body["counts"]["identity"] == 1
            assert body["entries"][0]["slot_label"] == "身份"

            # 编辑
            entry_id = body["entries"][0]["id"]
            r = await c.put("/v1/memory/profile?user_id=u1", json={
                "id": entry_id, "value": "全栈工程师", "confidence": 95,
            })
            assert r.json()["entry"]["value"] == "全栈工程师"

            # 删除
            r = await c.delete(f"/v1/memory/profile/{entry_id}?user_id=u1")
            assert r.json()["success"] is True
            assert (await c.get("/v1/memory/profile?user_id=u1")).json()["total"] == 0

    async def test_upsert_validation_400(self, monkeypatch):
        svc = _service()
        app = _api_app(svc)
        import app.api.memory as mem_api
        monkeypatch.setattr(mem_api, "get_service", lambda: svc)
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/v1/memory/profile?user_id=u1", json={
                "slot": "hobby", "key": "k", "value": "v",
            })
            assert r.status_code == 400
            assert "invalid slot" in r.json()["error"]

    async def test_clear_requires_confirm(self, monkeypatch):
        svc = _service()
        app = _api_app(svc)
        import app.api.memory as mem_api
        monkeypatch.setattr(mem_api, "get_service", lambda: svc)
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post("/v1/memory/profile?user_id=u1", json={"slot": "fact", "key": "k", "value": "v"})
            r = await c.post("/v1/memory/profile/clear?user_id=u1", json={})
            assert r.status_code == 400
            r = await c.post("/v1/memory/profile/clear?user_id=u1", json={"confirm": True})
            assert r.json()["deleted"] == 1

    async def test_search_endpoint(self, monkeypatch):
        svc = _service()
        app = _api_app(svc)
        import app.api.memory as mem_api
        monkeypatch.setattr(mem_api, "get_service", lambda: svc)
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post("/v1/memory/profile?user_id=u1", json={
                "slot": "fact", "key": "stack", "value": "Go + Python",
            })
            r = await c.post("/v1/memory/search?user_id=u1", json={"query": "Python"})
            body = r.json()
            assert body["success"] is True
            assert body["count"] == 1
            assert body["results"][0]["key"] == "stack"

    async def test_organize_endpoint(self, monkeypatch):
        svc = _service()
        app = _api_app(svc)
        import app.api.memory as mem_api
        monkeypatch.setattr(mem_api, "get_service", lambda: svc)
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post("/v1/memory/profile?user_id=u1", json={"slot": "fact", "key": "k", "value": "v"})
            r = await c.post("/v1/memory/organize?user_id=u1")
            assert r.json()["success"] is True
            # 轮询直到任务完成（finished_at 判定）
            status = None
            for _ in range(100):
                r = await c.get("/v1/memory/organize/status?user_id=u1")
                status = r.json()["status"]
                if status["finished_at"] or status["result"] or status["error"]:
                    break
                await asyncio.sleep(0.01)
            assert status["running"] is False
            assert status["result"] is not None


# ── 工具层（remember/recall/forget 接入 MemoryService）────

class TestMemoryTools:
    async def test_remember_recall_forget_with_service(self, monkeypatch):
        from app.tools.memory import forget, recall, remember
        svc = _service()
        # 工具层读的是 get_memory_service（主名）；patch get_service 不会生效 ——
        # 名字对不上时工具会静默拿到 None，测试就变成了在测 "service not available"。
        monkeypatch.setattr("app.memory.service.get_memory_service", lambda: svc)
        tool_context.set_tool_context(user_id="u1", tenant_id="t1")
        try:
            r = await remember("timezone", "上海时区 UTC+8", slot="preference")
            assert "Remembered" in r["output"]
            assert r["slot"] == "preference"

            r = await recall("上海")
            assert "timezone" in r["output"]

            r = await recall()
            assert "timezone" in r["output"] and r["count"] == 1

            r = await forget("timezone", slot="preference")
            assert r["deleted"] == 1
        finally:
            tool_context.restore_context({})

    async def test_tools_fail_loud_without_user_context(self, monkeypatch):
        from app.tools.memory import remember
        svc = _service()
        monkeypatch.setattr("app.memory.service.get_memory_service", lambda: svc)
        tool_context.restore_context({})
        r = await remember("k", "v")
        assert "user context" in r["error"]

    async def test_without_service_returns_warning(self, monkeypatch):
        """无 MemoryService 时应如实告知「不可用」，而不是假装记住。

        记忆工具只有 MemoryService 一条链路（旧 MemoryManager 降级只存在于
        prompt_engine 的上下文注入，工具层从未实现），因此这里断言「如实报告」，
        而不是「降级后仍然成功」—— 断言一个从未实现的行为只会掩盖真实行为。
        """
        from app.tools.memory import remember
        monkeypatch.setattr("app.memory.service.get_memory_service", lambda: None)
        tool_context.restore_context({})
        r = await remember("legacy:k", "v")
        assert "not available" in r["output"]
        assert "warning" in r
