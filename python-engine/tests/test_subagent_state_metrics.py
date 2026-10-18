"""P1-3：子 Agent 状态转移必须**可观测**，且只有唯一的收尾入口。

背景（该设计文档**未入库**，结论如下）：

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
from app.subagent.store import BACKFILL_LOST_USAGE_SQL, REAP_STALE_SQL, SubagentRunStore


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

    async def fetchrow(self, sql, *args):
        """M15a 起 `start_run` 会解析 root —— 桩得有这个字，否则落库走的是异常降级分支。"""
        self.executed.append("SELECT")
        return None

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


class _TwoStatementPool:
    """M15d：收口器现在发**两条** UPDATE（收口 + 补账），且必须返回不同条数。

    用 `make_interval` 区分：只有收口那条带超龄参数。桩不解释 SQL 语义 ——
    它只钉住"谁被调用、返回值取哪一条"，真正的求和口径由
    `tests/test_reap_backfill_db.py` 在真库里判。
    """

    def __init__(self, reap_rows: int = 3, backfill_rows: int = 7) -> None:
        self.reap_rows = reap_rows
        self.backfill_rows = backfill_rows
        self.sqls: list[str] = []
        self.args: list[tuple] = []
        self.fail_backfill = False

    @staticmethod
    def _is_reap(sql: str) -> bool:
        return "make_interval" in sql

    async def execute(self, sql, *args):
        norm = " ".join(sql.split())
        self.sqls.append(norm)
        self.args.append(args)
        if not self._is_reap(sql) and self.fail_backfill:
            raise RuntimeError("connection lost")
        return f"UPDATE {self.reap_rows if self._is_reap(sql) else self.backfill_rows}"

    async def fetchrow(self, sql, *args):
        return None

    async def executemany(self, sql, rows):
        return None


@pytest.mark.asyncio
async def test_reaper_returns_only_transitions_and_backfills_separately():
    """补账（幂等修复）不许混进"running→lost 转移数"—— 否则 reaper 指标虚高。"""
    lost_before = _counter(SUBAGENT_RUN_TERMINAL, status="lost")
    pool = _TwoStatementPool(reap_rows=3, backfill_rows=7)
    store = SubagentRunStore(pool)

    assert await store.reap_stale_runs(max_age_hours=2) == 3
    assert _counter(SUBAGENT_RUN_TERMINAL, status="lost") == lost_before + 3
    assert len(pool.sqls) == 2, "收口之后必须再发一条补账语句"
    assert pool.args[0] == (2,) and pool.args[1] == (), "补账是无参的（它按 status 筛，不看年龄）"


@pytest.mark.asyncio
async def test_reaper_backfills_even_with_no_transitions():
    """网关取消路径标的 lost 行**从来没经过收口**（它一开始就不是 running）。

    补账必须在"这次没有任何僵尸"时照跑，否则那类行永远停在 0 token、任务线账永久少算。
    """
    pool = _TwoStatementPool(reap_rows=0, backfill_rows=2)
    store = SubagentRunStore(pool)
    assert await store.reap_stale_runs(max_age_hours=2) == 0
    assert len(pool.sqls) == 2


@pytest.mark.asyncio
async def test_reaper_backfill_failure_does_not_hide_transitions():
    """补账失败只降级补账：已经发生的收口（和它的指标）不能被一起吞掉。"""
    lost_before = _counter(SUBAGENT_RUN_TERMINAL, status="lost")
    pool = _TwoStatementPool(reap_rows=1, backfill_rows=0)
    pool.fail_backfill = True
    store = SubagentRunStore(pool)

    assert await store.reap_stale_runs(max_age_hours=2) == 1
    assert _counter(SUBAGENT_RUN_TERMINAL, status="lost") == lost_before + 1
    assert store.available, "补账语句失败是这一次的问题，不该把整个 store 标成不可用"


def test_reap_sql_fills_usage_from_step_rows():
    """收口那条 SQL 必须**赋值**（不是相加）补上 token：既有值必然是初值 0，相加会数两遍。"""
    sql = " ".join(REAP_STALE_SQL.split())
    assert "input_tokens = d.in_tokens" in sql
    assert "output_tokens = d.out_tokens" in sql
    assert "input_tokens + r.input_tokens" not in sql, "赋值而不是相加：相加会把同一批 token 数两遍"
    # 步骤求和是唯一的重建依据；收口仍只动 running
    assert "subagent_run_steps" in sql
    assert "r.status = 'running'" in sql, "收口只动 running，绝不触碰任何终态行"


def test_backfill_sql_is_idempotent_by_predicate():
    """补账靠"当前为 0 且步骤和 > 0"收敛：写过的行下次不再命中（否则每次收口都重写）。"""
    sql = " ".join(BACKFILL_LOST_USAGE_SQL.split())
    assert "r.status = 'lost'" in sql
    assert "COALESCE(r.input_tokens, 0) + COALESCE(r.output_tokens, 0) = 0" in sql
    assert "f.in_tokens + f.out_tokens > 0" in sql
    set_clause = sql.split("UPDATE subagent_runs r SET", 1)[1].split(" FROM filled", 1)[0]
    for frozen in ("status", "finished_at", "error"):
        assert frozen not in set_clause, f"补账不许改动 {frozen}：那是网关已经定的终态"


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

    async def mark_lifecycle(self, *args, **kwargs):
        """A5：记录遥测调用（真实写入由 store 自己的测试覆盖）。"""
        self.lifecycles = getattr(self, "lifecycles", [])
        self.lifecycles.append(kwargs)


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
