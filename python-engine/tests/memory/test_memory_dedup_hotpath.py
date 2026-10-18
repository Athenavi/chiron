"""M12：记忆相似度/近重复**热路径**的回归。

改造只有一个承诺：**省掉重复计算，不改变任何结论**。所以每条测试都要么拿
"改造前的实现"当对照跑一遍同样输入，要么直接钉住"这一趟不许碰什么"：

1. `CosineScorer` / `unit_vector` + `dot_product` 与 `cosine_similarity` 逐条相等；
2. `_plan_near_duplicate_merge` 与改造前 `organize_now` ② 的那段成对循环给出
   完全相同的「谁被并掉、谁留下」（含置信度取舍、并列留先入库、无向量条目）；
3. 容量淘汰不许再走带回向量的 `list()`（每次写都要跑一趟，白解析 200 条向量）；
4. 整理的成对比较运行在非事件循环线程上，期间前台照样被调度。

阈值、排序、`duplicate_of` 提示的对外语义都没动 —— 上面第 3、4 条正是为防止
"优化顺手改了行为"而留的。
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from app.memory import service as svc_mod
from app.memory.layers import (
    CosineScorer,
    MemoryEntry,
    SummaryEntry,
    cosine_similarity,
    dot_product,
    unit_vector,
    vector_norm,
)
from app.memory.profile import EvictionRow, ProfileStore
from app.memory.service import NEAR_DUPLICATE_THRESHOLD, MemoryService
from tests.fakes import InMemoryProfileStore

DIM = 64


def _vec(rng: random.Random, dim: int = DIM) -> list[float]:
    return [rng.uniform(-1.0, 1.0) for _ in range(dim)]


def _perturbed(rng: random.Random, base: list[float], eps: float = 0.01) -> list[float]:
    """与 base 几乎同向（cosine ≈ 1）—— 近重复用例的构造方式。"""
    return [x + rng.uniform(-eps, eps) for x in base]


def _entry(
    entry_id: str,
    embedding: list[float] | None,
    confidence: int = 50,
    slot: str = "fact",
    key: str = "k",
) -> MemoryEntry:
    return MemoryEntry(
        id=entry_id,
        tenant_id="t",
        user_id="u",
        slot=slot,
        item_key=key,
        item_value=f"v-{entry_id}",
        confidence=confidence,
        source="derived",
        embedding=embedding,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


# ── 1. 算分工具与 cosine_similarity 等价 ────────────────────────────────


def test_scorer_matches_cosine_similarity():
    rng = random.Random(11)
    q = _vec(rng)
    scorer = CosineScorer(q)
    for _ in range(50):
        cand = _vec(rng)
        assert scorer.score(cand) == pytest.approx(cosine_similarity(q, cand), abs=1e-12)


def test_unit_dot_matches_cosine_similarity():
    """成对比较的替代写法：两侧各单位化一次 + 点积，结论必须一致（余弦尺度不变）。"""
    rng = random.Random(12)
    for _ in range(50):
        a, b = _vec(rng), _vec(rng)
        ua, ub = unit_vector(a), unit_vector(b)
        assert dot_product(ua, ub) == pytest.approx(cosine_similarity(a, b), abs=1e-12)


def test_degenerate_vectors_stay_zero_on_every_path():
    zero, empty, short = [0.0] * DIM, [], [1.0, 2.0]
    base = _vec(random.Random(13))
    assert cosine_similarity(base, zero) == 0.0
    assert CosineScorer(base).score(zero) == 0.0
    assert unit_vector(zero) is None
    assert cosine_similarity(base, empty) == 0.0
    assert CosineScorer(base).score(empty) == 0.0
    # 维度不一致：两条路径都必须判"不相似"，而不是按短的那个截断后硬算
    assert cosine_similarity(base, short) == 0.0
    assert CosineScorer(base).score(short) == 0.0
    assert dot_product(base, short) == 0.0
    assert vector_norm([3.0, 4.0]) == 5.0
    assert CosineScorer(zero).score(base) == 0.0  # query 自己是零向量


# ── 2. 合并计划与改造前的循环逐输入等价 ─────────────────────────────────


def _legacy_merge(items: list[MemoryEntry]) -> tuple[list[MemoryEntry], list[MemoryEntry]]:
    """改造前 `organize_now` ② 那段循环的逐字复刻（只算，不落库）。

    保留它就是为了有一份**不是**用新写法写的对照物；哪天两边结论分叉，
    下面的用例必须失败。
    """
    survivors: list[MemoryEntry] = []
    victims: list[MemoryEntry] = []
    for item in items:
        if not item.embedding:
            survivors.append(item)
            continue
        duplicate_of = None
        for kept in survivors:
            if not kept.embedding:
                continue
            if cosine_similarity(item.embedding, kept.embedding) <= NEAR_DUPLICATE_THRESHOLD:
                continue
            duplicate_of = kept
            break
        if duplicate_of is None:
            survivors.append(item)
            continue
        if item.confidence > duplicate_of.confidence:
            victims.append(duplicate_of)
            survivors = [s for s in survivors if s is not duplicate_of]
            survivors.append(item)
        else:
            victims.append(item)
    return victims, survivors


def _corpus(seed: int) -> list[MemoryEntry]:
    """造一批混合现场：真重复簇、无关条目、没向量的、维度不对的、置信度并列的。"""
    rng = random.Random(seed)
    items: list[MemoryEntry] = []
    n = 0
    for _ in range(4):
        anchor = _vec(rng)
        for _ in range(rng.randint(1, 3)):
            items.append(_entry(f"e{n}", _perturbed(rng, anchor), confidence=rng.choice([40, 50, 90])))
            n += 1
        items.append(_entry(f"e{n}", _vec(rng), confidence=rng.choice([10, 50, 80])))  # 簇外的无关项
        n += 1
    items.append(_entry(f"e{n}", None, confidence=99))  # 没向量：永不参与比较
    n += 1
    items.append(_entry(f"e{n}", [1.0, 0.0], confidence=70))  # 维度与其他条目不一致
    n += 1
    items.append(_entry(f"e{n}", [0.0] * DIM, confidence=70))  # 零向量
    rng.shuffle(items)
    return items


@pytest.mark.parametrize("seed", range(25))
def test_merge_plan_matches_legacy_loop(seed: int):
    items = _corpus(seed)
    new_victims, new_survivors = svc_mod._plan_near_duplicate_merge(items)
    old_victims, old_survivors = _legacy_merge(items)

    assert {v.id for v in new_victims} == {v.id for v in old_victims}
    assert {s.id for s in new_survivors} == {s.id for s in old_survivors}
    # 每条要么被并掉、要么活着，两边都不能漏（否则归档/淘汰会少记账）
    assert len(new_victims) + len(new_survivors) == len(items)


def test_merge_plan_keeps_confidence_higher_and_breaks_ties_by_order():
    rng = random.Random(21)
    anchor = _vec(rng)
    low_first = _entry("low-first", _perturbed(rng, anchor), confidence=30, key="a")
    high_second = _entry("high-second", _perturbed(rng, anchor), confidence=80, key="b")
    victims, survivors = svc_mod._plan_near_duplicate_merge([low_first, high_second])
    assert [v.id for v in victims] == ["low-first"]
    assert [s.id for s in survivors] == ["high-second"]

    # 置信度并列：留**先入库**的那条（更早的引用更多）
    tie_a = _entry("tie-a", anchor, confidence=50, key="a")
    tie_b = _entry("tie-b", _perturbed(rng, anchor), confidence=50, key="b")
    victims, survivors = svc_mod._plan_near_duplicate_merge([tie_a, tie_b])
    assert [v.id for v in victims] == ["tie-b"]
    assert [s.id for s in survivors] == ["tie-a"]


def test_merge_plan_is_pure_and_io_free():
    """线程里跑的前提是不碰 IO：这里断言它只读入参、不改入参。"""
    items = _corpus(7)
    before = [(i.id, i.confidence, None if i.embedding is None else list(i.embedding)) for i in items]
    svc_mod._plan_near_duplicate_merge(items)
    after = [(i.id, i.confidence, i.embedding) for i in items]
    assert after == before
    assert len(items) == len(before)  # 没有原地增删


# ── 3. 淘汰走无向量投影 ─────────────────────────────────────────────────


class _RecordingStore(InMemoryProfileStore):
    """记下两条读取路径各被调用了几次（`list` = 带回向量的那条）。"""

    def __init__(self) -> None:
        super().__init__()
        self.list_calls = 0
        self.evictable_calls = 0

    async def list(self, *args: Any, **kwargs: Any):
        self.list_calls += 1
        return await super().list(*args, **kwargs)

    async def list_evictable(self, *args: Any, **kwargs: Any):
        self.evictable_calls += 1
        return await super().list_evictable(*args, **kwargs)


async def test_eviction_never_reads_the_embedding_carrying_list(monkeypatch):
    store = _RecordingStore()
    for i in range(4):
        await store.insert(_entry(f"e{i}", [1.0] * DIM, confidence=10 + i, key=f"k{i}"))
    monkeypatch.setattr(svc_mod.settings, "memory_profile_max_items", 2)

    async def boom(*_args: Any, **_kwargs: Any):
        raise AssertionError("容量淘汰不该走带回向量的 list()")

    store.list = boom  # 一旦退回旧写法，这里立刻炸

    evicted = await MemoryService(store=store)._evict_over_limit("t", "u")
    assert evicted == 2
    assert store.evictable_calls == 1


async def test_upsert_evicts_without_extra_vector_parses(monkeypatch):
    """端到端钉住写入路径：超限淘汰用投影，`list` 只为判重跑一次。"""
    store = _RecordingStore()
    monkeypatch.setattr(svc_mod.settings, "memory_profile_max_items", 3)
    seen: list[list[float]] = []

    async def embedder(text: str) -> list[float]:
        return [0.5] * DIM

    svc = MemoryService(store=store, embedder=embedder)
    for i in range(5):
        resp = await svc.upsert("t", "u", slot="fact", key=f"k{i}", value=f"v{i}", confidence=20 + i)
        seen.append(resp["entry"]["has_embedding"])

    assert store.evictable_calls == 5          # 每次写各一次
    assert store.list_calls == 5               # 判重仍要走 list（它要返回给用户看）
    assert sum(seen) == 5
    assert len(await store.list_evictable("t", "u")) == 3


class _FakePool:
    """只录 SQL 的 asyncpg 池替身。"""

    def __init__(self) -> None:
        self.query = ""
        self.args: tuple[Any, ...] = ()

    async def fetch(self, query: str, *args: Any):
        self.query = query
        self.args = args
        return [
            {
                "id": "e1",
                "confidence": 42,
                "source": "derived",
                "last_accessed_at": datetime.now(UTC),
            }
        ]


async def test_profile_projection_selects_no_embedding_or_value():
    pool = _FakePool()
    rows = await ProfileStore(pool).list_evictable("t", "u", project="p1")

    selected = pool.query.split("FROM")[0].lower()
    assert "embedding" not in selected
    assert "item_value" not in selected
    assert "status='active'" in pool.query
    # 并列时靠它保证"哪条被挤掉"可复现 —— 与 list() 同一顺序
    assert "ORDER BY slot, updated_at DESC" in pool.query
    assert pool.args == ("t", "u", "p1")
    assert rows == [EvictionRow(id="e1", confidence=42, source="derived",
                                last_accessed_at=rows[0].last_accessed_at)]


# ── 4. 整理的成对比较离开事件循环 ───────────────────────────────────────


async def test_organize_runs_merge_plan_off_the_event_loop(monkeypatch):
    """N=200 时这段是纯 CPU（实测约 0.7s）。留在事件循环上，同一 worker 的
    所有并发请求会一起停 —— 所以它必须跑在别的线程，且期间前台仍被调度。
    """
    store = InMemoryProfileStore()
    rng = random.Random(99)
    anchor = _vec(rng, dim=8)
    for i in range(6):
        await store.insert(_entry(f"e{i}", _perturbed(rng, anchor, 0.001), confidence=50, key=f"k{i}"))

    main_thread = threading.get_ident()
    ran_on: dict[str, int] = {}
    real = svc_mod._plan_near_duplicate_merge

    def slow_plan(items: list[Any]):
        ran_on["thread"] = threading.get_ident()
        time.sleep(0.25)  # 模拟 1536 维成对比较的 CPU 时间
        return real(items)

    monkeypatch.setattr(svc_mod, "_plan_near_duplicate_merge", slow_plan)

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        await MemoryService(store=store).organize_now("t", "u")
    finally:
        beat.cancel()

    assert ran_on["thread"] != main_thread, "合并计划没有离开事件循环线程"
    assert ticks >= 5, f"整理期间前台没被调度（心跳只走了 {ticks} 次）"


async def test_organize_still_merges_and_reports_counts():
    """搬到线程里之后，整理该删的还是要删、计数与错误口径不变。"""
    store = InMemoryProfileStore()
    rng = random.Random(101)
    anchor = _vec(rng)
    await store.insert(_entry("keep", _perturbed(rng, anchor), confidence=90, key="a"))
    await store.insert(_entry("drop", _perturbed(rng, anchor), confidence=20, key="b"))
    await store.insert(_entry("other", _vec(rng), confidence=30, key="c"))

    result = await MemoryService(store=store).organize_now("t", "u")

    assert result.merged == 1
    assert result.errors == []
    alive = {e.id for e in await store.list_evictable("t", "u")}
    assert alive == {"keep", "other"}


async def test_organize_survives_delete_failure_without_losing_rows(monkeypatch):
    """删失败 = 两条都还在库里：都不许从记账里消失，等下一轮整理再说。"""
    store = InMemoryProfileStore()
    rng = random.Random(102)
    anchor = _vec(rng)
    await store.insert(_entry("hi", _perturbed(rng, anchor), confidence=90, key="a"))
    await store.insert(_entry("lo", _perturbed(rng, anchor), confidence=20, key="b"))

    async def fail_delete(tenant_id, user_id, entry_id, project=""):
        raise RuntimeError("storage down")

    monkeypatch.setattr(store, "delete", fail_delete)

    result = await MemoryService(store=store).organize_now("t", "u")

    assert result.merged == 0
    assert len(result.errors) == 1
    assert {e.id for e in await store.list_evictable("t", "u")} == {"hi", "lo"}


# ── 5. L3 退化路径：「取最近」不再拉满 200 行 ───────────────────────────


def _summary(i: int, embedding: list[float] | None = None) -> SummaryEntry:
    return SummaryEntry(
        id=f"s{i}",
        tenant_id="t",
        user_id="u",
        session_id="sess",
        content=f"摘要 {i}",
        topics=["t"],
        entities={},
        turn_start=i,
        turn_end=i,
        content_hash=f"h{i}",
        access_count=0,
        last_accessed_at=None,
        status="active",
        created_at=float(1_700_000_000 + i),
        embedding=embedding,
    )


class _LimitRecordingSummaryStore:
    """没有 `recall` 方法 ⇒ 强制走 service 层的退化检索分支。"""

    def __init__(self, items: list[SummaryEntry]) -> None:
        self.items = items
        self.limits: list[int] = []
        self.touched: list[str] = []

    async def list_active(self, tenant_id, user_id, limit=50):
        self.limits.append(limit)
        return self.items[:limit]

    async def touch(self, summary_id):
        self.touched.append(summary_id)


async def test_recall_summaries_without_query_loads_only_top_k():
    store = _LimitRecordingSummaryStore([_summary(i) for i in range(200)])
    out = await MemoryService(summary_store=store).recall_summaries("t", "u", "", top_k=5)

    assert store.limits == [5], f"取最近只需 5 条，却请求了 {store.limits}"
    assert out["mode"] == "recent"
    assert out["count"] == 5
    assert [s["id"] for s in out["summaries"]] == ["s0", "s1", "s2", "s3", "s4"]


async def test_recall_summaries_empty_corpus_still_reports_empty():
    out = await MemoryService(summary_store=_LimitRecordingSummaryStore([])).recall_summaries(
        "t", "u", "", top_k=5
    )
    assert out == {"summaries": [], "count": 0, "mode": "empty"}

    out = await MemoryService(summary_store=_LimitRecordingSummaryStore([])).recall_summaries(
        "t", "u", "随便", top_k=5
    )
    assert out == {"summaries": [], "count": 0, "mode": "empty"}


async def test_recall_summaries_semantic_fallback_ranks_by_similarity():
    rng = random.Random(202)
    qv = _vec(rng)
    near = _summary(1, _perturbed(rng, qv))
    far = _summary(2, _vec(rng))
    store = _LimitRecordingSummaryStore([far, near])

    async def embedder(text: str) -> list[float]:
        return qv

    out = await MemoryService(summary_store=store, embedder=embedder).recall_summaries(
        "t", "u", "查询", top_k=2
    )

    assert out["mode"] == "semantic"
    assert [s["id"] for s in out["summaries"]] == ["s1"]  # 只有够像的才留下
    assert out["count"] == 1
    assert store.touched == ["s1"]
