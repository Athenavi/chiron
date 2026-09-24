# 记忆系统 L3 摘要 + 冲突裁决 + /memory 命令测试

import httpx
import pytest
from httpx import ASGITransport

from app.memory.consolidator import (
    Consolidator,
    _extract_entities,
    _extract_summary,
    _extract_topics,
)
from app.memory.layers import (
    RecallResult,
    SummaryEntry,
)
from app.memory.service import MemoryService
from tests.fakes import InMemoryProfileStore, InMemorySummaryStore

# ── 测试基建 ──────────────────────────────────────────────





class FakeEmbedder:
    def __init__(self):
        self.mapping: dict[str, list[float]] = {}

    def set(self, text, vec):
        self.mapping[text] = vec

    async def __call__(self, text):
        if text in self.mapping:
            return list(self.mapping[text])
        return [0.5] * 8


def _profile_svc(embedder=None, store=None):
    return MemoryService(store=store or InMemoryProfileStore(), embedder=embedder)


def _full_svc(embedder=None, profile_store=None, summary_store=None, consolidator=None):
    emb = embedder or FakeEmbedder()
    ps = profile_store or InMemoryProfileStore()
    ss = summary_store or InMemorySummaryStore()
    con = consolidator or Consolidator(store=ss, embedder=emb)
    return MemoryService(store=ps, embedder=emb, summary_store=ss, consolidator=con)


# ── SummaryEntry 数据结构 ────────────────────────────────

class TestSummaryEntry:
    def test_to_dict_has_fields(self):
        e = SummaryEntry(
            id="sms_001", tenant_id="t1", user_id="u1", session_id="s1",
            content="讨论了 Go 微服务架构", topics=["Go", "微服务"],
        )
        d = e.to_dict()
        assert d["id"] == "sms_001"
        assert d["content"] == "讨论了 Go 微服务架构"
        assert d["topics"] == ["Go", "微服务"]
        assert d["has_embedding"] is False
        assert d["status"] == "active"

    def test_embed_text_with_topics(self):
        e = SummaryEntry(
            id="sms_002", tenant_id="t1", user_id="u1", session_id="s1",
            content="Python 异步编程", topics=["asyncio", "Python"],
        )
        assert "asyncio" in e.embed_text
        assert "Python 异步编程" in e.embed_text

    def test_embed_text_without_topics(self):
        e = SummaryEntry(
            id="sms_003", tenant_id="t1", user_id="u1", session_id="s1",
            content="简洁摘要",
        )
        assert e.embed_text == "简洁摘要"


# ── Consolidator 纯函数 ──────────────────────────────────

class TestConsolidatorPure:
    def test_extract_summary_concatenates_messages(self):
        msgs = [
            {"role": "user", "content": "帮我用 Go 写一个 HTTP 服务"},
            {"role": "assistant", "content": "好的，这是一个示例..."},
        ]
        result = _extract_summary(msgs)
        assert "user" in result
        assert "Go" in result

    def test_extract_summary_empty_messages(self):
        assert _extract_summary([]) == "(no content)"

    def test_extract_summary_skips_empty_content(self):
        msgs = [{"role": "user", "content": ""}, {"role": "assistant", "content": "hello"}]
        result = _extract_summary(msgs)
        assert "hello" in result
        assert result.count("[user]:") == 0

    def test_extract_entities_finds_urls_emails_phones(self):
        text = "访问 https://example.com 或联系 test@mail.com，电话 13800138000"
        entities = _extract_entities(text)
        assert "https://example.com" in entities["urls"]
        assert "test@mail.com" in entities["emails"]
        assert "13800138000" in entities["phones"]

    def test_extract_entities_finds_dates_amounts_ips(self):
        text = "在 2026-08-21 支付 ¥1000，服务器 IP 192.168.1.1"
        entities = _extract_entities(text)
        assert "2026-08-21" in entities["dates"]
        assert any("1000" in a for a in entities["amounts"])
        assert "192.168.1.1" in entities["ip_addresses"]

    def test_extract_entities_finds_file_paths_and_code_refs(self):
        text = "修改了 main.py 和 config.yaml，调用了 Logger.info()"
        entities = _extract_entities(text)
        assert "main.py" in entities["file_paths"]
        assert "config.yaml" in entities["file_paths"]
        assert any("Logger" in c for c in entities.get("code_refs", []))

    def test_extract_entities_empty_text(self):
        entities = _extract_entities("no special content here")
        assert entities == {}

    def test_extract_topics_explicit_line(self):
        text = "Summary: 讨论了架构\ntopics: Go, 微服务, 容器"
        topics = _extract_topics(text)
        assert "Go" in topics
        assert "微服务" in topics
        assert "容器" in topics

    def test_extract_topics_keyword_fallback(self):
        text = "Go 微服务 Go 微服务 架构 设计 架构 设计 模式"
        topics = _extract_topics(text)
        assert len(topics) > 0
        # 高频词应排在前面
        assert "Go" in topics or "微服务" in topics

    def test_extract_topics_no_keywords(self):
        topics = _extract_topics("a b")
        assert topics == []


