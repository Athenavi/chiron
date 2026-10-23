"""M7a 回归：人工介入现场（`pending_interactions`）与 `awaiting_input` 状态。

真实 SQL 由 `integration` 用例覆盖（需要 PG）；这里用替身池验证**调用契约**——
尤其是四条只有看 SQL 才能确认的不变量：

1. `create` 撞唯一索引时**只刷新 `pending`，绝不覆盖已收到的 `answered`**；
2. `resolve` / `list_open` 把 `tenant_id` + `user_id` 写进 WHERE（介入是授权，跨用户命中
   等于拿别人的批准放行自己的调用）；
3. `consume_grant` 按 `(会话, kind, 工具名, args_hash)` 认票且**单次消费**，空 args_hash
   永不匹配；
4. `awaiting_input` 不属于活跃集合 ⇒ `load()` / `claim()` / reconciler 的谓词都不认它，
   读它必须与 `claim_awaiting` 配对。

以及一条行为不变量：**任何存储失败都返回"没有"**，让调用方回落 M6 的即时拒绝，
而不是"没人批准却执行了"。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from app.agent import checkpoint as cp
from app.agent import interactions as ix


class _FakePool:
    """替身池：记录 SQL 与参数，按 `row` / `rows` / `status` 模拟 asyncpg 返回。"""

    def __init__(
        self,
        *,
        row: dict[str, Any] | None = None,
        rows: list[dict[str, Any]] | None = None,
        status: str = "UPDATE 1",
        raise_on: str = "",
    ) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.row = row
        self.rows = rows or []
        self.status = status
        self.raise_on = raise_on

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((sql, args))
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError("db down")
        return self.row

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError("db down")
        return self.status

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError("db down")
        return self.rows

    @property
    def last_sql(self) -> str:
        return self.calls[-1][0]

    @property
    def last_args(self) -> tuple[Any, ...]:
        return self.calls[-1][1]


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> _FakePool:
    fake = _FakePool()
    monkeypatch.setattr(ix, "_pool", lambda: fake)
    return fake


def _norm(sql: str) -> str:
    return " ".join(sql.split())


# ── create：登记"正在等人" ────────────────────────────────────────────────


async def test_create_returns_the_row_id(pool: _FakePool) -> None:
    pool.row = {"id": "int-1"}
    got = await ix.create(
        tenant_id=1,
        user_id="u1",
        session_id="s1",
        kind=ix.KIND_APPROVAL,
        tool_call_id="tc1",
        tool_name="shell",
        args_hash="h1",
        request={"question": "请求执行 shell"},
        run_token="rt1",
    )
    assert got == "int-1"
    sql, args = pool.last_sql, pool.last_args
    assert "INSERT INTO pending_interactions" in sql
    assert args[1] == 1 and args[2] == "u1" and args[3] == "s1"
    assert args[6] == ix.KIND_APPROVAL and args[7] == "tc1"
    # run_id 存 run_token（诊断用），ttl 走 make_interval 而不是拼字符串
    assert args[4] == "rt1"
    assert args[11] == ix.TTL_SECONDS
    assert "make_interval" in sql
    # request 是 JSON 文本进 jsonb
    assert json.loads(args[10])["question"] == "请求执行 shell"


async def test_create_reuse_never_overwrites_an_unconsumed_answer(pool: _FakePool) -> None:
    """重新问同一件事时复用唯一索引上的那一行，但**已收到的答复不能被擦掉**。"""
    pool.row = {"id": "int-1"}
    await ix.create(
        tenant_id=0,
        user_id="u1",
        session_id="s1",
        kind=ix.KIND_QUESTION,
        tool_call_id="tc1",
    )
    sql = _norm(pool.last_sql)
    assert "ON CONFLICT (session_id, tool_call_id) WHERE status IN ('pending', 'answered')" in sql
    assert "WHERE pending_interactions.status = 'pending' RETURNING id" in sql


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "approve_me", "tool_call_id": "tc1", "session_id": "s1"},
        {"kind": ix.KIND_APPROVAL, "tool_call_id": "", "session_id": "s1"},
        {"kind": ix.KIND_APPROVAL, "tool_call_id": "tc1", "session_id": ""},
    ],
)
async def test_create_refuses_before_touching_the_db(
    pool: _FakePool, kwargs: dict[str, Any]
) -> None:
    assert await ix.create(tenant_id=0, user_id="u1", **kwargs) == ""
    assert pool.calls == []


async def test_create_degrades_to_empty_on_any_failure(
    pool: _FakePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写不进去 ⇒ 返回空串（调用方回落 M6 的即时拒绝）。**不能**抛出去打断对话。"""
    kwargs = dict(
        tenant_id=0, user_id="u1", session_id="s1",
        kind=ix.KIND_APPROVAL, tool_call_id="tc1",
    )
    pool.raise_on = "INSERT INTO pending_interactions"
    assert await ix.create(**kwargs) == ""

    monkeypatch.setattr(ix, "_pool", lambda: None)
    pool.raise_on = ""
    assert await ix.create(**kwargs) == ""


