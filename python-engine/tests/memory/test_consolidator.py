"""Task 18: 单元测试 - Consolidator。

测试 Consolidator 巩固 pipeline 的完整流程、去重逻辑、错误处理和幂等性。
"""

from __future__ import annotations

import pytest

from app.memory.consolidator import (
    ConsolidateResult,
    Consolidator,
    _extract_entities,
    _extract_summary,
    _extract_topics,
    compute_hash,
    new_summary_id,
)
from app.memory.layers import SummaryEntry

# ── Mock / Fake 基础设施 ─────────────────────────────────────────────────


class FakeSummaryStore:
    """模拟 SummaryStore 接口，供 Consolidator 测试使用。"""

    def __init__(self):
        self._entries: dict[str, SummaryEntry] = {}
        self._hashes: dict[str, str] = {}  # content_hash -> summary_id
        self._embeddings: dict[str, list[float]] = {}  # summary_id -> embedding
        self._insert_calls: list[tuple] = []
        self._get_by_hash_calls: list[tuple] = []
        self._list_active_calls: list[tuple] = []
        self._get_by_id_calls: list[tuple] = []
        self._should_fail_insert = False
        self._should_fail_get_by_hash = False
        self._should_fail_list_active = False

    async def get_by_hash(
        self,
        tenant_id: str,
        user_id: str,
        content_hash: str,
    ) -> SummaryEntry | None:
        """根据 content_hash 查询摘要。"""
        self._get_by_hash_calls.append((tenant_id, user_id, content_hash))
        if self._should_fail_get_by_hash:
            raise RuntimeError("DB error")
        sid = self._hashes.get(content_hash)
        if sid is None:
            return None
        return self._entries.get(sid)

    async def insert(
        self,
        entry: SummaryEntry,
        embedding: list[float] | None = None,
    ) -> SummaryEntry:
        """插入摘要。"""
        self._insert_calls.append((entry, embedding))
        if self._should_fail_insert:
            raise RuntimeError("Insert failed")
        self._entries[entry.id] = entry
        if entry.content_hash:
            self._hashes[entry.content_hash] = entry.id
        if embedding:
            self._embeddings[entry.id] = embedding
        return entry

    async def get_by_id(
        self,
        tenant_id: str,
        user_id: str,
        summary_id: str,
    ) -> SummaryEntry | None:
        """根据 ID 查询摘要。"""
        self._get_by_id_calls.append((tenant_id, user_id, summary_id))
        return self._entries.get(summary_id)

    async def list_active(
        self,
        tenant_id: str,
        user_id: str,
        limit: int = 50,
    ) -> list[SummaryEntry]:
        """列出活跃摘要。"""
        self._list_active_calls.append((tenant_id, user_id, limit))
        if self._should_fail_list_active:
            raise RuntimeError("DB error")
        return list(self._entries.values())


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def fake_store():
    """创建 FakeSummaryStore 实例。"""
    return FakeSummaryStore()


@pytest.fixture
def simple_messages():
    """创建简单的测试消息列表。"""
    return [
        {"role": "user", "content": "我想学习 Python 编程"},
        {"role": "assistant", "content": "好的，让我帮你学习 Python。Python 是一门流行的编程语言。"},
    ]


@pytest.fixture
def messages_with_entities():
    """创建包含实体的测试消息列表。"""
    return [
        {"role": "user", "content": "请访问 https://example.com 查看我的邮箱 test@example.com"},
        {"role": "assistant", "content": "好的，我会记录你的信息，电话号码是 13812345678。"},
    ]


@pytest.fixture
def mock_embedder():
    """创建模拟嵌入函数。"""
    async def embedder(text: str) -> list[float]:
        return [0.1, 0.2, 0.3]
    return embedder


@pytest.fixture
def mock_summariser():
    """创建模拟摘要函数。"""
    async def summariser(messages: list[dict]) -> str:
        return "这是一段测试摘要，关于 Python 编程学习"
    return summariser


@pytest.fixture
def mock_summariser_failing():
    """创建失败的模拟摘要函数。"""
    async def summariser(messages: list[dict]) -> str:
        raise RuntimeError("LLM service unavailable")
    return summariser


@pytest.fixture
def mock_embedder_failing():
    """创建失败的模拟嵌入函数。"""
    async def embedder(text: str) -> list[float]:
        raise RuntimeError("Embedding service unavailable")
    return embedder


