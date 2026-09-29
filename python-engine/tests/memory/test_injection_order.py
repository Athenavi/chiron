"""A4 回归：L2 记忆注入必须「排序 + 按预算挑选」，且 score 不得丢失。

修复前的两个问题：
1. 注入是「全量拼接 + 超限**整段截断**」—— 截断线之后的条目（含刚 `remember` 写入的）
   会无声丢失，且没有任何提示；
2. `recall` 构造 `RecalledItem` 时**硬编码 `score=0.0`**，委托路径的 `_as_dict` 也不带
   `score` —— 两处叠加使注入 prompt 的 `(score 0.00)` 恒为 0。
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from app.memory.service import MemoryService


@dataclass
class _Entry:
    """最小 L2 条目替身（只需 `_serialize_entries` / `_injection_rank` 用到的字段）。"""

    slot: str = "preference"
    item_key: str = "k"
    item_value: str = "v"
    confidence: int = 80
    source: str = "derived"
    last_accessed_at: float = 0.0


def test_injection_rank_puts_confirmed_then_confidence():
    """注入优先级：user_confirmed 最优先，其次按置信度降序。"""
    low = _Entry(item_key="low", confidence=10)
    confirmed = _Entry(item_key="confirmed", confidence=1, source="user_confirmed")
    high = _Entry(item_key="high", confidence=99)

    ordered = [
        entry.item_key
        for entry in sorted([low, confirmed, high], key=MemoryService._injection_rank)
    ]
    assert ordered == ["confirmed", "high", "low"]


def test_budget_overflow_is_announced(monkeypatch: pytest.MonkeyPatch):
    """超出预算的条目要**显式告知条数**，而不是被静默截断。"""
    monkeypatch.setattr(MemoryService, "_PROFILE_BLOCK_MAX_BYTES", 60)
    items = [
        _Entry(item_key=f"k{i}", item_value="v" * 40, confidence=90 - i)
        for i in range(5)
    ]

    text = MemoryService._serialize_entries(items)
    assert "条记忆未注入" in text
    assert "memory_search" in text


def test_within_budget_has_no_omission_notice():
    items = [_Entry(item_key="only", item_value="v")]
    text = MemoryService._serialize_entries(items)
    assert "未注入" not in text
    assert "only" in text


@pytest.mark.asyncio
async def test_delegated_recall_preserves_score():
    """A4 主症状：委托路径（`store.recall`）算出的 final_score 不得被丢掉。"""
    store = MagicMock()

    async def fake_recall(*, scope=None, query="", top_k=5):
        return [
            MagicMock(
                id="s1",
                session_id="x",
                content="c",
                topics=[],
                entities={},
                turn_range=(1, 2),
                access_count=1,
                last_accessed_at=1.0,
                created_at=1.0,
                embedding=None,
                status="active",
                score=0.83,
            )
        ]

    store.recall = fake_recall
    service = MemoryService(summary_store=store)
    result = await service.recall("t", "u", query="q")

    assert result.summary_items, "委托路径未返回条目"
    assert result.summary_items[0].score == pytest.approx(0.83)
