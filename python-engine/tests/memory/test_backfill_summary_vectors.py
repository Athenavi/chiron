"""M13 存量摘要向量回填（⑦）：给迁移 `0019` 之前的老行补向量。

四条不变量：

1. **默认 dry-run**：只数不写（每条一次 embedding 调用是花钱的）；
2. **口径只认 `SummaryEntry.embed_text`** —— 这里**不许自己拼**（曾经两处都自称
   "topics + 正文"，落库的却是两个互不可比的向量空间）；
3. **幂等**：写入带 `WHERE id = $4 AND embedding IS NULL` 谓词；
4. **一条失败不中断整批**（失败逐条记 id），空向量**不写**（不拿 NULL 覆盖）。
"""
from __future__ import annotations

from typing import Any

import pytest

from app.memory import backfill as bf
from app.memory.layers import SummaryEntry


class _FakePool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, _sql: str, _limit: int) -> list[dict[str, Any]]:
        return list(self.rows)

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "UPDATE 1"


class _FakeEmbedder:
    def __init__(self, *, vector: list[float] | None = None, raises: bool = False) -> None:
        self.vector = vector if vector is not None else [0.1, 0.2]
        self.raises = raises
        self.seen: list[str] = []

    async def __call__(self, text: str) -> list[float] | None:
        self.seen.append(text)
        if self.raises:
            raise RuntimeError("gateway down")
        return self.vector


def _row(i: int, *, topics: Any = None) -> dict[str, Any]:
    return {
        "id": f"sms_{i}",
        "tenant_id": "t1",
        "user_id": "u1",
        "session_id": "s1",
        "content": f"内容{i}",
        "topics": topics,
    }


async def test_dry_run_counts_but_never_writes() -> None:
    pool = _FakePool([_row(1), _row(2)])
    result = await bf.backfill_summary_vectors(pool=pool, embedder=_FakeEmbedder())

    assert result.considered == 2 and result.filled == 0
    assert result.dry_run is True
    assert pool.executed == [], "dry-run 绝不能写库（每条嵌入都是花钱的）"


async def test_backfill_fills_and_write_is_idempotent_by_predicate() -> None:
    pool = _FakePool([_row(1)])
    embedder = _FakeEmbedder(vector=[1.0, 0.0, 0.5])

    result = await bf.backfill_summary_vectors(pool=pool, embedder=embedder, dry_run=False)

    assert result.filled == 1 and result.failed == 0
    sql, args = pool.executed[0]
    assert "embedding IS NULL" in sql, "幂等靠这条谓词：并发/重跑都不会覆盖已有向量"
    assert args[0] == [1.0, 0.0, 0.5] and args[2] == 3, "维度要按实际向量长度写"


async def test_embedding_text_uses_the_entry_contract() -> None:
    """口径必须来自 `SummaryEntry.embed_text` —— 自己拼字符串就是造第二个向量空间。"""
    pool = _FakePool([_row(1, topics=["部署", "回滚"])])
    embedder = _FakeEmbedder()

    await bf.backfill_summary_vectors(pool=pool, embedder=embedder, dry_run=False)

    expected = SummaryEntry(
        id="sms_1", tenant_id="t1", user_id="u1", session_id="s1",
        content="内容1", topics=["部署", "回滚"],
    ).embed_text
    assert embedder.seen == [expected]


async def test_failed_embedding_does_not_stop_the_batch() -> None:
    pool = _FakePool([_row(1)])
    result = await bf.backfill_summary_vectors(
        pool=pool, embedder=_FakeEmbedder(raises=True), dry_run=False
    )

    assert result.filled == 0 and result.failed == 1
    assert result.errors and "sms_1" in result.errors[0]
    assert pool.executed == [], "嵌入失败时不该写任何东西"


async def test_empty_vector_is_not_written() -> None:
    """嵌入函数说"这次算不出来" ⇒ 记失败、**不写**（拿 NULL/空向量覆盖是更糟的结果）。"""
    pool = _FakePool([_row(1)])
    result = await bf.backfill_summary_vectors(
        pool=pool, embedder=_FakeEmbedder(vector=[]), dry_run=False
    )

    assert result.filled == 0 and result.failed == 1
    assert pool.executed == []


@pytest.mark.parametrize("topics", [["a", "b"], '["a", "b"]', None, "not-json", {"x": 1}])
async def test_topics_accepts_json_text_list_and_garbage(topics: Any) -> None:
    """`topics` 是 jsonb：驱动可能给 list 也可能给 JSON 文本；垃圾值不该让整批炸掉。"""
    pool = _FakePool([_row(1, topics=topics)])
    result = await bf.backfill_summary_vectors(
        pool=pool, embedder=_FakeEmbedder(), dry_run=False
    )

    assert result.filled == 1, f"topics={topics!r} 不该让这条失败"