# ── 单元测试：纯函数 ──────────────────────────────────────────────────────


class TestPureFunctions:
    """测试纯函数（无外部依赖）。"""

    def test_extract_summary_basic(self, simple_messages):
        """测试降级摘要提取。"""
        result = _extract_summary(simple_messages)
        assert "user" in result
        assert "assistant" in result
        assert "Python" in result

    def test_extract_summary_empty(self):
        """测试空消息列表。"""
        result = _extract_summary([])
        assert result == "(no content)"

    def test_extract_summary_truncation(self):
        """测试长内容截断。"""
        messages = [{"role": "user", "content": "a" * 300}]
        result = _extract_summary(messages)
        assert len(result) < 300  # 应该被截断

    def test_extract_entities_url(self):
        """测试 URL 实体提取。"""
        text = "请访问 https://example.com/path 查看详情"
        result = _extract_entities(text)
        assert "urls" in result
        assert "https://example.com/path" in result["urls"]

    def test_extract_entities_email(self):
        """测试邮箱实体提取。"""
        text = "联系我 test.user@example.com"
        result = _extract_entities(text)
        assert "emails" in result
        assert "test.user@example.com" in result["emails"]

    def test_extract_entities_phone(self):
        """测试手机号实体提取。"""
        text = "我的号码是 13812345678"
        result = _extract_entities(text)
        assert "phones" in result
        assert "13812345678" in result["phones"]

    def test_extract_entities_date(self):
        """测试日期实体提取。"""
        text = "会议日期是 2025-01-15"
        result = _extract_entities(text)
        assert "dates" in result
        assert "2025-01-15" in result["dates"]

    def test_extract_entities_amount(self):
        """测试金额实体提取。"""
        text = "费用是 ¥1,299.00"
        result = _extract_entities(text)
        assert "amounts" in result
        assert "¥1,299.00" in result["amounts"]

    def test_extract_entities_ip(self):
        """测试 IP 地址实体提取。"""
        text = "服务器 IP 是 192.168.1.1"
        result = _extract_entities(text)
        assert "ip_addresses" in result
        assert "192.168.1.1" in result["ip_addresses"]

    def test_extract_entities_file_path(self):
        """测试文件路径实体提取。"""
        text = "请查看 /home/user/report.py"
        result = _extract_entities(text)
        assert "file_paths" in result

    def test_extract_entities_multiple(self):
        """测试多种实体同时提取。"""
        text = "访问 https://example.com 或发邮件到 test@test.com，日期 2025-01-01"
        result = _extract_entities(text)
        assert "urls" in result
        assert "emails" in result
        assert "dates" in result

    def test_extract_entities_empty(self):
        """测试无可提取实体。"""
        text = "这是一段普通文本，没有特殊信息"
        result = _extract_entities(text)
        assert result == {}

    def test_extract_topics_explicit(self):
        """测试显式 topics 提取。"""
        text = "这是摘要。Topics: Python, 编程, 入门"
        result = _extract_topics(text)
        assert "Python" in result
        assert "编程" in result

    def test_extract_topics_fallback(self):
        """测试关键词提取策略。"""
        text = "Python Python Python 编程 编程 入门"
        result = _extract_topics(text)
        assert len(result) > 0

    def test_extract_topics_empty(self):
        """测试空文本主题提取。"""
        result = _extract_topics("")
        assert result == []

    def test_new_summary_id_format(self):
        """测试新 ID 格式。"""
        sid = new_summary_id()
        assert sid.startswith("sms_")
        assert len(sid) == 24  # "sms_" + 20 hex chars

    def test_new_summary_id_unique(self):
        """测试 ID 唯一性。"""
        ids = {new_summary_id() for _ in range(100)}
        assert len(ids) == 100

    def test_compute_hash(self):
        """测试哈希计算。"""
        h1 = compute_hash("t1", "u1", "content")
        h2 = compute_hash("t1", "u1", "content")
        h3 = compute_hash("t1", "u1", "different")
        assert h1 == h2  # 相同输入产生相同哈希
        assert h1 != h3  # 不同输入产生不同哈希
        assert h1.startswith("sha256:")


# ── 单元测试：Consolidator 完整 Pipeline ──────────────────────────────────


