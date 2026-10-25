"""M22：L3 摘要检索的模式必须**如实透出**到前端可见的那一层。

现场：`recall_summaries` 一直算得出 `semantic` / `recent` / `keyword` / `empty`，
但 `MemoryService.search()` 只取 `summaries`、把 `mode` 丢掉 —— 前端因此回答不了
"这批摘要是怎么被选出来的"（它与 L2 的 `mode` 是两件事）。

三条不变量：
1. 有 L3 结果时 `summary_mode` 等于 **L3 自己报的** mode；
2. L3 抛错 / 没接 `summary_store` 时是 `empty` —— 那是"没拿到"，不是"最近"；
3. 两个 mode **互不覆盖**（L2 与 L3 可以同时不同）。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.memory.service import MemoryService


class _FakeProfileStore:
    """L2 侧替身：没有条目、也不需要真的记账。"""

    def __init__(self) -> None:
        self.touched: list[list[str]] = []

    async def list(self, tenant_id: int, user_id: str, *, slot: str = "", project: str = "") -> list[Any]:
        return []

    async def touch(self, ids: list[str]) -> None:
        self.touched.append(list(ids))


def _summary(source: str, item_id: str = "s1") -> Any:
    return SimpleNamespace(
        id=item_id,
        session_id="sess-1",
        content="历史对话摘要",
        topics=[],
        entities={},
        turn_range=(0, 3),
        access_count=1,
        last_accessed_at=None,
        created_at=0.0,
        score=0.9,
        embedding=[0.1, 0.2],
        status="active",
        source=source,
    )


class _FakeSummaryStore:
    def __init__(self, items: list[Any] | None = None, *, raise_on_recall: bool = False) -> None:
        self._items = items or []
        self._raise = raise_on_recall
        self.calls: list[dict[str, Any]] = []

    async def recall(self, *, scope: Any, query: str, top_k: int) -> list[Any]:
        self.calls.append({"scope": scope, "query": query, "top_k": top_k})
        if self._raise:
            raise RuntimeError("l3 down")
        return list(self._items)


async def _search(summary_store: Any) -> dict[str, Any]:
    svc = MemoryService(store=_FakeProfileStore(), summary_store=summary_store)
    return await svc.search("t1", "u1", "上次说的部署方案")


async def test_summary_mode_is_the_l3_mode_not_the_l2_one() -> None:
    out = await _search(_FakeSummaryStore([_summary("vector")]))

    assert out["summary_count"] == 1
    # L3 走了向量 ⇒ semantic；而 L2 没有 embedder ⇒ keyword。两者必须各说各话。
    assert out["summary_mode"] == "semantic"
    assert out["mode"] == "keyword"


async def test_summary_mode_reports_recent_when_l3_did_not_vectorize() -> None:
    out = await _search(_FakeSummaryStore([_summary("recent")]))

    assert out["summary_mode"] == "recent"


async def test_summary_mode_is_empty_without_a_summary_store() -> None:
    out = await _search(None)

    assert out["summaries"] == []
    assert out["summary_mode"] == "empty"


async def test_summary_mode_is_empty_when_l3_fails() -> None:
    """L3 是增强项：失败不该让整个检索失败，但也不能把它报成"最近"。"""
    out = await _search(_FakeSummaryStore(raise_on_recall=True))

    assert out["summaries"] == []
    assert out["summary_mode"] == "empty"


async def test_search_requires_a_query() -> None:
    svc = MemoryService(store=_FakeProfileStore(), summary_store=None)
    with pytest.raises(ValueError):
        await svc.search("t1", "u1", "   ")
