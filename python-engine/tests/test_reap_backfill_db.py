"""M15d 的**真实 SQL** 回归（`integration`）：收口时补用量、给 lost 行回填。

单元用例（`tests/test_subagent_state_metrics.py`）用替身池钉住"发哪两条语句、返回值取哪条"，
但**求和口径只有真库能证明** —— 而这次改的正是求和：

1. `REAP_STALE_SQL` 里三条相关子查询（步骤求和 / 步骤条数 / 最后活跃时间）真的能在
   `UPDATE … FROM doomed` 里一起工作（CTE 之间的可见性、`::bigint` 转换都是易写错的地方）；
2. "超龄"按**最后活跃时间**判定：一直有步骤产出的长任务不该被收口；
3. `BACKFILL_LOST_USAGE_SQL` 的幂等：谓词是"当前 token 为 0 **且**步骤和 > 0"，
   补过的行第二次不再命中 —— 写错就会让每次收口都重写一遍终态行；
4. 两条语句都**不碰**任何已有非零用量的终态行（`completed` 的真值不能被子查询盖掉）。

跑法：`pytest -m integration tests/test_reap_backfill_db.py`（需要 `POSTGRES_DSN`）。
表不存在时**跳过**。所有写入都在一个**未提交的事务**里，断言完 rollback：
收口的谓词是"全库超龄的 running 行"，它会连**别人留下的**僵尸一起改掉，
测试不该有那种副作用。
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from app.subagent.store import SubagentRunStore

pytestmark = pytest.mark.integration


def _dsn() -> str:
    return os.environ.get("POSTGRES_DSN", "").strip()


class _TxPool:
    """让 store 的 `execute` 走**同一条连接、同一个事务**（用例结束统一回滚）。"""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def execute(self, sql: str, *args: Any) -> str:
        return await self._conn.execute(sql, *args)

    async def executemany(self, sql: str, rows: Any) -> None:
        await self._conn.executemany(sql, rows)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return await self._conn.fetchrow(sql, *args)


@pytest.fixture
async def tx():
    """一个回滚型事务 + 绑到它上面的"池"。库里没有这两张表时跳过。"""
    import asyncpg

    dsn = _dsn()
    if not dsn:
        pytest.skip("需要 POSTGRES_DSN")
    probe = await asyncpg.connect(dsn)
    try:
        for table in ("subagent_runs", "subagent_run_steps"):
            row = await probe.fetchrow("SELECT to_regclass($1) AS name", f"public.{table}")
            if row is None or row["name"] is None:
                pytest.skip(f"库里没有 {table}；请先 alembic upgrade head")
    finally:
        await probe.close()

    conn = await asyncpg.connect(dsn)
    tr = conn.transaction()
    await tr.start()
    try:
        yield _TxPool(conn), conn
    finally:
        await tr.rollback()
        await conn.close()


async def _insert_run(
    conn: Any,
    *,
    status: str = "running",
    started_hours_ago: int | None = 10,
    input_tokens: int = 0,
    output_tokens: int = 0,
    steps: int = 0,
    finished: bool = False,
    error: str | None = None,
) -> str:
    run_id = f"itest-{uuid.uuid4().hex[:12]}"
    await conn.execute(
        """
        INSERT INTO subagent_runs
            (id, root_session_id, task, status, started_at, finished_at,
             input_tokens, output_tokens, steps, error)
        VALUES ($1, $2, 'reap 用例', $3,
                (now() - make_interval(hours => $4::int))::timestamp,
                CASE WHEN $5::boolean THEN now()::timestamp ELSE NULL END,
                $6, $7, $8, $9)
        """,
        run_id,
        f"sess-{run_id}",
        status,
        int(started_hours_ago if started_hours_ago is not None else 0),
        finished,
        input_tokens,
        output_tokens,
        steps,
        error,
    )
    return run_id


async def _insert_steps(conn: Any, run_id: str, rows: list[tuple[int, int, int]]) -> None:
    """rows = `(input_tokens, output_tokens, created_minutes_ago)`。"""
    for seq, (inp, outp, minutes) in enumerate(rows, start=1):
        await conn.execute(
            """
            INSERT INTO subagent_run_steps
                (run_id, seq, kind, input_tokens, output_tokens, created_at)
            VALUES ($1, $2, 'tool_result', $3, $4,
                    (now() - make_interval(mins => $5::int))::timestamp)
            """,
            run_id,
            seq,
            inp,
            outp,
            int(minutes),
        )


async def _row(conn: Any, run_id: str) -> dict[str, Any]:
    r = await conn.fetchrow(
        """
        SELECT status, input_tokens, output_tokens, steps, finished_at, error
          FROM subagent_runs WHERE id = $1
        """,
        run_id,
    )
    return dict(r)


# ── 1：收口顺手补账 ────────────────────────────────────────────────────────


async def test_reap_fills_usage_from_step_rows(tx: tuple[Any, Any]) -> None:
    pool, conn = tx
    stale = await _insert_run(conn, started_hours_ago=10)
    await _insert_steps(conn, stale, [(100, 20, 500), (50, 5, 480)])

    fresh = await _insert_run(conn, started_hours_ago=100)
    await _insert_steps(conn, fresh, [(1000, 100, 1)])

    done = await _insert_run(
        conn, status="completed", started_hours_ago=100,
        input_tokens=777, output_tokens=33, steps=4, finished=True,
    )

    count = await SubagentRunStore(pool).reap_stale_runs(max_age_hours=2)
    assert count >= 1

    got = await _row(conn, stale)
    assert got["status"] == "lost"
    assert got["input_tokens"] == 150 and got["output_tokens"] == 25, (
        "用量必须由步骤求和重建：崩在半路的那一段不进线账就是**假少算**"
    )
    assert got["steps"] == 2
    assert got["finished_at"] is not None

    # ② 一直在产出的长任务**不是**僵尸（活跃时间是心跳）
    assert (await _row(conn, fresh))["status"] == "running"
    # ③ 已有非零用量的终态行绝不被动
    after = await _row(conn, done)
    assert (after["status"], after["input_tokens"], after["output_tokens"], after["steps"]) == (
        "completed",
        777,
        33,
        4,
    )


async def test_reap_keeps_larger_existing_step_count(tx: tuple[Any, Any]) -> None:
    """`steps` 取 max(步骤行数, 原值)：崩时缓冲区里未 flush 的步骤会**丢行**，
    而 run 行上的计数是事件循环里累加出来的 —— 宁可多一步，也不要把已有读数改小。
    token 仍按行数求和（少算的方向与线账整体一致）。"""
    pool, conn = tx
    run_id = await _insert_run(conn, started_hours_ago=10, steps=9)
    await _insert_steps(conn, run_id, [(10, 1, 500)])

    await SubagentRunStore(pool).reap_stale_runs(max_age_hours=2)

    got = await _row(conn, run_id)
    assert got["steps"] == 9 and got["input_tokens"] == 10 and got["output_tokens"] == 1


# ── 2：网关标的 lost 行（从来没经过收口）───────────────────────────────────


async def test_backfill_fills_gateway_marked_lost_rows(tx: tuple[Any, Any]) -> None:
    pool, conn = tx
    # markRunLost 的形态：status=lost、**没有**任何步骤以外的用量，finished_at 已写
    run_id = await _insert_run(
        conn, status="lost", started_hours_ago=1,
        finished=True, error="unclaimed", input_tokens=0, output_tokens=0,
    )
    await _insert_steps(conn, run_id, [(30, 4, 20), (7, 1, 10)])
    # 没有任何步骤的 lost 行：补不出来，必须**保持 0**（不能凭空造数）
    empty = await _insert_run(conn, status="lost", started_hours_ago=1, finished=True)

    before = await _row(conn, run_id)
    count = await SubagentRunStore(pool).reap_stale_runs(max_age_hours=2)

    got = await _row(conn, run_id)
    assert (got["input_tokens"], got["output_tokens"], got["steps"]) == (37, 5, 2)
    assert got["status"] == before["status"] and got["error"] == before["error"], (
        "补账只写用量：status/finished_at/error 是网关已经定的终态"
    )
    assert got["finished_at"] == before["finished_at"]
    assert (await _row(conn, empty))["input_tokens"] == 0
    # 指标口径：只算 running→lost 的转移，补账条数不得混进来
    assert count == 0, "本用例没有任何超龄 running 行，返回值必须仍是转移数"


async def test_backfill_is_idempotent(tx: tuple[Any, Any]) -> None:
    """谓词"当前为 0 且步骤和 > 0"让补过的行不再命中 —— 否则每次收口都重写终态行。"""
    pool, conn = tx
    run_id = await _insert_run(conn, status="lost", started_hours_ago=1, finished=True)
    await _insert_steps(conn, run_id, [(200, 30, 5)])

    store = SubagentRunStore(pool)
    await store.reap_stale_runs(max_age_hours=2)
    first = await _row(conn, run_id)
    await store.reap_stale_runs(max_age_hours=2)
    second = await _row(conn, run_id)

    assert (first["input_tokens"], first["output_tokens"]) == (200, 30)
    assert first == second, "第二次必须一行都不动"