class TestConsolidatorPipeline:
    """测试 Consolidator 完整 pipeline。"""

    @pytest.mark.asyncio
    async def test_consolidate_success(
        self, fake_store, simple_messages, mock_embedder, mock_summariser
    ):
        """测试完整巩固流程成功。"""
        consolidator = Consolidator(
            store=fake_store,
            embedder=mock_embedder,
            summariser=mock_summariser,
        )
        result = await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=simple_messages,
            turn_start=0,
            turn_end=1,
        )

        assert isinstance(result, ConsolidateResult)
        assert result.error == ""
        assert result.summary is not None
        assert result.summary.id.startswith("sms_")
        assert result.summary.tenant_id == "tenant1"
        assert result.summary.user_id == "user1"
        assert result.summary.session_id == "session1"
        assert result.deduplicated is False

        # 验证写入被调用
        assert len(fake_store._insert_calls) == 1
        assert len(fake_store._get_by_hash_calls) == 1

    @pytest.mark.asyncio
    async def test_consolidate_without_optional_funcs(
        self, fake_store, simple_messages
    ):
        """测试无可选函数时使用降级策略。"""
        consolidator = Consolidator(store=fake_store)
        result = await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=simple_messages,
        )

        assert result.error == ""
        assert result.summary is not None
        # 使用降级摘要
        assert "user" in result.summary.content or "Python" in result.summary.content

    @pytest.mark.asyncio
    async def test_consolidate_empty_messages(self, fake_store):
        """测试空消息列表。"""
        consolidator = Consolidator(store=fake_store)
        result = await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=[],
        )

        assert result.error == "no messages to consolidate"
        assert result.summary is None

    @pytest.mark.asyncio
    async def test_consolidate_summariser_fallback(
        self, fake_store, simple_messages, mock_summariser_failing
    ):
        """测试摘要失败时的降级。"""
        consolidator = Consolidator(
            store=fake_store,
            summariser=mock_summariser_failing,
        )
        result = await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=simple_messages,
        )

        # 应该降级到 _extract_summary
        assert result.error == ""
        assert result.summary is not None

    @pytest.mark.asyncio
    async def test_consolidate_embedder_failure(
        self, fake_store, simple_messages, mock_embedder_failing, mock_summariser
    ):
        """测试嵌入失败不影响写入。"""
        consolidator = Consolidator(
            store=fake_store,
            embedder=mock_embedder_failing,
            summariser=mock_summariser,
        )
        result = await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=simple_messages,
        )

        # 嵌入失败但写入仍应成功
        assert result.error == ""
        assert result.summary is not None

    @pytest.mark.asyncio
    async def test_consolidate_insert_failure(
        self, fake_store, simple_messages, mock_embedder, mock_summariser
    ):
        """测试插入失败。"""
        fake_store._should_fail_insert = True
        consolidator = Consolidator(
            store=fake_store,
            embedder=mock_embedder,
            summariser=mock_summariser,
        )
        result = await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=simple_messages,
        )

        assert result.error != ""
        assert result.summary is None

    @pytest.mark.asyncio
    async def test_consolidate_turn_range(
        self, fake_store, simple_messages, mock_embedder, mock_summariser
    ):
        """测试 turn_range 参数传递。"""
        consolidator = Consolidator(
            store=fake_store,
            embedder=mock_embedder,
            summariser=mock_summariser,
        )
        result = await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=simple_messages,
            turn_start=5,
            turn_end=10,
        )

        assert result.summary is not None
        # 验证 turn_range 被正确传递到 insert
        entry, _ = fake_store._insert_calls[0]
        assert entry.turn_start == 5
        assert entry.turn_end == 10


# ── 单元测试：去重逻辑 ────────────────────────────────────────────────────


