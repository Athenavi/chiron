"""R2 回归测试：子 Agent 写路径仲裁（`app/subagent/scheduler.py`）。

对位 vendor/规划.md §3.5 的 R2 与 `vendor/DeepSeek-Reasonix/internal/agent/scheduler.go`
（含 `scheduler_test.go` / `write_claims_test.go` 的口径）。**三条并发行为是核心验收**：

1. 同路径 ⇒ **串行**（后来的排队等前者释放）；
2. 不同路径 ⇒ **并行**（互不阻塞）；
3. 嵌套 ⇒ 无容量时**立即失败**（排队就是自等死锁 —— 父正持有它要等的那类资源）。

外加："默认关 ⇒ 零行为变化"、"声明非法显式失败"、"release 幂等"、"取消不漏槽"、
"父写预留挡住重叠的子 Agent 但放行它自己派发的委派"这几条边界。
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.subagent import scheduler as scheduler_mod
from app.subagent.scheduler import (
    AcquireRequest,
    WriteConflictError,
    WritePathSet,
    WriteScheduler,
    get_scheduler,
    normalize_concurrency_limits,
    normalize_write_paths,
    schedule_overlaps,
    whole_workspace_claim,
)


def _scheduler(root, **kwargs) -> WriteScheduler:
    kwargs.setdefault("max_total", 8)
    kwargs.setdefault("max_writers", 3)
    return WriteScheduler(workspace_root=str(root), **kwargs)


def _paths(root, *entries) -> WritePathSet:
    return normalize_write_paths(str(root), list(entries))


async def _settle() -> None:
    """让 create_task 出来的协程跑到它的等待点。"""
    for _ in range(3):
        await asyncio.sleep(0)


# ── 核心：三条并发行为 ──


@pytest.mark.asyncio
async def test_same_path_writers_serialize(tmp_path):
    sched = _scheduler(tmp_path)
    first, _ = await sched.acquire(AcquireRequest(writer=True, write_paths=_paths(tmp_path, "a.txt")))
    second = asyncio.create_task(
        sched.acquire(AcquireRequest(writer=True, write_paths=_paths(tmp_path, "a.txt")))
    )
    await _settle()
    assert not second.done()                      # 同路径：必须等
    assert sched.pending_waiter_count() == 1

    first()
    release2, _ = await asyncio.wait_for(second, timeout=1.0)
    assert sched.active_counts() == (1, 1)
    release2()
    assert sched.active_counts() == (0, 0)


@pytest.mark.asyncio
async def test_different_path_writers_run_in_parallel(tmp_path):
    sched = _scheduler(tmp_path)
    rel_a, _ = await sched.acquire(AcquireRequest(writer=True, write_paths=_paths(tmp_path, "docs/a.md")))
    rel_b, _ = await sched.acquire(AcquireRequest(writer=True, write_paths=_paths(tmp_path, "docs/b.md")))
    assert sched.active_counts() == (2, 2)        # 不等、直接并发
    assert sched.pending_waiter_count() == 0
    rel_a()
    rel_b()


@pytest.mark.asyncio
async def test_nested_writer_fails_fast_instead_of_queueing(tmp_path):
    sched = _scheduler(tmp_path, max_writers=1)
    holder, _ = await sched.acquire(AcquireRequest(writer=True, write_paths=_paths(tmp_path, "a.txt")))

    # 非嵌套：**排队**（后台委派可以等）
    queued = asyncio.create_task(
        sched.acquire(AcquireRequest(writer=True, write_paths=_paths(tmp_path, "b.txt")))
    )
    await _settle()
    assert not queued.done()

    # 嵌套：**立即失败** —— 它要等的槽正被父持有，排队必然自等
    with pytest.raises(WriteConflictError):
        await sched.acquire(
            AcquireRequest(writer=True, write_paths=_paths(tmp_path, "c.txt"), nested=True)
        )

    holder()
    queued_release, _ = await asyncio.wait_for(queued, timeout=1.0)
    queued_release()


# ── 默认关：零行为变化 ──


def test_arbitration_is_off_by_default():
    """§1.3：新增机制的默认值必须是"什么都不做"。"""
    assert settings.subagent_write_arbitration is False
    scheduler_mod.reset_schedulers()
    assert get_scheduler("sess-1", workspace_root="x") is None


def test_enabled_scheduler_is_shared_per_session(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "subagent_write_arbitration", True)
    monkeypatch.setattr(settings, "subagent_max_concurrency", 4)
    monkeypatch.setattr(settings, "subagent_max_writers", 2)
    scheduler_mod.reset_schedulers()

    first = get_scheduler("sess-1", workspace_root=str(tmp_path))
    assert first is not None
    # 同一会话必须**共享**同一个仲裁器，否则"路径冲突"根本看不见彼此
    assert get_scheduler("sess-1", workspace_root=str(tmp_path)) is first
    assert first.limits == (4, 2)
    assert get_scheduler("sess-2", workspace_root=str(tmp_path)) is not first
    # 无会话 id ⇒ 没有可共享的作用域，返回 None（不造一个"全局"仲裁器）
    assert get_scheduler("", workspace_root=str(tmp_path)) is None


# ── 声明规范化：非法一律显式失败（不静默降级）──


def test_normalize_rejects_glob_escape_and_empty(tmp_path):
    with pytest.raises(ValueError):
        normalize_write_paths(str(tmp_path), ["*.md"])
    with pytest.raises(ValueError):
        normalize_write_paths(str(tmp_path), ["../outside.txt"])
    with pytest.raises(ValueError):
        normalize_write_paths(str(tmp_path), ["   "])
    # 空声明是合法的（= 只读），不是错误
    assert normalize_write_paths(str(tmp_path), []).empty
    assert normalize_write_paths(str(tmp_path), None).empty


def test_normalize_marks_directories_and_dedupes(tmp_path):
    (tmp_path / "docs").mkdir()
    paths = normalize_write_paths(str(tmp_path), ["docs/", "docs", "a.txt", "a.txt"])
    assert paths.kinds == ("dir", "file")          # docs 重复项被去重，a.txt 同理
    assert len(paths.paths) == 2
    assert not paths.empty


# ── 重叠语义 ──


def test_directory_claims_parallel_but_file_inside_conflicts(tmp_path):
    docs = _paths(tmp_path, "docs/")
    more_docs = _paths(tmp_path, "docs/")
    one_file = _paths(tmp_path, "docs/a.md")
    other_file = _paths(tmp_path, "docs/b.md")

    assert not schedule_overlaps(docs, more_docs)  # 目录↔目录：可以并行
    assert schedule_overlaps(docs, one_file)       # 目录 vs 它里面的文件：冲突
    assert not schedule_overlaps(one_file, other_file)
    assert not schedule_overlaps(docs, WritePathSet())  # 只读不参与


def test_whole_workspace_conflicts_with_everything(tmp_path):
    whole = whole_workspace_claim(str(tmp_path))
    assert schedule_overlaps(whole, _paths(tmp_path, "a.txt"))
    assert schedule_overlaps(whole, _paths(tmp_path, "docs/"))
    assert not schedule_overlaps(whole, WritePathSet())


@pytest.mark.asyncio
async def test_writer_without_declared_paths_takes_whole_workspace(tmp_path):
    """可写但没声明写了哪儿 ⇒ 保守地按整工作区独占（否则两个 writer 会静默互相覆盖）。"""
    sched = _scheduler(tmp_path)
    declared, _ = await sched.acquire(AcquireRequest(writer=True))
    claims = sched.active_writer_claims()
    assert len(claims) == 1 and claims[0].whole_workspace is True

    later = asyncio.create_task(
        sched.acquire(AcquireRequest(writer=True, write_paths=_paths(tmp_path, "a.txt")))
    )
    await _settle()
    assert not later.done()

    declared()
    later_release, _ = await asyncio.wait_for(later, timeout=1.0)
    later_release()


# ── 槽位记账 ──


@pytest.mark.asyncio
async def test_release_is_idempotent_and_readonly_never_conflicts(tmp_path):
    sched = _scheduler(tmp_path)
    release, _ = await sched.acquire(AcquireRequest(writer=True, write_paths=_paths(tmp_path, "a.txt")))
    release()
    release()                                     # 二次调用不得把计数减成负数
    assert sched.active_counts() == (0, 0)

    read_only_1, _ = await sched.acquire(AcquireRequest(writer=False))
    read_only_2, _ = await sched.acquire(AcquireRequest(writer=False))
    writer, _ = await sched.acquire(AcquireRequest(writer=True, write_paths=_paths(tmp_path, "a.txt")))
    assert sched.active_counts() == (3, 1)        # 只读占总额但不占 writer 槽、也不冲突
    read_only_1()
    read_only_2()
    writer()


@pytest.mark.asyncio
async def test_total_concurrency_limit_queues_readonly_too(tmp_path):
    sched = _scheduler(tmp_path, max_total=2, max_writers=2)
    first, _ = await sched.acquire(AcquireRequest())
    second, _ = await sched.acquire(AcquireRequest())

    queued = asyncio.create_task(sched.acquire(AcquireRequest()))
    await _settle()
    assert not queued.done()

    first()
    third, _ = await asyncio.wait_for(queued, timeout=1.0)
    second()
    third()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_slots(tmp_path):
    sched = _scheduler(tmp_path, max_writers=1)
    holder, _ = await sched.acquire(AcquireRequest(writer=True, write_paths=_paths(tmp_path, "a.txt")))
    waiter = asyncio.create_task(
        sched.acquire(AcquireRequest(writer=True, write_paths=_paths(tmp_path, "b.txt")))
    )
    await _settle()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert sched.pending_waiter_count() == 0
    holder()
    assert sched.active_counts() == (0, 0)
    # 取消之后槽位仍然可用（泄漏的话这里会卡住）
    retry, _ = await asyncio.wait_for(
        sched.acquire(AcquireRequest(writer=True, write_paths=_paths(tmp_path, "b.txt"))),
        timeout=1.0,
    )
    retry()


# ── 父写预留 ──


@pytest.mark.asyncio
async def test_parent_write_reservation_blocks_overlapping_subagent(tmp_path):
    sched = _scheduler(tmp_path)
    release_parent, claim_id = sched.reserve_parent_write(_paths(tmp_path, "a.txt"))
    assert claim_id > 0
    assert sched.try_claim_write_paths(_paths(tmp_path, "a.txt")) is not None

    # 预留**内部**派发的委派带着同一个 claim id ⇒ 不被自己挡住
    inside, _ = await sched.acquire(AcquireRequest(
        writer=True, write_paths=_paths(tmp_path, "a.txt"), parent_claim_id=claim_id
    ))
    inside()
    # 不重叠的路径照常
    other, _ = await sched.acquire(AcquireRequest(writer=True, write_paths=_paths(tmp_path, "b.txt")))
    other()

    release_parent()
    assert sched.try_claim_write_paths(_paths(tmp_path, "a.txt")) is None


def test_parent_reservation_conflict_fails_immediately(tmp_path):
    """父在工具调用里，不能去等后台任务 —— 冲突必须立刻失败。"""
    sched = _scheduler(tmp_path)
    release_parent, _ = sched.reserve_parent_write(_paths(tmp_path, "a.txt"))
    with pytest.raises(WriteConflictError):
        sched.reserve_parent_write(_paths(tmp_path, "a.txt"))
    release_parent()


def test_empty_parent_reservation_is_a_noop(tmp_path):
    sched = _scheduler(tmp_path)
    release, claim_id = sched.reserve_parent_write(WritePathSet())
    assert claim_id == 0
    release()                                     # 幂等且安全


# ── 上限夹取 ──


def test_concurrency_limits_are_clamped():
    assert normalize_concurrency_limits(0, 0) == (6, 3)
    assert normalize_concurrency_limits(-1, -1) == (6, 3)
    assert normalize_concurrency_limits(100, 100) == (32, 32)
    assert normalize_concurrency_limits(2, 5) == (2, 2)