# ── M15c：`budget` 这一类，以及"写不进去"必须可见 ────────────────────────


def _fail_count(reason: str) -> float:
    from app.observability.metrics import INTERACTION_CREATE_FAILED

    return INTERACTION_CREATE_FAILED.labels(reason=reason)._value.get()


async def test_create_accepts_a_budget_ticket(pool: _FakePool) -> None:
    """线账越界的票**不需要 run_token** 也能挂出去：它不靠"下一轮来取"生效。

    这一条值得单独锁住 —— 子 Agent 的失败段跑在合成会话里，本来就没有可交给下一轮的东西，
    如果 `create` 要求 run_token，那条被暂停的线就永远挂不出票（= 没人能提额）。
    """
    pool.row = {"id": "int-2"}
    got = await ix.create(
        tenant_id=1,
        user_id="u1",
        session_id="s-main",
        kind=ix.KIND_BUDGET,
        tool_call_id="budget:rs_root",
        tool_name="subagent",
        args_hash="h2",
        request={"root_run_id": "rs_root", "axis": "line_tokens"},
    )
    assert got == "int-2"
    assert pool.last_args[6] == ix.KIND_BUDGET
    assert pool.last_args[4] is None, "run_id 列存的是 run_token，没有就是 NULL"
    assert json.loads(pool.last_args[10])["root_run_id"] == "rs_root"


async def test_create_failure_is_counted_per_reason(pool: _FakePool) -> None:
    """每条返回空串的路径都要留下可见痕迹：approval 失败只是"回落 M6 的即时拒绝"，而
    budget 票失败意味着"一条被暂停的线，没有任何人能看见"。返回值仍是空串（改成异常会把
    增强路径变成能打断对话的东西），但不再静默。"""
    base = dict(tenant_id=1, user_id="u1", session_id="s1")

    before = _fail_count("unknown_kind")
    assert await ix.create(kind="approve_me", tool_call_id="tc1", **base) == ""
    assert _fail_count("unknown_kind") == before + 1
    assert pool.calls == [], "认不出的 kind 连库都不该碰"

    before = _fail_count("missing_keys")
    assert await ix.create(kind=ix.KIND_BUDGET, tool_call_id="", **base) == ""
    assert _fail_count("missing_keys") == before + 1

    before = _fail_count("storage_error")
    pool.raise_on = "INSERT INTO pending_interactions"
    assert await ix.create(kind=ix.KIND_BUDGET, tool_call_id="budget:rs", **base) == ""
    assert _fail_count("storage_error") == before + 1

    # ON CONFLICT 命中一个已 `answered` 的名额 ⇒ RETURNING 无行：这张占位的票必须可见
    pool.raise_on = ""
    pool.row = None
    before = _fail_count("conflict_answered")
    assert await ix.create(kind=ix.KIND_BUDGET, tool_call_id="budget:rs", **base) == ""
    assert _fail_count("conflict_answered") == before + 1


async def test_create_without_a_pool_is_counted(pool: _FakePool, monkeypatch) -> None:
    monkeypatch.setattr(ix, "_pool", lambda: None)
    before = _fail_count("no_pool")
    assert await ix.create(
        tenant_id=1, user_id="u1", session_id="s1",
        kind=ix.KIND_BUDGET, tool_call_id="budget:rs",
    ) == ""
    assert _fail_count("no_pool") == before + 1