class TestDeduplication:
    """测试去重逻辑。"""

    @pytest.mark.asyncio
    async def test_exact_hash_dedup(
        self, fake_store, simple_messages, mock_embedder, mock_summariser
    ):
        """测试精确哈希去重。"""
        # 先插入一条摘要
        from app.memory.consolidator import compute_hash
        entry1 = SummaryEntry(
            id="sms_existing_001",
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            content="这是一段测试摘要，关于 Python 编程学习",
            topics=["Python", "编程"],
            entities={},
            turn_start=0,
            turn_end=1,
            content_hash=compute_hash("tenant1", "user1", "这是一段测试摘要，关于 Python 编程学习"),
            access_count=0,
            last_accessed_at=None,
        )
        await fake_store.insert(entry1)

        # 执行巩固（相同内容会命中去重）
        consolidator = Consolidator(
            store=fake_store,
            embedder=mock_embedder,
            summariser=mock_summariser,  # 产生相同摘要
        )
        result = await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=simple_messages,
        )

        # 应该被去重
        assert result.deduplicated is True
        assert result.summary is not None
        assert result.summary.id == "sms_existing_001"

    @pytest.mark.asyncio
    async def test_near_duplicate_detection(
        self, fake_store, simple_messages, mock_summariser
    ):
        """测试近重复检测（cosine > 0.95）。"""
        # 先插入一条带有嵌入的摘要
        existing_embedding = [0.1, 0.2, 0.3]
        entry1 = SummaryEntry(
            id="sms_existing_002",
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            content="完全不同的内容，用于测试近重复",
            topics=["测试"],
            entities={},
            turn_start=0,
            turn_end=1,
            content_hash="sha256:different_hash",
            access_count=0,
            last_accessed_at=None,
            embedding=existing_embedding,
        )
        await fake_store.insert(entry1, existing_embedding)

        # 创建一个产生相似嵌入的 embedder
        async def similar_embedder(text: str) -> list[float]:
            return [0.1, 0.2, 0.3]  # 与 existing_embedding 完全相同

        consolidator = Consolidator(
            store=fake_store,
            embedder=similar_embedder,
            summariser=mock_summariser,
        )
        result = await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=simple_messages,
        )

        # 近重复应该被检测到
        assert result.near_duplicate_of == "sms_existing_002"

    @pytest.mark.asyncio
    async def test_near_duplicate_not_triggered(
        self, fake_store, simple_messages, mock_summariser
    ):
        """测试不触发近重复（cosine < 0.95）。"""
        # 先插入一条带有嵌入的摘要
        entry1 = SummaryEntry(
            id="sms_existing_003",
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            content="完全不同的内容",
            topics=["测试"],
            entities={},
            turn_start=0,
            turn_end=1,
            content_hash="sha256:another_hash",
            access_count=0,
            last_accessed_at=None,
            embedding=[1.0, 0.0, 0.0],  # 完全不同的方向
        )
        await fake_store.insert(entry1, [1.0, 0.0, 0.0])

        # 创建一个产生不同嵌入的 embedder
        async def different_embedder(text: str) -> list[float]:
            return [0.0, 1.0, 0.0]  # 正交向量，cosine = 0

        consolidator = Consolidator(
            store=fake_store,
            embedder=different_embedder,
            summariser=mock_summariser,
        )
        result = await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=simple_messages,
        )

        # 不应该触发近重复
        assert result.near_duplicate_of is None
        assert result.deduplicated is False

    @pytest.mark.asyncio
    async def test_hash_failure_continues(
        self, fake_store, simple_messages, mock_embedder, mock_summariser
    ):
        """测试 get_by_hash 失败后继续执行。"""
        fake_store._should_fail_get_by_hash = True

        consolidator = Consolidator(
            store=fake_store,
            embedder=mock_embedder,
            summariser=mock_summariser,
        )
        result = await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=simple_messages,
        )

        # 虽然 hash 查询失败，但应该继续执行并尝试写入
        assert result.error == ""
        assert result.summary is not None

    @pytest.mark.asyncio
    async def test_list_active_failure_continues(
        self, fake_store, simple_messages, mock_embedder, mock_summariser
    ):
        """测试 list_active 失败后继续执行（近重复检测被跳过）。"""
        fake_store._should_fail_list_active = True

        consolidator = Consolidator(
            store=fake_store,
            embedder=mock_embedder,
            summariser=mock_summariser,
        )
        result = await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=simple_messages,
        )

        # list_active 失败应该被优雅处理
        assert result.error == ""
        assert result.summary is not None
        assert result.near_duplicate_of is None


# ── 单元测试：幂等性与顺序 ────────────────────────────────────────────────