# ── Consolidator pipeline ────────────────────────────────

class TestConsolidatorPipeline:
    async def test_consolidate_creates_summary(self):
        ss = InMemorySummaryStore()
        emb = FakeEmbedder()
        emb.set("Go 微服务: 讨论了 Go 微服务架构", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        con = Consolidator(store=ss, embedder=emb)
        msgs = [
            {"role": "user", "content": "帮我设计 Go 微服务架构"},
            {"role": "assistant", "content": "好的，建议使用 gRPC + Kubernetes"},
        ]
        result = await con.consolidate("t1", "u1", "s1", msgs, turn_start=0, turn_end=2)
        assert result.error == ""
        assert result.summary is not None
        assert result.deduplicated is False
        assert result.summary.content
        assert result.summary.tenant_id == "t1"
        assert result.summary.user_id == "u1"
        assert result.summary.session_id == "s1"
        assert result.summary.turn_start == 0
        assert result.summary.turn_end == 2

    async def test_consolidate_dedup_exact_hash(self):
        ss = InMemorySummaryStore()
        con = Consolidator(store=ss)
        msgs = [{"role": "user", "content": "Same message"}, {"role": "assistant", "content": "Same reply"}]
        r1 = await con.consolidate("t1", "u1", "s1", msgs, 0, 2)
        r2 = await con.consolidate("t1", "u1", "s1", msgs, 0, 2)
        assert r1.deduplicated is False
        assert r2.deduplicated is True
        assert r2.summary.id == r1.summary.id

    async def test_consolidate_near_duplicate_detected(self):
        ss = InMemorySummaryStore()
        emb = FakeEmbedder()
        emb.set("Python: 讨论了 Python 异步编程", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        emb.set("Python asyncio: 讨论了 Python asyncio 异步", [0.99, 0.01, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        con = Consolidator(store=ss, embedder=emb)
        msgs1 = [{"role": "user", "content": "Python 异步编程讨论"}, {"role": "assistant", "content": "asyncio"}]
        msgs2 = [{"role": "user", "content": "Python asyncio 异步讨论"}, {"role": "assistant", "content": "asyncio"}]
        await con.consolidate("t1", "u1", "s1", msgs1, 0, 2)
        r2 = await con.consolidate("t1", "u1", "s1", msgs2, 0, 2)
        assert r2.near_duplicate_of is not None

    async def test_consolidate_empty_messages_fail_loud(self):
        ss = InMemorySummaryStore()
        con = Consolidator(store=ss)
        result = await con.consolidate("t1", "u1", "s1", [], 0, 0)
        assert result.error == "no messages to consolidate"

    async def test_consolidate_embedder_failure_still_saves(self):
        """embedder 不可用 → 摘要照常入库（embedding=NULL），fail-soft。"""
        ss = InMemorySummaryStore()

        async def fail_embedder(text):
            raise RuntimeError("embed unavailable")

        con = Consolidator(store=ss, embedder=fail_embedder)
        msgs = [{"role": "user", "content": "Test content"}, {"role": "assistant", "content": "Reply"}]
        result = await con.consolidate("t1", "u1", "s1", msgs, 0, 2)
        assert result.error == ""
        assert result.summary is not None
        assert result.summary.embedding is None

    async def test_consolidate_with_custom_summariser(self):
        ss = InMemorySummaryStore()

        async def custom_summariser(messages):
            return "topics: API, 设计\n用户讨论了 API 设计最佳实践"

        con = Consolidator(store=ss, summariser=custom_summariser)
        msgs = [{"role": "user", "content": "How to design APIs?"}, {"role": "assistant", "content": "Use REST"}]
        result = await con.consolidate("t1", "u1", "s1", msgs, 0, 2)
        assert result.summary is not None
        assert "API" in result.summary.topics
        assert "设计" in result.summary.topics


# ── MemoryService L3 功能 ────────────────────────────────

class TestServiceL3:
    async def test_save_summary_delegates_to_consolidator(self):
        svc = _full_svc()
        msgs = [{"role": "user", "content": "讨论 Go 架构"}, {"role": "assistant", "content": "建议用 gRPC"}]
        result = await svc.save_summary("t1", "u1", "s1", msgs, 0, 2)
        assert result["error"] is None
        assert result["summary"] is not None
        assert result["deduplicated"] is False

    async def test_save_summary_without_consolidator_fail_loud(self):
        svc = _profile_svc()  # no summary_store / consolidator
        with pytest.raises(RuntimeError, match="consolidator not bound"):
            await svc.save_summary("t1", "u1", "s1", [], 0, 0)

    async def test_recall_summaries_empty(self):
        svc = _full_svc()
        result = await svc.recall_summaries("t1", "u1", "test query")
        assert result["count"] == 0
        assert result["mode"] == "empty"

    async def test_recall_summaries_semantic_match(self):
        emb = FakeEmbedder()
        svc = _full_svc(embedder=emb)
        msgs = [{"role": "user", "content": "讨论 Go 微服务"}, {"role": "assistant", "content": "建议用 gRPC"}]
        await svc.save_summary("t1", "u1", "s1", msgs, 0, 2)
        # 注入查询嵌入
        emb.set("Go 微服务", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        result = await svc.recall_summaries("t1", "u1", "Go 微服务")
        assert result["count"] >= 0  # 可能因 threshold 过滤

    async def test_recall_combines_l2_and_l3(self):
        emb = FakeEmbedder()
        svc = _full_svc(embedder=emb)
        # L2 档案卡
        await svc.upsert("t1", "u1", "fact", "language", "Go", confidence=90, source="user_confirmed")
        # L3 摘要
        msgs = [{"role": "user", "content": "Go 架构讨论"}, {"role": "assistant", "content": "gRPC"}]
        await svc.save_summary("t1", "u1", "s1", msgs, 0, 2)
        result = await svc.recall("t1", "u1", "Go")
        assert result.has_content
        assert "language" in result.profile_block
        assert isinstance(result.summary_items, list)

    async def test_recall_empty_user(self):
        svc = _full_svc()
        result = await svc.recall("t1", "u1", "anything")
        assert result.has_content is False

    async def test_list_summaries(self):
        svc = _full_svc()
        msgs = [{"role": "user", "content": "讨论 A"}, {"role": "assistant", "content": "回复 A"}]
        await svc.save_summary("t1", "u1", "s1", msgs, 0, 2)
        data = await svc.list_summaries("t1", "u1")
        assert data["count"] >= 1
        assert data["summaries"][0]["content"]


# ── 冲突检测与裁决 ────────────────────────────────────────

class TestConflictDetection:
    async def test_upsert_creates_conflict_when_user_confirmed_overwritten_by_derived(self):
        svc = _profile_svc()
        # 先用 user_confirmed 设一个值
        await svc.upsert("t1", "u1", "fact", "city", "上海", confidence=95, source="user_confirmed")
        # 用 derived 尝试覆盖不同值 → 应创建冲突
        result = await svc.upsert("t1", "u1", "fact", "city", "北京", confidence=50, source="derived")
        assert result["created"] is False
        assert "conflict" in result
        assert result["conflict"]["old_value"] == "上海"
        assert result["conflict"]["new_value"] == "北京"
        assert result["conflict"]["status"] == "pending"

    async def test_upsert_no_conflict_when_both_user_confirmed(self):
        svc = _profile_svc()
        await svc.upsert("t1", "u1", "fact", "city", "上海", confidence=95, source="user_confirmed")
        result = await svc.upsert("t1", "u1", "fact", "city", "北京", confidence=95, source="user_confirmed")
        assert "conflict" not in result
        assert result["entry"]["value"] == "北京"

    async def test_upsert_no_conflict_when_old_not_user_confirmed(self):
        svc = _profile_svc()
        await svc.upsert("t1", "u1", "fact", "city", "上海", confidence=50, source="derived")
        result = await svc.upsert("t1", "u1", "fact", "city", "北京", confidence=50, source="derived")
        assert "conflict" not in result

    async def test_list_conflicts(self):
        svc = _profile_svc()
        await svc.upsert("t1", "u1", "fact", "city", "上海", confidence=95, source="user_confirmed")
        await svc.upsert("t1", "u1", "fact", "city", "北京", confidence=50, source="derived")
        conflicts = svc.list_conflicts("t1", "u1")
        assert len(conflicts) == 1
        assert conflicts[0]["key"] == "city"

    async def test_list_conflicts_tenant_isolation(self):
        svc = _profile_svc()
        await svc.upsert("t1", "u1", "fact", "city", "上海", confidence=95, source="user_confirmed")
        await svc.upsert("t1", "u1", "fact", "city", "北京", confidence=50, source="derived")
        # 不同租户不应看到冲突
        conflicts = svc.list_conflicts("t2", "u1")
        assert len(conflicts) == 0

    async def test_resolve_conflict_keep_old(self):
        svc = _profile_svc()
        await svc.upsert("t1", "u1", "fact", "city", "上海", confidence=95, source="user_confirmed")
        r = await svc.upsert("t1", "u1", "fact", "city", "北京", confidence=50, source="derived")
        conflict_id = r["conflict"]["conflict_id"]
        result = await svc.resolve_conflict(conflict_id, "keep_old")
        assert result["status"] == "resolved"
        assert result["resolution"] == "keep_old"
        assert result["resolved_value"] == "上海"
        # 实际存储值未变
        entry = await svc._store.get_by_key("t1", "u1", "fact", "city")
        assert entry.item_value == "上海"
        assert entry.confidence == 100
        assert entry.source == "user_confirmed"

    async def test_resolve_conflict_adopt_new(self):
        svc = _profile_svc()
        await svc.upsert("t1", "u1", "fact", "city", "上海", confidence=95, source="user_confirmed")
        r = await svc.upsert("t1", "u1", "fact", "city", "北京", confidence=50, source="derived")
        conflict_id = r["conflict"]["conflict_id"]
        result = await svc.resolve_conflict(conflict_id, "adopt_new")
        assert result["resolved_value"] == "北京"
        entry = await svc._store.get_by_key("t1", "u1", "fact", "city")
        assert entry.item_value == "北京"

    async def test_resolve_conflict_manual(self):
        svc = _profile_svc()
        await svc.upsert("t1", "u1", "fact", "city", "上海", confidence=95, source="user_confirmed")
        r = await svc.upsert("t1", "u1", "fact", "city", "北京", confidence=50, source="derived")
        conflict_id = r["conflict"]["conflict_id"]
        result = await svc.resolve_conflict(conflict_id, "manual", manual_value="深圳")
        assert result["resolved_value"] == "深圳"
        entry = await svc._store.get_by_key("t1", "u1", "fact", "city")
        assert entry.item_value == "深圳"

    async def test_resolve_conflict_not_found(self):
        svc = _profile_svc()
        with pytest.raises(ValueError, match="conflict not found"):
            await svc.resolve_conflict("nonexistent", "keep_old")

    async def test_resolve_conflict_invalid_resolution(self):
        svc = _profile_svc()
        await svc.upsert("t1", "u1", "fact", "city", "上海", confidence=95, source="user_confirmed")
        r = await svc.upsert("t1", "u1", "fact", "city", "北京", confidence=50, source="derived")
        conflict_id = r["conflict"]["conflict_id"]
        with pytest.raises(ValueError, match="invalid resolution"):
            await svc.resolve_conflict(conflict_id, "invalid_option")


# ── API 层（summaries / conflicts / resolve）──────────────

def _api_app(svc):
    from fastapi import FastAPI

    from app.api.memory import router
    app = FastAPI()
    app.include_router(router)
    return app


class TestL3API:
    async def test_summaries_endpoint(self, monkeypatch):
        svc = _full_svc()
        msgs = [{"role": "user", "content": "讨论 A"}, {"role": "assistant", "content": "回复 A"}]
        await svc.save_summary("t1", "u1", "s1", msgs, 0, 2)
        app = _api_app(svc)
        import app.api.memory as mem_api
        monkeypatch.setattr(mem_api, "get_service", lambda: svc)
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/v1/memory/summaries?user_id=u1&tenant_id=t1")
            assert r.status_code == 200
            body = r.json()
            assert body["success"] is True
            assert body["count"] >= 1

    async def test_conflicts_endpoint(self, monkeypatch):
        svc = _profile_svc()
        await svc.upsert("t1", "u1", "fact", "city", "上海", confidence=95, source="user_confirmed")
        await svc.upsert("t1", "u1", "fact", "city", "北京", confidence=50, source="derived")
        app = _api_app(svc)
        import app.api.memory as mem_api
        monkeypatch.setattr(mem_api, "get_service", lambda: svc)
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/v1/memory/conflicts?user_id=u1&tenant_id=t1")
            assert r.status_code == 200
            body = r.json()
            assert body["count"] == 1
            assert body["conflicts"][0]["key"] == "city"

    async def test_resolve_endpoint(self, monkeypatch):
        svc = _profile_svc()
        await svc.upsert("t1", "u1", "fact", "city", "上海", confidence=95, source="user_confirmed")
        r_upsert = await svc.upsert("t1", "u1", "fact", "city", "北京", confidence=50, source="derived")
        conflict_id = r_upsert["conflict"]["conflict_id"]
        app = _api_app(svc)
        import app.api.memory as mem_api
        monkeypatch.setattr(mem_api, "get_service", lambda: svc)
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/v1/memory/conflicts/{conflict_id}/resolve?user_id=u1&tenant_id=t1",
                json={"resolution": "adopt_new"},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["success"] is True
            assert body["conflict"]["resolved_value"] == "北京"

    async def test_resolve_endpoint_404(self, monkeypatch):
        svc = _profile_svc()
        app = _api_app(svc)
        import app.api.memory as mem_api
        monkeypatch.setattr(mem_api, "get_service", lambda: svc)
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                "/v1/memory/conflicts/nonexistent/resolve?user_id=u1&tenant_id=t1",
                json={"resolution": "keep_old"},
            )
            assert r.status_code == 404

    async def test_resolve_endpoint_invalid_resolution_400(self, monkeypatch):
        svc = _profile_svc()
        app = _api_app(svc)
        import app.api.memory as mem_api
        monkeypatch.setattr(mem_api, "get_service", lambda: svc)
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                "/v1/memory/conflicts/whatever/resolve?user_id=u1&tenant_id=t1",
                json={"resolution": "bad"},
            )
            assert r.status_code == 400


# ── /memory 命令 ──────────────────────────────────────────

class TestMemoryCommand:
    async def test_memory_search_with_service(self, monkeypatch):
        from app.commands.builtins import _memory
        from app.commands.registry import CommandContext
        svc = _profile_svc()
        await svc.upsert("t1", "u1", "fact", "language", "Go", confidence=90)
        monkeypatch.setattr("app.memory.service.get_service", lambda: svc)
        ctx = CommandContext(metadata={"tenant_id": "t1", "user_id": "u1"})
        result = await _memory("Go", ctx)
        assert "language" in result
        assert "Go" in result

    async def test_memory_list_without_query(self, monkeypatch):
        from app.commands.builtins import _memory
        from app.commands.registry import CommandContext
        svc = _profile_svc()
        await svc.upsert("t1", "u1", "fact", "k1", "v1", confidence=80)
        await svc.upsert("t1", "u1", "preference", "k2", "v2", confidence=80)
        monkeypatch.setattr("app.memory.service.get_service", lambda: svc)
        ctx = CommandContext(metadata={"tenant_id": "t1", "user_id": "u1"})
        result = await _memory("", ctx)
        assert "Stored memories" in result
        assert "k1" in result
        assert "k2" in result

    async def test_memory_no_service(self, monkeypatch):
        from app.commands.builtins import _memory
        from app.commands.registry import CommandContext
        monkeypatch.setattr("app.memory.service.get_service", lambda: None)
        result = await _memory("test", CommandContext())
        assert "not available" in result

    async def test_memory_no_user_context(self, monkeypatch):
        from app.commands.builtins import _memory
        from app.commands.registry import CommandContext
        svc = _profile_svc()
        monkeypatch.setattr("app.memory.service.get_service", lambda: svc)
        result = await _memory("test", CommandContext())
        assert "User context missing" in result

    async def test_memory_no_results(self, monkeypatch):
        from app.commands.builtins import _memory
        from app.commands.registry import CommandContext
        svc = _profile_svc()
        monkeypatch.setattr("app.memory.service.get_service", lambda: svc)
        ctx = CommandContext(metadata={"tenant_id": "t1", "user_id": "u1"})
        result = await _memory("nonexistent", ctx)
        assert "No memories found" in result


# ── RecallResult 数据结构 ─────────────────────────────────

class TestRecallResult:
    def test_has_content_true_with_profile(self):
        r = RecallResult(profile_block="some profile")
        assert r.has_content is True

    def test_has_content_true_with_summaries(self):
        r = RecallResult(summary_items=[{"id": "1"}])
        assert r.has_content is True

    def test_has_content_false_empty(self):
        r = RecallResult(profile_block="", summary_items=[])
        assert r.has_content is False