# ── settle：把现场一并记上答复并关掉 ─────────────────────────────────────


async def test_settle_closes_the_ticket_and_records_the_reply(pool: _FakePool) -> None:
    pool.status = "UPDATE 1"
    assert await ix.settle(
        interaction_id="int-1",
        decision={"cap_tokens": 5000},
        reason="提到了 5000",
        decided_by="u1",
        tenant_id=1,
        user_id="u1",
    ) is True
    norm = _norm(pool.last_sql)
    assert "SET status = 'consumed'" in norm
    assert "AND status IN ('pending', 'answered')" in norm
    assert "AND ($5 = 0 OR tenant_id = $5)" in norm
    assert "AND ($6 = '' OR user_id = $6)" in norm
    args = pool.last_args
    assert args[0] == "int-1"
    assert json.loads(args[1]) == {"cap_tokens": 5000}
    assert args[2] == "提到了 5000"
    assert args[3:] == ("u1", 1, "u1")


async def test_settle_without_identity_matches_the_m7_callers(pool: _FakePool) -> None:
    """M7 的实时路径不传身份 ⇒ 空串谓词恒真，与加这两个参数之前逐字等价。"""
    pool.status = "UPDATE 1"
    assert await ix.settle(interaction_id="int-1", decision={"answer": "B"}) is True
    assert pool.last_args[3:] == ("", 0, "")


@pytest.mark.parametrize("status", ["UPDATE 0", "UPDATE 1"])
async def test_settle_reports_the_rowcount(pool: _FakePool, status: str) -> None:
    """0 行 = 那张票已经被取走/过期 ⇒ False，调用方据此不写"已经关掉"的谎。"""
    pool.status = status
    got = await ix.settle(interaction_id="int-1", decision={})
    assert got is (status == "UPDATE 1")


# ── resolve：用户给出答复 ─────────────────────────────────────────────────


async def test_resolve_is_ownership_scoped_and_single_shot(pool: _FakePool) -> None:
    pool.row = {
        "id": "int-1",
        "kind": ix.KIND_APPROVAL,
        "session_id": "s1",
        "run_id": "rt1",
        "turn_id": None,
        "tool_call_id": "tc1",
        "tool_name": "shell",
        "args_hash": "h1",
        "request": json.dumps({"question": "请求执行 shell"}),
        "decision": json.dumps({"decision": "approve"}),
    }
    got = await ix.resolve(
        interaction_id="int-1",
        tenant_id=1,
        user_id="u1",
        decision={"decision": "approve"},
        reason="看了，可以",
    )
    assert got is not None
    assert got["request"] == {"question": "请求执行 shell"}
    assert got["decision"] == {"decision": "approve"}
    sql, args = pool.last_sql, pool.last_args
    norm = _norm(sql)
    assert "SET status = 'answered'" in norm
    assert "AND ($2 = 0 OR tenant_id = $2)" in norm
    assert "AND user_id = $3" in norm
    assert "AND status = 'pending'" in norm
    assert "AND expires_at > now()" in norm
    assert args[:3] == ("int-1", 1, "u1")


async def test_resolve_returns_none_when_the_row_is_not_mine(pool: _FakePool) -> None:
    """不存在 / 不是这个人的 / 已答过 / 已过期 —— 一律 None（调用方统一 404，不做区分）。"""
    pool.row = None
    assert await ix.resolve(
        interaction_id="int-1", tenant_id=1, user_id="u1", decision={"decision": "reject"}
    ) is None
    assert await ix.resolve(
        interaction_id="", tenant_id=1, user_id="u1", decision={}
    ) is None
    pool.raise_on = "UPDATE pending_interactions"
    assert await ix.resolve(
        interaction_id="int-1", tenant_id=1, user_id="u1", decision={}
    ) is None


# ── consume_grant：续跑回合取走授权 ───────────────────────────────────────


