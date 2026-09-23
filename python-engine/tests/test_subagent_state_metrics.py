"""P1-3：子 Agent 状态转移必须**可观测**，且只有唯一的收尾入口。

背景（docs/subagent-interaction-redesign.md §八「尚未完成」）：

* 终态写入点分散在多条路径（正常收尾 / 取消收尾 / wall 定时器 / 僵尸收口器），
  历史上反复出现"某条路径忘了写"——DB 里留下 ``status='running'``，前端永久显示
  "运行中"，而**没有任何指标能反映它**。
* 缓存侧还有两个面（run Hash 与整树骨架）。取消路径此前只写了前者，于是被取消的
  run 在侧边栏/整树里停在 ``running`` —— 两条路径各写各的，就是漏写的形式。

这里把两件事钉住：
1. 权威写入点（``subagent_runs``）上的指标：started / terminal / unfinalized / persist_failed；
2. 缓存终态**只有一个入口**，两个面一起写（``_persist_terminal_to_cache``）。
"""
from __future__ import annotations

import asyncio

import pytest

from app.agent.subagent_runner import SubAgentRunner, _persist_terminal_to_cache
from app.observability.metrics import (
    SUBAGENT_PERSIST_FAILED,
    SUBAGENT_RUN_STARTED,
    SUBAGENT_RUN_TERMINAL,
    SUBAGENT_RUN_UNFINALIZED,
)
from app.subagent.store import SubagentRunStore


def _counter(counter, **labels) -> float:
    return counter.labels(**labels)._value.get() if labels else counter._value.get()


def _gauge(gauge) -> float:
    return gauge._value.get()


class _FakePool:
    """记录 SQL；`update_rows` 决定 reap 的 UPDATE n 返回值。"""

    def __init__(self, update_rows: int = 1) -> None:
        self.executed: list[str] = []
        self.update_rows = update_rows

    async def execute(self, sql, *args):
        self.executed.append(sql.strip().split()[0].upper())
        return f"UPDATE {self.update_rows}"

    async def executemany(self, sql, rows):
        return None


class _BrokenPool:
    async def execute(self, sql, *args):
        raise RuntimeError('relation "subagent_runs" does not exist')

    async def executemany(self, sql, rows):
        raise RuntimeError('relation "subagent_run_steps" does not exist')


@pytest.mark.asyncio
async def test_start_and_finish_move_the_state_machine_metrics():
    """started/terminal 成对增长，unfinalized 回到 0 —— 状态机收敛。"""
    started_before = _counter(SUBAGENT_RUN_STARTED)
    completed_before = _counter(SUBAGENT_RUN_TERMINAL, status="completed")

    store = SubagentRunStore(_FakePool())
    await store.start_run(run_id="rs_m1", root_session_id="s1", task="t")
    assert _counter(SUBAGENT_RUN_STARTED) == started_before + 1
    assert _gauge(SUBAGENT_RUN_UNFINALIZED) == 1

    await store.finish_run("rs_m1", status="completed", summary="s")
    assert _counter(SUBAGENT_RUN_TERMINAL, status="completed") == completed_before + 1
    assert _gauge(SUBAGENT_RUN_UNFINALIZED) == 0


@pytest.mark.asyncio
async def test_terminal_label_is_bounded():
    """未知/大小写异常的状态归到 other：标签必须有界，否则统计会被打乱。"""
    other_before = _counter(SUBAGENT_RUN_TERMINAL, status="other")
    store = SubagentRunStore(_FakePool())
    await store.finish_run("rs_m2", status="Completed!")
    assert _counter(SUBAGENT_RUN_TERMINAL, status="other") == other_before + 1


@pytest.mark.asyncio
async def test_double_finalize_does_not_go_negative():
    """同一 run 写两次终态（正常收尾与取消收尾竞争）时 unfinalized 不得变负。"""
    store = SubagentRunStore(_FakePool())
    await store.start_run(run_id="rs_m3", root_session_id="s1", task="t")
    await store.finish_run("rs_m3", status="completed")
    await store.finish_run("rs_m3", status="cancelled")
    assert _gauge(SUBAGENT_RUN_UNFINALIZED) == 0


@pytest.mark.asyncio
async def test_finish_without_start_does_not_decrement():
    """进程重启后收尾上一进程的 run：terminal 照记，但不污染 unfinalized。"""
    cancelled_before = _counter(SUBAGENT_RUN_TERMINAL, status="cancelled")
    store = SubagentRunStore(_FakePool())
    await store.finish_run("rs_from_previous_process", status="cancelled")
    assert _counter(SUBAGENT_RUN_TERMINAL, status="cancelled") == cancelled_before + 1
    assert _gauge(SUBAGENT_RUN_UNFINALIZED) == 0


