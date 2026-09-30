"""C5：整理调度自动化（阈值触发 + 单飞行 + 项目隔离）。

三条阈值**任一**满足即触发：条目数 / 累积回合数 / 距上次触发时长。

为什么是"任一"而不是"全部"：三类触发场景互不重叠 —— 批量导入（条目多、回合少）、
长期闲聊（回合多、条目少）、低频用户（只是很久没整理）。要"全部满足"等于三种都不做。

触发一律走 `start_organize` 的**单飞行**：并发调用只有一个真正起任务，其余拿到
`already_running`（此时不重置记账，否则阈值会被"看起来触发了"抹掉）。
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.memory.service import MemoryService
from tests.fakes import InMemoryProfileStore


def _svc() -> MemoryService:
    return MemoryService(store=InMemoryProfileStore())


async def _seed(svc: MemoryService, n: int, project: str = "") -> None:
    for i in range(n):
        await svc.upsert(
            "t", "u", slot="fact", key=f"k{i}", value=f"v{i}", project=project
        )


# ── 三个阈值 ────────────────────────────────────────────────────────────


async def test_triggers_when_entry_threshold_reached(monkeypatch):
    monkeypatch.setattr(settings, "memory_organize_min_entries", 2)
    monkeypatch.setattr(settings, "memory_organize_min_turns", 999)
    monkeypatch.setattr(settings, "memory_organize_interval_seconds", 0)
    svc = _svc()
    await _seed(svc, 2)

    assert await svc.maybe_schedule_organize("t", "u") is True
    await asyncio.sleep(0)


async def test_does_not_trigger_below_every_threshold(monkeypatch):
    monkeypatch.setattr(settings, "memory_organize_min_entries", 10)
    monkeypatch.setattr(settings, "memory_organize_min_turns", 999)
    monkeypatch.setattr(settings, "memory_organize_interval_seconds", 0)
    svc = _svc()
    await _seed(svc, 1)

    assert await svc.maybe_schedule_organize("t", "u") is False


async def test_triggers_on_turn_threshold(monkeypatch):
    monkeypatch.setattr(settings, "memory_organize_min_entries", 999)
    monkeypatch.setattr(settings, "memory_organize_min_turns", 3)
    monkeypatch.setattr(settings, "memory_organize_interval_seconds", 0)
    svc = _svc()

    results = [await svc.maybe_schedule_organize("t", "u") for _ in range(3)]

    assert results == [False, False, True], "第 3 个回合才该触发"
    await asyncio.sleep(0)


async def test_triggers_on_interval(monkeypatch):
    monkeypatch.setattr(settings, "memory_organize_min_entries", 999)
    monkeypatch.setattr(settings, "memory_organize_min_turns", 999)
    monkeypatch.setattr(settings, "memory_organize_interval_seconds", 60)
    svc = _svc()
    svc._organize_meta[("t", "u", "")] = {"turns": 0, "last_at": time.time() - 3600}

    assert await svc.maybe_schedule_organize("t", "u") is True
    await asyncio.sleep(0)


async def test_disabled_by_switch(monkeypatch):
    monkeypatch.setattr(settings, "memory_organize_auto", False)
    monkeypatch.setattr(settings, "memory_organize_min_entries", 0)
    svc = _svc()

    assert await svc.maybe_schedule_organize("t", "u") is False
    assert svc._organize_meta == {}, "开关关掉时连记账都不该做"


# ── 单飞行 ──────────────────────────────────────────────────────────────


async def test_single_flight_does_not_start_second_task(monkeypatch):
    """已在整理时再触发：不起第二个任务（并发整理会重复烧 embedding）。"""
    monkeypatch.setattr(settings, "memory_organize_min_entries", 0)
    monkeypatch.setattr(settings, "memory_organize_min_turns", 999)
    monkeypatch.setattr(settings, "memory_organize_interval_seconds", 0)
    svc = _svc()

    assert await svc.maybe_schedule_organize("t", "u") is True
    assert await svc.maybe_schedule_organize("t", "u") is False
    assert len(svc._organize_tasks) == 1
    await asyncio.sleep(0)


# ── 与项目维的关系 ──────────────────────────────────────────────────────


async def test_accounting_is_per_project(monkeypatch):
    """A 项目的回合记账不该让 B 项目跟着触发（C3 的 project 维贯穿到调度）。"""
    monkeypatch.setattr(settings, "memory_organize_min_entries", 999)
    monkeypatch.setattr(settings, "memory_organize_min_turns", 2)
    monkeypatch.setattr(settings, "memory_organize_interval_seconds", 0)
    svc = _svc()

    assert await svc.maybe_schedule_organize("t", "u", "proj-a") is False
    assert await svc.maybe_schedule_organize("t", "u", "proj-b") is False
    assert await svc.maybe_schedule_organize("t", "u", "proj-a") is True
    await asyncio.sleep(0)


async def test_entry_count_is_per_project(monkeypatch):
    monkeypatch.setattr(settings, "memory_organize_min_entries", 2)
    monkeypatch.setattr(settings, "memory_organize_min_turns", 999)
    monkeypatch.setattr(settings, "memory_organize_interval_seconds", 0)
    svc = _svc()
    await _seed(svc, 2, project="proj-a")

    # proj-b 里一条都没有 ⇒ 不该触发；proj-a 里有 2 条 ⇒ 触发
    assert await svc.maybe_schedule_organize("t", "u", "proj-b") is False
    assert await svc.maybe_schedule_organize("t", "u", "proj-a") is True
    await asyncio.sleep(0)


# ── 钩子接线 ────────────────────────────────────────────────────────────


async def test_on_turn_complete_passes_project(monkeypatch):
    """回合钩子必须把 project 透传下去 —— 否则自动整理只作用于"未分组"。"""
    svc = _svc()
    seen: list[str] = []

    async def fake_maybe(tenant_id: str, user_id: str, project: str = "") -> bool:
        seen.append(project)
        return False

    monkeypatch.setattr(svc, "maybe_schedule_organize", fake_maybe)
    meta = MagicMock()
    meta.tenant_id, meta.user_id, meta.turn_count = "t", "u", 3
    svc._session_meta = MagicMock()
    svc._session_meta.get.return_value = meta

    await svc.on_turn_complete("s1", project="proj-a")

    assert seen == ["proj-a"]


async def test_on_turn_complete_survives_scheduler_failure(monkeypatch):
    """调度出错不能影响回合收尾（记账与 L3 入队已经做完了）。"""
    svc = _svc()

    async def boom(*_args, **_kwargs):
        raise RuntimeError("scheduler exploded")

    monkeypatch.setattr(svc, "maybe_schedule_organize", boom)
    meta = MagicMock()
    meta.tenant_id, meta.user_id, meta.turn_count = "t", "u", 1
    svc._session_meta = MagicMock()
    svc._session_meta.get.return_value = meta

    await svc.on_turn_complete("s1")  # 不该抛出


@pytest.mark.parametrize("entries,min_entries,expected", [(0, 0, True), (0, 1, False)])
async def test_entry_threshold_boundary(monkeypatch, entries, min_entries, expected):
    monkeypatch.setattr(settings, "memory_organize_min_entries", min_entries)
    monkeypatch.setattr(settings, "memory_organize_min_turns", 999)
    monkeypatch.setattr(settings, "memory_organize_interval_seconds", 0)
    svc = _svc()
    await _seed(svc, entries)

    assert await svc.maybe_schedule_organize("t", "u") is expected
    await asyncio.sleep(0)