async def test_grant_matches_arguments_not_call_id(pool: _FakePool) -> None:
    pool.row = {
        "id": "int-1",
        "decision": json.dumps({"decision": "approve"}),
        "reason": "看了，可以",
        "decided_by": "u1",
    }
    got = await ix.consume_grant(
        tenant_id=1,
        session_id="s1",
        kind=ix.KIND_APPROVAL,
        tool_name="shell",
        args_hash="h1",
    )
    assert got is not None
    assert got["decision"] == {"decision": "approve"}
    assert got["decided_by"] == "u1"
    sql, args = pool.last_sql, pool.last_args
    norm = _norm(sql)
    # 认票四要素 + 单次消费 + 过期不认
    assert "status = 'answered'" in norm
    assert "AND kind = $3" in norm and "AND tool_name = $4" in norm
    assert "AND args_hash = $5" in norm
    assert "AND expires_at > now()" in norm
    assert "SET status = 'consumed'" in norm
    # `tool_call_id` 只出现在 INSERT 那条语句里，认票 SQL 不涉及它（新 run 的 id 必然不同）
    assert "tool_call_id" not in norm
    assert args == ("s1", 1, ix.KIND_APPROVAL, "shell", "h1")


async def test_grant_refuses_without_a_argument_fingerprint(pool: _FakePool) -> None:
    """空 args_hash 的授权能匹配该工具名下的**任何**一次调用 —— 那是绕过确认，不是介入。"""
    assert await ix.consume_grant(
        tenant_id=1, session_id="s1", kind=ix.KIND_APPROVAL, tool_name="shell", args_hash=""
    ) is None
    assert await ix.consume_grant(
        tenant_id=1, session_id="s1", kind="bogus", tool_name="shell", args_hash="h1"
    ) is None
    assert await ix.consume_grant(
        tenant_id=1, session_id="", kind=ix.KIND_APPROVAL, tool_name="shell", args_hash="h1"
    ) is None
    assert pool.calls == []


async def test_grant_miss_and_failure_both_mean_not_approved(pool: _FakePool) -> None:
    pool.row = None
    kwargs = dict(
        tenant_id=1, session_id="s1",
        kind=ix.KIND_APPROVAL, tool_name="shell", args_hash="h1",
    )
    assert await ix.consume_grant(**kwargs) is None
    pool.raise_on = "UPDATE pending_interactions"
    assert await ix.consume_grant(**kwargs) is None


# ── list_open / expire_stale ──────────────────────────────────────────────


async def test_list_open_only_shows_rows_still_waiting(pool: _FakePool) -> None:
    pool.rows = [
        {
            "id": "int-1", "kind": ix.KIND_APPROVAL, "status": ix.STATUS_PENDING,
            "run_id": "rt1", "turn_id": None, "tool_call_id": "tc1", "tool_name": "shell",
            "args_hash": "h1", "request": '{"question": "q"}', "decision": None,
            "reason": None, "created_at": None, "updated_at": None, "expires_at": None,
        }
    ]
    got = await ix.list_open(tenant_id=1, user_id="u1", session_id="s1")
    assert len(got) == 1
    assert got[0]["request"] == {"question": "q"}
    assert got[0]["decision"] == {}
    norm = _norm(pool.last_sql)
    assert "AND user_id = $3" in norm
    assert "status IN ('pending', 'answered')" in norm
    assert await ix.list_open(tenant_id=1, user_id="u1", session_id="") == []


async def test_expire_stale_marks_rather_than_filters(pool: _FakePool) -> None:
    """过期必须**落状态**：留在 `answered` 会一直占着唯一索引的名额，让重新问静默失败。"""
    pool.status = "UPDATE 3"
    assert await ix.expire_stale(limit=10) == 3
    norm = _norm(pool.last_sql)
    assert "SET status = 'expired'" in norm
    assert "status IN ('pending', 'answered')" in norm
    assert "expires_at <= now()" in norm
    assert pool.last_args == (10,)
    pool.status = "UPDATE 0"
    assert await ix.expire_stale() == 0
    pool.raise_on = "UPDATE pending_interactions"
    assert await ix.expire_stale() == 0


# ── awaiting_input：第八个状态，刻意不属于活跃集合 ────────────────────────


def test_awaiting_input_is_not_an_active_status() -> None:
    assert cp.STATUS_AWAITING_INPUT == "awaiting_input"
    assert len(cp.STATUS_AWAITING_INPUT) <= 16  # agent_runs.status 是 varchar(16)
    # 这一条是整个设计的支点：不在活跃集合 ⇒ 唯一索引不锁会话、reconciler 不可能接管
    assert cp.STATUS_AWAITING_INPUT not in cp.ACTIVE_STATUSES