@pytest.mark.asyncio
async def test_reaper_counts_lost_transitions():
    """僵尸收口是状态转移（running → lost），必须计入指标，否则仍然只能靠查 DB。"""
    lost_before = _counter(SUBAGENT_RUN_TERMINAL, status="lost")
    store = SubagentRunStore(_FakePool(update_rows=3))
    assert await store.reap_stale_runs(max_age_hours=2) == 3
    assert _counter(SUBAGENT_RUN_TERMINAL, status="lost") == lost_before + 3


@pytest.mark.asyncio
async def test_persist_failure_is_counted_and_state_not_claimed():
    """落库失败既不能计入 started（指标不许说谎），又必须有失败计数。"""
    started_before = _counter(SUBAGENT_RUN_STARTED)
    failed_before = _counter(SUBAGENT_PERSIST_FAILED, component="db", op="start_run")

    store = SubagentRunStore(_BrokenPool())
    await store.start_run(run_id="rs_m4", root_session_id="s1", task="t")

    assert _counter(SUBAGENT_RUN_STARTED) == started_before  # 没写成就不算启动
    assert _counter(SUBAGENT_PERSIST_FAILED, component="db", op="start_run") == failed_before + 1


class _RecordingCache:
    """记录缓存两个面的写入，用来验证"必须一起写"。"""

    def __init__(self) -> None:
        self.status_writes: list[dict] = []
        self.tree_writes: list[dict] = []

    async def start_run(self, **kwargs):
        self.started = kwargs

    async def update_status(self, **kwargs):
        self.status_writes.append(kwargs)

    async def update_tree_summary(self, **kwargs):
        self.tree_writes.append(kwargs)


@pytest.mark.asyncio
async def test_terminal_persist_writes_both_cache_faces():
    cache = _RecordingCache()
    await _persist_terminal_to_cache(
        cache,
        run_id="rs_c1",
        tenant="t1",
        root_session_id="root",
        status="cancelled",
        summary="被取消",
        usage={"steps": 3},
        depth=1,
        parent_run_id="rs_parent",
        profile="reviewer",
    )
    assert cache.status_writes[0]["status"] == "cancelled"
    assert cache.tree_writes[0]["status"] == "cancelled"
    assert cache.tree_writes[0]["summary"] == "被取消"


@pytest.mark.asyncio
async def test_terminal_persist_tolerates_missing_cache():
    await _persist_terminal_to_cache(None, run_id="rs_c2", tenant="t1",
                                     root_session_id="root", status="completed",
                                     summary="")


class _RecordingStore:
    def __init__(self) -> None:
        self.finished = False
        self.finished_status = ""

    async def start_run(self, *args, **kwargs):
        return kwargs.get("run_id") or "rs_test"

    async def add_step(self, *args, **kwargs):
        return None

    async def flush_steps(self, *args, **kwargs):
        return None

    async def finish_run(self, *args, **kwargs):
        self.finished = True
        self.finished_status = kwargs.get("status")


@pytest.mark.asyncio
async def test_cancelled_run_also_updates_tree(monkeypatch):
    """取消收尾必须与正常收尾写同样多的面 —— 否则树里永远停在 running。"""
    from app.agent import runtime as runtime_mod

    started = asyncio.Event()

    async def _hung_run(self, task):  # noqa: ANN001
        started.set()
        await asyncio.sleep(3600)
        yield runtime_mod.AgentEvent(type="text", content="unreachable")

    monkeypatch.setattr(runtime_mod.AgentRuntime, "run", _hung_run)

    store = _RecordingStore()
    cache = _RecordingCache()
    runner = SubAgentRunner(
        store=store,
        cache=cache,
        gateway=object(),
        parent_session_id="s1",
        parent_run_id="rs_parent",
        tenant_id="t1",
        user_id="u1",
    )

    task = asyncio.create_task(runner.run("阻塞任务", profile_ref="", mode="normal", max_turns=3))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(5):
        await asyncio.sleep(0)

    assert store.finished and store.finished_status == "cancelled"
    assert cache.status_writes and cache.status_writes[-1]["status"] == "cancelled"
    assert cache.tree_writes and cache.tree_writes[-1]["status"] == "cancelled", (
        "取消收尾必须同时写整树骨架：它才是 GET /v1/subagent/runs 的数据源，"
        "漏写会让被取消的 run 在侧边栏里一直显示'运行中'"
    )