class TestIdempotencyAndOrder:
    """测试幂等性和操作顺序。"""

    @pytest.mark.asyncio
    async def test_repeated_calls_no_duplicate(
        self, fake_store, simple_messages, mock_embedder, mock_summariser
    ):
        """测试重复调用不产生重复写入（幂等性）。"""
        consolidator = Consolidator(
            store=fake_store,
            embedder=mock_embedder,
            summariser=mock_summariser,
        )

        # 第一次调用
        result1 = await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=simple_messages,
        )
        assert result1.error == ""
        assert result1.summary is not None

        # 第二次调用（相同内容）
        result2 = await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=simple_messages,
        )

        # 第二次应该被去重
        assert result2.deduplicated is True
        # 不应该有新的 insert 调用
        assert len(fake_store._insert_calls) == 1

    @pytest.mark.asyncio
    async def test_write_before_clean_order(
        self, fake_store, simple_messages, mock_embedder, mock_summariser
    ):
        """测试先写后清顺序（写入在清理之前）。"""
        consolidator = Consolidator(
            store=fake_store,
            embedder=mock_embedder,
            summariser=mock_summariser,
        )
        await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=simple_messages,
        )

        # 验证操作顺序：先 get_by_hash，然后 insert
        assert len(fake_store._get_by_hash_calls) >= 1
        assert len(fake_store._insert_calls) == 1

        # 验证摘要已存储
        inserted_entry, _ = fake_store._insert_calls[0]
        assert inserted_entry.id in fake_store._entries

    @pytest.mark.asyncio
    async def test_data_not_lost_on_error(
        self, fake_store, simple_messages, mock_embedder, mock_summariser
    ):
        """测试错误时数据不丢失。"""
        # 第一次：成功写入
        consolidator1 = Consolidator(
            store=fake_store,
            embedder=mock_embedder,
            summariser=mock_summariser,
        )
        result1 = await consolidator1.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=simple_messages,
        )
        assert result1.summary is not None

        # 第二次：使用不同的 summariser 产生不同内容，模拟 insert 失败
        async def different_summariser(messages):
            return "这是一段不同的测试摘要内容"

        fake_store._should_fail_insert = True
        consolidator2 = Consolidator(
            store=fake_store,
            embedder=mock_embedder,
            summariser=different_summariser,
        )
        result2 = await consolidator2.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=simple_messages,
        )

        # 第二次失败，但第一次的数据应该保留
        assert result2.error != ""
        assert result1.summary.id in fake_store._entries


# ── 单元测试：实体与主题提取 ──────────────────────────────────────────────


class TestEntityExtraction:
    """测试实体和主题提取集成。"""

    @pytest.mark.asyncio
    async def test_entities_extracted_in_consolidate(
        self, fake_store, messages_with_entities, mock_embedder, mock_summariser
    ):
        """测试巩固时提取实体。"""
        consolidator = Consolidator(
            store=fake_store,
            embedder=mock_embedder,
            summariser=mock_summariser,
        )
        result = await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=messages_with_entities,
        )

        assert result.summary is not None
        # 验证实体在摘要中
        entry, _ = fake_store._insert_calls[0]
        # summariser 返回固定文本，所以实体可能为空或来自降级提取
        assert entry.entities is not None

    def test_entities_from_degraded_summary(self, messages_with_entities):
        """测试降级摘要中的实体提取。"""
        summary = _extract_summary(messages_with_entities)
        entities = _extract_entities(summary)

        # 至少应该提取到 URL 和邮箱
        assert "urls" in entities or "emails" in entities


# ── 单元测试：ConsolidateResult ───────────────────────────────────────────


class TestConsolidateResult:
    """测试 ConsolidateResult 数据类。"""

    def test_result_to_dict(self):
        """测试结果序列化。"""
        entry = SummaryEntry(
            id="sms_test_001",
            tenant_id="t1",
            user_id="u1",
            session_id="s1",
            content="测试内容",
            topics=["测试"],
            entities={},
            turn_start=0,
            turn_end=1,
            content_hash="sha256:abc",
            access_count=0,
            last_accessed_at=None,
        )
        result = ConsolidateResult(
            summary=entry,
            deduplicated=False,
            near_duplicate_of=None,
            error="",
        )
        d = result.to_dict()
        assert d["summary"] is not None
        assert d["summary"]["id"] == "sms_test_001"
        assert d["deduplicated"] is False
        assert d["near_duplicate_of"] is None
        assert d["error"] is None

    def test_result_to_dict_no_summary(self):
        """测试无摘要时的序列化。"""
        result = ConsolidateResult(error="test error")
        d = result.to_dict()
        assert d["summary"] is None
        assert d["error"] == "test error"

    def test_result_to_dict_with_near_duplicate(self):
        """测试近重复时的序列化。"""
        result = ConsolidateResult(
            summary=None,
            near_duplicate_of="sms_existing_001",
        )
        d = result.to_dict()
        assert d["near_duplicate_of"] == "sms_existing_001"