async def test_load_awaiting_parses_the_suspended_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakePool(row={"checkpoint": json.dumps({"turn_index": 4, "done_tools": ["c1"]}),
                          "run_token": "rt-old"})
    monkeypatch.setattr(cp, "_pool", lambda: fake)
    got = await cp.load_awaiting(tenant_id=1, session_id="s1")
    assert got is not None
    assert got["run_token"] == "rt-old"
    assert got["checkpoint"]["turn_index"] == 4
    norm = _norm(fake.last_sql)
    assert "AND status = 'awaiting_input'" in norm
    # 普通活跃现场读不到挂起行（`load()` 的过滤不动，批 3 修的陈旧快照问题不会复发）
    fake.row = {"checkpoint": {"turn_index": 1}, "run_token": "rt"}
    monkeypatch.setattr(cp, "_pool", lambda: fake)
    assert await cp.load(tenant_id=1, session_id="s1") is not None
    assert "'running', 'checkpointed', 'resuming'" in _norm(fake.calls[-1][0])
    assert "awaiting_input" not in _norm(fake.calls[-1][0])


async def test_load_awaiting_degrades_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cp, "_pool", lambda: _FakePool(row=None))
    assert await cp.load_awaiting(tenant_id=1, session_id="s1") is None
    monkeypatch.setattr(cp, "_pool", lambda: _FakePool(row={"checkpoint": None, "run_token": ""}))
    assert await cp.load_awaiting(tenant_id=1, session_id="s1") is None
    monkeypatch.setattr(cp, "_pool", lambda: _FakePool(raise_on="SELECT checkpoint, run_token"))
    assert await cp.load_awaiting(tenant_id=1, session_id="s1") is None
    monkeypatch.setattr(cp, "_pool", lambda: None)
    assert await cp.load_awaiting(tenant_id=1, session_id="") is None


async def test_claim_awaiting_is_cas_and_rejects_on_unique_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读挂起现场必须与交还同一个动作；交还失败（含撞唯一索引）时调用方按"没续上"走。"""
    fake = _FakePool(status="UPDATE 1")
    monkeypatch.setattr(cp, "_pool", lambda: fake)
    assert await cp.claim_awaiting(tenant_id=1, session_id="s1", new_run_token="rt-new")
    norm = _norm(fake.last_sql)
    assert "SET status = 'resuming', run_token = $3" in norm
    assert "AND status = 'awaiting_input'" in norm
    assert fake.last_args == ("s1", 1, "rt-new")

    monkeypatch.setattr(cp, "_pool", lambda: _FakePool(status="UPDATE 0"))
    assert not await cp.claim_awaiting(tenant_id=1, session_id="s1", new_run_token="rt-new")
    # 会话上已有别的活跃行 ⇒ ux_agent_runs_session_active 冲突，必须**不**抛出去
    monkeypatch.setattr(
        cp, "_pool",
        lambda: _FakePool(raise_on="UPDATE agent_runs", status="UPDATE 1"),
    )
    assert not await cp.claim_awaiting(tenant_id=1, session_id="s1", new_run_token="rt-new")
    monkeypatch.setattr(cp, "_pool", lambda: _FakePool())
    assert not await cp.claim_awaiting(tenant_id=1, session_id="s1", new_run_token="")
    assert not await cp.claim_awaiting(tenant_id=1, session_id="", new_run_token="rt")


async def test_suspend_transition_leaves_the_active_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """`mark(awaiting_input)` 从活跃集合迁出 ⇒ 唯一索引释放、会话可用、巡检看不见。"""
    fake = _FakePool(status="UPDATE 1")
    monkeypatch.setattr(cp, "_pool", lambda: fake)
    assert await cp.mark(
        tenant_id=1,
        session_id="s1",
        status=cp.STATUS_AWAITING_INPUT,
        run_token="rt-1",
    )
    norm = _norm(fake.last_sql)
    assert "'running', 'checkpointed', 'resuming'" in norm
    assert "AND ($4 = '' OR run_token = $4)" in norm
