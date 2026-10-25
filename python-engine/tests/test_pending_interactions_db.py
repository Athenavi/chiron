"""M7a 的**真实 SQL** 回归（`integration`）：`pending_interactions` 与 `awaiting_input`。

单元用例（`tests/test_pending_interactions.py`）用替身池验证调用契约，但有四条不变量
**只有真库能证明**，而它们恰好是这次改动最容易写错的地方：

1. `ON CONFLICT (session_id, tool_call_id) WHERE status IN (...)` —— 冲突目标指向**部分**
   唯一索引，谓词写错时 PG 直接报错（"there is no unique or exclusion constraint matching"），
   替身池却会"通过"；
2. `DO UPDATE … WHERE pending_interactions.status = 'pending'` —— 用户已答但尚未被取走的那条
   授权，**不能**被一次重新提问擦掉；
3. `consume_grant` 的**单次消费**：两个回合并列续跑时只能有一个拿到批准（靠 PG 在
   READ COMMITTED 下对被更新行的 EPQ，不是靠我们手写锁）；
4. `awaiting_input` 不在 `ux_agent_runs_session_active` 谓词里 ⇒ 挂起时会话可以被另一个 run
   占用，而 `claim_awaiting` 在那种情况下必须**干净地失败**（撞唯一索引 = False，不抛出去）。

顺带覆盖 `jsonb` 在 asyncpg 下返回**字符串**这一事实（`_decode` 的存在理由）。

跑法：先 `alembic upgrade head`，再 `pytest -m integration tests/test_pending_interactions_db.py`。
库不存在这两张表时**跳过**（不建表：测试不该改 schema，也不该因为跑在旧库上就假装通过）。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import pytest

from app.agent import checkpoint as cp
from app.agent import interactions as ix

_TABLES = ("pending_interactions", "agent_runs")

#: 整个文件都是真库用例（CI 的 `-m integration` job 会跑；本地无 DSN 即跳过）。
#: 用 `pytestmark` 而不是逐个函数贴装饰器 —— 漏贴一个就会让一个真库用例悄悄不进 CI。
pytestmark = pytest.mark.integration


def _dsn() -> str:
    return os.environ.get("POSTGRES_DSN", "").strip()


async def _missing_tables(dsn: str) -> list[str]:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        out: list[str] = []
        for table in _TABLES:
            row = await conn.fetchrow("SELECT to_regclass($1) AS name", f"public.{table}")
            if row is None or row["name"] is None:
                out.append(table)
        return out
    finally:
        await conn.close()


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch):
    """真库连接池，并把两个模块的 `_pool` 指过去。"""
    import asyncpg

    dsn = _dsn()
    if not dsn:
        pytest.skip("需要 POSTGRES_DSN")
    missing = await _missing_tables(dsn)
    if missing:
        pytest.skip(f"库里没有 {', '.join(missing)}；请先 alembic upgrade head")

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
    monkeypatch.setattr(ix, "_pool", lambda: pool)
    monkeypatch.setattr(cp, "_pool", lambda: pool)
    try:
        yield pool
    finally:
        await pool.close()


async def _purge(pool: Any, session_id: str) -> None:
    await pool.execute("DELETE FROM pending_interactions WHERE session_id = $1", session_id)
    await pool.execute("DELETE FROM agent_runs WHERE session_id = $1", session_id)


async def _row(pool: Any, session_id: str, interaction_id: str) -> dict[str, Any]:
    row = await pool.fetchrow(
        """
        SELECT status, request, decision, args_hash, run_id
          FROM pending_interactions WHERE id = $1 AND session_id = $2
        """,
        interaction_id,
        session_id,
    )
    return dict(row) if row else {}


# ── 1A：现场登记 → 答复 → 取走 ────────────────────────────────────────────


async def test_create_resolve_consume_roundtrip(db: Any) -> None:
    session_id = f"itest-{uuid.uuid4().hex[:12]}"
    try:
        created = await ix.create(
            tenant_id=1,
            user_id="u-a",
            session_id=session_id,
            kind=ix.KIND_APPROVAL,
            tool_call_id="tc-1",
            tool_name="shell",
            args_hash="hash-a",
            request={"question": "请求执行 shell"},
            run_token="rt-1",
        )
        assert created, "create 失败"

        # ① 同一 tool_call_id 再问一次：**复用那一行**（否则唯一索引让第二次询问静默失败）
        again = await ix.create(
            tenant_id=1,
            user_id="u-a",
            session_id=session_id,
            kind=ix.KIND_APPROVAL,
            tool_call_id="tc-1",
            tool_name="shell",
            args_hash="hash-a",
            request={"question": "真的需要执行 shell"},
            run_token="rt-1",
        )
        assert again == created
        first = await _row(db, session_id, created)
        assert first["status"] == ix.STATUS_PENDING
        assert "真的需要" in str(first["request"])
        assert first["run_id"] == "rt-1"

        # ② 用户答复
        resolved = await ix.resolve(
            interaction_id=created,
            tenant_id=1,
            user_id="u-a",
            decision={"decision": "approve"},
            reason="可以",
        )
        assert resolved is not None
        assert resolved["decision"] == {"decision": "approve"}
        # jsonb 经 asyncpg 是**字符串**，_decode 必须已经把它变成 dict
        assert resolved["request"] == {"question": "真的需要执行 shell"}
        assert (await _row(db, session_id, created))["status"] == ix.STATUS_ANSWERED

        # ③ 答复未取走时，重新提问**不得**擦掉它（DO UPDATE 的 status='pending' 守卫）
        wiped = await ix.create(
            tenant_id=1,
            user_id="u-a",
            session_id=session_id,
            kind=ix.KIND_APPROVAL,
            tool_call_id="tc-1",
            tool_name="shell",
            args_hash="hash-a",
            request={"question": "重新问一次"},
            run_token="rt-1",
        )
        assert wiped == ""
        after_reask = await _row(db, session_id, created)
        assert after_reask["status"] == ix.STATUS_ANSWERED
        assert after_reask["decision"] is not None
        # 答复还在，展示内容也没被这次"重新提问"改掉
        assert "真的需要" in str(after_reask["request"])

        # ④ 续跑回合取走：`answered` → `consumed`
        grant = await ix.consume_grant(
            tenant_id=1,
            session_id=session_id,
            kind=ix.KIND_APPROVAL,
            tool_name="shell",
            args_hash="hash-a",
        )
        assert grant is not None and grant["decision"] == {"decision": "approve"}
        assert (await _row(db, session_id, created))["status"] == ix.STATUS_CONSUMED
        # 单次消费：批准不会生效第二次
        assert await ix.consume_grant(
            tenant_id=1,
            session_id=session_id,
            kind=ix.KIND_APPROVAL,
            tool_name="shell",
            args_hash="hash-a",
        ) is None
        # consumed 之后可以重新问了（旧行不再占用唯一索引名额）
        third = await ix.create(
            tenant_id=1,
            user_id="u-a",
            session_id=session_id,
            kind=ix.KIND_APPROVAL,
            tool_call_id="tc-1",
            tool_name="shell",
            args_hash="hash-a",
        )
        assert third and third != created
    finally:
        await _purge(db, session_id)


async def test_grant_is_bound_to_arguments_and_owner(db: Any) -> None:
    session_id = f"itest-{uuid.uuid4().hex[:12]}"
    try:
        approval_id = await ix.create(
            tenant_id=1,
            user_id="u-a",
            session_id=session_id,
            kind=ix.KIND_APPROVAL,
            tool_call_id="tc-args",
            tool_name="write_file",
            args_hash="hash-path-a",
        )
        await ix.resolve(
            interaction_id=approval_id,
            tenant_id=1,
            user_id="u-a",
            decision={"decision": "approve"},
        )
        # 参数变了就是另一件事：**不认**（这是"批准的就是执行的"的跨进程版本）
        assert await ix.consume_grant(
            tenant_id=1,
            session_id=session_id,
            kind=ix.KIND_APPROVAL,
            tool_name="write_file",
            args_hash="hash-path-b",
        ) is None
        # 别的租户问不到（介入是授权，跨租户命中等于替别人的会话放行）
        assert await ix.consume_grant(
            tenant_id=2,
            session_id=session_id,
            kind=ix.KIND_APPROVAL,
            tool_name="write_file",
            args_hash="hash-path-a",
        ) is None
        # 别人的答复不算数
        assert await ix.resolve(
            interaction_id=approval_id, tenant_id=1, user_id="u-b", decision={}
        ) is None
        assert await ix.resolve(
            interaction_id=approval_id, tenant_id=2, user_id="u-a", decision={}
        ) is None
        assert await ix.list_open(tenant_id=1, user_id="u-b", session_id=session_id) == []
        assert len(await ix.list_open(tenant_id=1, user_id="u-a", session_id=session_id)) == 1
        # 上面那次失败的 resolve 没有把状态挪走 → 原样取走
        got = await ix.consume_grant(
            tenant_id=1,
            session_id=session_id,
            kind=ix.KIND_APPROVAL,
            tool_name="write_file",
            args_hash="hash-path-a",
        )
        assert got is not None and got["decided_by"] == "u-a"
    finally:
        await _purge(db, session_id)


async def test_two_concurrent_resumes_one_grant_wins(db: Any) -> None:
    """两个回合并列续跑：批准只生效**一次**（EPQ 后第二个谓词不成立）。"""
    session_id = f"itest-{uuid.uuid4().hex[:12]}"
    try:
        approval_id = await ix.create(
            tenant_id=1,
            user_id="u-a",
            session_id=session_id,
            kind=ix.KIND_APPROVAL,
            tool_call_id="tc-race",
            tool_name="shell",
            args_hash="hash-race",
        )
        await ix.resolve(
            interaction_id=approval_id,
            tenant_id=1,
            user_id="u-a",
            decision={"decision": "approve"},
        )
        winners = await asyncio.gather(
            *(
                ix.consume_grant(
                    tenant_id=1,
                    session_id=session_id,
                    kind=ix.KIND_APPROVAL,
                    tool_name="shell",
                    args_hash="hash-race",
                )
                for _ in range(5)
            )
        )
        assert sum(1 for item in winners if item is not None) == 1
    finally:
        await _purge(db, session_id)


async def test_expiry_frees_the_unique_slot(db: Any) -> None:
    """过期必须**落状态**：否则 `answered` 一直占着名额 ⇒ 重新问静默失败。"""
    session_id = f"itest-{uuid.uuid4().hex[:12]}"
    try:
        approval_id = await ix.create(
            tenant_id=1,
            user_id="u-a",
            session_id=session_id,
            kind=ix.KIND_QUESTION,
            tool_call_id="tc-exp",
            tool_name="ask_user",
            args_hash="hash-exp",
        )
        await ix.resolve(
            interaction_id=approval_id,
            tenant_id=1,
            user_id="u-a",
            decision={"answer": "选 A"},
        )
        # 授权过期了才来取 ⇒ 作废（不能拿一张早该失效的票放行执行）
        await db.execute(
            "UPDATE pending_interactions SET expires_at = now() - interval '1 hour' WHERE id = $1",
            approval_id,
        )
        assert await ix.consume_grant(
            tenant_id=1,
            session_id=session_id,
            kind=ix.KIND_QUESTION,
            tool_name="ask_user",
            args_hash="hash-exp",
        ) is None
        assert await ix.expire_stale(limit=200) >= 1
        assert (await _row(db, session_id, approval_id))["status"] == ix.STATUS_EXPIRED
        assert await ix.list_open(tenant_id=1, user_id="u-a", session_id=session_id) == []
        # 名额已释放：同一件事可以重新问了
        fresh = await ix.create(
            tenant_id=1,
            user_id="u-a",
            session_id=session_id,
            kind=ix.KIND_QUESTION,
            tool_call_id="tc-exp",
            tool_name="ask_user",
            args_hash="hash-exp",
        )
        assert fresh and fresh != approval_id
    finally:
        await _purge(db, session_id)


# ── awaiting_input：真实唯一索引下的接管语义 ──────────────────────────────


async def _insert_run(pool: Any, session_id: str, *, status: str, run_token: str) -> str:
    row_id = uuid.uuid4().hex
    await pool.execute(
        """
        INSERT INTO agent_runs
            (id, tenant_id, session_id, turn_id, run_token, status,
             checkpoint, checkpoint_at, created_at, updated_at)
        VALUES ($1, 't-a', $2, 'turn-1', $3, $4, $5::jsonb, now(), now(), now())
        """,
        row_id,
        session_id,
        run_token,
        status,
        '{"schema_version": 1, "turn_index": 3, "messages": [{"role": "user", "content": "hi"}]}',
    )
    return row_id


async def test_awaiting_input_releases_and_reclaims_the_session(db: Any) -> None:
    session_id = f"itest-{uuid.uuid4().hex[:12]}"
    try:
        await _insert_run(db, session_id, status=cp.STATUS_RUNNING, run_token="rt-old")

        # 挂起：迁出活跃集合 ⇒ 唯一索引的名额被释放
        assert await cp.mark(
            tenant_id=1, session_id=session_id,
            status=cp.STATUS_AWAITING_INPUT, run_token="rt-old",
        )
        # 会话于是可以再开一个 run（用户可以接着聊别的）
        await _insert_run(db, session_id, status=cp.STATUS_RUNNING, run_token="rt-other")

        # 读挂起现场仍在（`load()` 只认活跃行 ⇒ 看到的是那个新 run，不是挂起的那份）
        loaded = await cp.load(tenant_id=1, session_id=session_id)
        assert loaded is not None and int(loaded.get("turn_index") or 0) == 3
        awaiting = await cp.load_awaiting(tenant_id=1, session_id=session_id)
        assert awaiting is not None and awaiting["run_token"] == "rt-old"

        # 但**交还**会被唯一索引拒绝：会话上已有活跃行，不能让两个 run 共用一份挂起现场
        assert not await cp.claim_awaiting(
            tenant_id=1, session_id=session_id, new_run_token="rt-new"
        )
        assert (await cp.load_awaiting(tenant_id=1, session_id=session_id)) is not None

        # 新 run 结束后，挂起现场可以被下一个回合接手
        await db.execute(
            "UPDATE agent_runs SET status = 'completed' WHERE run_token = 'rt-other'"
        )
        assert await cp.claim_awaiting(
            tenant_id=1, session_id=session_id, new_run_token="rt-new"
        )
        assert await cp.load_awaiting(tenant_id=1, session_id=session_id) is None
        resumed = await cp.load(tenant_id=1, session_id=session_id)
        assert resumed is not None and resumed.get("turn_index") == 3
        row = await db.fetchrow(
            "SELECT status, run_token FROM agent_runs WHERE session_id = $1 "
            "AND run_token = 'rt-new'",
            session_id,
        )
        assert row["status"] == cp.STATUS_RESUMING
    finally:
        await _purge(db, session_id)


async def test_awaiting_input_is_invisible_to_the_takeover_paths(db: Any) -> None:
    """reconciler 的扫描与 `claim()` 都不认 `awaiting_input` ⇒ 机器不会"代替用户批准"。"""
    session_id = f"itest-{uuid.uuid4().hex[:12]}"
    try:
        await _insert_run(db, session_id, status=cp.STATUS_AWAITING_INPUT, run_token="rt-wait")

        assert not await cp.claim(
            tenant_id=1, session_id=session_id,
            expected_run_token="rt-wait", new_run_token="rt-steal",
        )
        swept = await db.fetch(
            """
            SELECT id FROM agent_runs
             WHERE session_id = $1 AND status IN ('running', 'checkpointed')
            """,
            session_id,
        )
        assert swept == []
        # 也读不到"普通活跃现场"：批 3 加的过滤不会让陈旧挂起快照复活后面的无关回合
        assert await cp.load(tenant_id=1, session_id=session_id) is None
        # 同一个会话因此可以再开一个 run 而不撞唯一索引
        await _insert_run(db, session_id, status=cp.STATUS_RUNNING, run_token="rt-next")
    finally:
        await _purge(db, session_id)