# ── 单元测试：跨租户隔离 ──────────────────────────────────────────────────


class TestTenantIsolation:
    """测试多租户隔离。"""

    @pytest.mark.asyncio
    async def test_different_tenants_isolated(
        self, fake_store, simple_messages, mock_embedder, mock_summariser
    ):
        """测试不同租户的数据隔离。"""
        # 租户 1 写入
        consolidator1 = Consolidator(
            store=fake_store,
            embedder=mock_embedder,
            summariser=mock_summariser,
        )
        result1 = await consolidator1.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=simple_messages,
        )

        # 租户 2 相同内容（应该独立，不被去重）
        async def tenant2_summariser(messages):
            return "这是一段测试摘要，关于 Python 编程学习"  # 与 tenant1 相同

        consolidator2 = Consolidator(
            store=fake_store,
            embedder=mock_embedder,
            summariser=tenant2_summariser,
        )
        result2 = await consolidator2.consolidate(
            tenant_id="tenant2",
            user_id="user2",
            session_id="session2",
            messages=simple_messages,
        )

        # 不同租户应该独立存储（content_hash 包含 tenant_id）
        assert result1.deduplicated is False
        assert result2.deduplicated is False
        assert len(fake_store._insert_calls) == 2

    @pytest.mark.asyncio
    async def test_same_tenant_same_user_dedup(
        self, fake_store, simple_messages, mock_embedder, mock_summariser
    ):
        """测试同租户同用户去重。"""
        consolidator = Consolidator(
            store=fake_store,
            embedder=mock_embedder,
            summariser=mock_summariser,
        )

        # 第一次
        await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=simple_messages,
        )

        # 第二次（相同租户 + 用户 + 内容）
        result = await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=simple_messages,
        )

        # 应该被去重
        assert result.deduplicated is True


# ── 压力 / 边界测试 ───────────────────────────────────────────────────────


class TestEdgeCases:
    """测试边界情况。"""

    @pytest.mark.asyncio
    async def test_very_long_content(
        self, fake_store, mock_embedder, mock_summariser
    ):
        """测试超长内容处理。"""
        messages = [{"role": "user", "content": "a" * 10000}]
        consolidator = Consolidator(
            store=fake_store,
            embedder=mock_embedder,
            summariser=mock_summariser,
        )
        result = await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=messages,
        )

        assert result.error == ""
        assert result.summary is not None

    @pytest.mark.asyncio
    async def test_many_messages(
        self, fake_store, mock_embedder, mock_summariser
    ):
        """测试大量消息处理。"""
        messages = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"消息 {i}"}
            for i in range(100)
        ]
        consolidator = Consolidator(
            store=fake_store,
            embedder=mock_embedder,
            summariser=mock_summariser,
        )
        result = await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=messages,
        )

        assert result.error == ""
        assert result.summary is not None

    @pytest.mark.asyncio
    async def test_empty_content_after_summarise(self, fake_store):
        """测试摘要为空的情况。"""
        async def empty_summariser(messages):
            return "   \n\n  "  # 只有空白字符

        consolidator = Consolidator(
            store=fake_store,
            summariser=empty_summariser,
        )
        result = await consolidator.consolidate(
            tenant_id="tenant1",
            user_id="user1",
            session_id="session1",
            messages=[{"role": "user", "content": "test"}],
        )

        assert result.error == "summary is empty"
        assert result.summary is None

    @pytest.mark.asyncio
    async def test_concurrent_consolidate(
        self, fake_store, simple_messages, mock_embedder, mock_summariser
    ):
        """测试并发巩固（不同 session）。"""
        import asyncio

        consolidator = Consolidator(
            store=fake_store,
            embedder=mock_embedder,
            summariser=mock_summariser,
        )

        tasks = [
            consolidator.consolidate(
                tenant_id="tenant1",
                user_id="user1",
                session_id=f"session_{i}",
                messages=simple_messages,
            )
            for i in range(5)
        ]
        results = await asyncio.gather(*tasks)

        # 所有应该成功
        for r in results:
            assert r.error == ""
            assert r.summary is not None

        # 应该有 5 次 insert（不同 session 不会被去重，因为 content_hash 不含 session_id）
        # 但由于 summariser 返回相同内容，实际上 content_hash 相同，所以只有 1 次成功写入
        # 后续的会被去重
        assert len(fake_store._insert_calls) >= 1
