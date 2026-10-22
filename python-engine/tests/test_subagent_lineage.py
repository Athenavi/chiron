"""M15a：子 Agent 任务线（`root_run_id`）落库的回归守卫。

要守住的缺陷「子 Agent 续跑未计配额」（核心机制剖析那份文档**不在仓库里**）：一条任务线可以被
反复续跑，而每条 run 的 token 只记在自己那一行上，**没有任何口径能把"这条线一共花了多少"问出来**。
本轮先把"这条线是谁"写进库，后面几片（M15b/c）才谈得上上限。

测试分两层，各管一件事：

* 本文件的**单测**管 Python 侧的编排：写入顺序、`claim_source` 的二次写、失败隔离、
  读侧的空值口径，以及"runner 到底有没有把 `parent_run_id` 传下去"这一条曾经**从没被传过**的
  接线。`fetchrow` 的结果是**编程喂进去的**，不是在替身里重算 SQL —— 重算就是拿一份 Python
  副本去证明 SQL 正确，证的是自己。
* 文件末尾标 ``integration`` 的用例打**真 PostgreSQL**（CI 的 real-stack job 用 alembic 建库，
  本地无库自动 skip），那才是 ``ROOT_RESOLVE_SQL`` 的优先级、``LINEAGE_USAGE_SQL`` 的求和、
  以及迁移里那条递归 CTE 的真实语义。
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.subagent import store as store_module
from app.subagent.store import (
    LINEAGE_SQL,
    LINEAGE_USAGE_SQL,
    RESUMED_FROM_SQL,
    ROOT_RESOLVE_SQL,
    RUN_INSERT_SQL,
    SubagentRunStore,
)

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0021_subagent_lineage_root.py"
)


def _norm(sql: str) -> str:
    return " ".join(sql.split())


def _migration_sql_statements() -> list[str]:
    """把迁移里每条 ``op.execute`` 的 SQL 字面量取出来（空白归一）。

    取而不是复制：本文件末尾的 integration 用例要跑**迁移里那一条**递归 CTE，
    在测试里重打一遍 SQL 就等于什么都没验证。
    """
    tree = ast.parse(MIGRATION_PATH.read_text(encoding="utf-8"))
    statements: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            statements.append(" ".join(node.args[0].value.split()))
    return statements


class _LineagePool:
    """记录调用并**按编程设定**回放 fetchrow 结果的池替身。

    刻意不解释 SQL：解释一次就多一份"和真库可能不一样的真相"。这里只回答两个问题 ——
    调了几次、按什么顺序、参数是什么。

    记账用**整条归一化后的 SQL**而不是前缀：`LINEAGE_SQL` 与 `RESUMED_FROM_SQL` 都以
    `UPDATE subagent_runs SET r` 开头，截前 24 个字符会让两条血缘写混成一堆 —— 于是"写了哪几行"
    这个恰恰要守的断言会假绿。
    """

    def __init__(self, *, root: str = "", claim: str | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        #: ROOT_RESOLVE_SQL 的回放值（单测里唯一的"输入"）
        self._root = root
        self._claim = claim
        #: 命中这些 SQL 片段就抛错，用来验证失败隔离
        self.fail_on: list[str] = []
        self.usage_rows: list[tuple[int, int]] = []

    def _maybe_fail(self, sql: str) -> None:
        normalized = _norm(sql)
        for needle in self.fail_on:
            if _norm(needle) in normalized:
                raise RuntimeError(f"boom: {needle}")

    async def execute(self, sql: str, *args: Any) -> str:
        self._maybe_fail(sql)
        self.calls.append((_norm(sql), args))
        return "UPDATE 1"

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self._maybe_fail(sql)
        self.calls.append((_norm(sql), args))
        if _norm(sql) == _norm(LINEAGE_USAGE_SQL):
            tokens, runs = self.usage_rows.pop(0) if self.usage_rows else (0, 0)
            return {"tokens": tokens, "runs": runs}
        if _norm(sql) == _norm(ROOT_RESOLVE_SQL):
            return {"root": self._root, "claim_source": self._claim}
        return None  # pragma: no cover - 不该有第三种读


async def _start(pool: _LineagePool, **kwargs: Any) -> None:
    store = SubagentRunStore(pool)
    await store.start_run(
        run_id=kwargs.pop("run_id", "rs_child"),
        root_session_id="s-main",
        task="t",
        **kwargs,
    )


def _heads(pool: _LineagePool) -> list[str]:
    return [head for head, _ in pool.calls]


def _writes(pool: _LineagePool, sql: str) -> list[tuple[Any, ...]]:
    """按完整 SQL 取回某条语句的所有调用参数。"""
    target = _norm(sql)
    return [args for head, args in pool.calls if head == target]


class TestLineageWriteOrdering:
    async def test_plain_delegation_writes_its_own_root_after_the_insert(self) -> None:
        pool = _LineagePool(root="rs_child", claim=None)
        await _start(pool, run_id="rs_child")

        # INSERT 必须排在血缘之前：解析根的查询读的是同一张表，写序颠倒会让"自己"这一行
        # 还没落库就被查（首轮派发 ⇒ claim 为 NULL，但根查询依赖来源行存在）。
        heads = _heads(pool)
        assert heads[0] == _norm(RUN_INSERT_SQL)
        assert heads.index(_norm(ROOT_RESOLVE_SQL)) > 0
        # 没有来源（顶层、非续跑）⇒ 只写自己这一行，一次都不该多写
        assert len(_writes(pool, LINEAGE_SQL)) == 1
        assert pool.calls[-1] == (_norm(LINEAGE_SQL), ("rs_child", "rs_child"))

    async def test_resume_is_resolved_before_the_link_is_written(self) -> None:
        """顺序即正确性：``ROOT_RESOLVE_SQL`` 靠 `resumed_from` 找来源，写在后面就找不到。

        这条测试是本轮最容易悄悄回归的一处 —— 把 `RESUMED_FROM_SQL` 与血缘写换个顺序，
        所有函数仍然"成功"，只是每次续跑都开一条**新线**，于是"续跑次数 × 额度"这个缺陷
        原地复活，而库里的数据看起来完全正常。
        """
        pool = _LineagePool(root="rs_first", claim=None)
        await _start(pool, run_id="rs_child", resumed_from="rs_first")

        heads = _heads(pool)
        assert heads.index(_norm(RESUMED_FROM_SQL)) < heads.index(_norm(ROOT_RESOLVE_SQL))
        resolve_args = _writes(pool, ROOT_RESOLVE_SQL)[-1]
        # (自己的 id, 续跑来源, 父 run) —— 第 2 位必须是来源，否则优先级失效
        assert resolve_args == ("rs_child", "rs_first", "")
        assert pool.calls[-1] == (_norm(LINEAGE_SQL), ("rs_child", "rs_first"))

    async def test_a_source_row_without_a_root_is_pulled_into_the_line(self) -> None:
        """`claim_source` 非空 ⇒ 再写一次，把**来源行**也归到这条线。

        少了这一次写，来源那条 run 的 `root_run_id` 还是 NULL，求和就漏掉它自己花过的 token ——
        漏的是"这条线已经花了多少"的**第一段**，恰好是最贵的那段。
        """
        pool = _LineagePool(root="rs_first", claim="rs_first")
        await _start(pool, run_id="rs_child", resumed_from="rs_first")

        assert _writes(pool, LINEAGE_SQL) == [
            ("rs_child", "rs_first"),
            ("rs_first", "rs_first"),
        ]

    async def test_the_write_is_conditional_so_a_root_never_moves(self) -> None:
        """`WHERE ... AND root_run_id IS NULL`：血缘一旦定了就不该被后续写覆盖。

        换根会把已经计入这条线的用量从新根下抹掉 —— 在预算账上，那是"花掉又消失"的静默。
        """
        assert "AND root_run_id IS NULL" in LINEAGE_SQL
        # 来源行的认领同样只填空值，绝不改写已有根
        assert LINEAGE_SQL.count("root_run_id IS NULL") == 1
        assert "UPDATE subagent_runs SET root_run_id = $2 WHERE id = $1" in LINEAGE_SQL

    async def test_nested_delegation_passes_the_parent_run(self) -> None:
        """`parent_run_id` 是本轮新传的参数：历史上调用点根本没传 ⇒ 库里那一列全是 NULL。"""
        pool = _LineagePool(root="rs_parent", claim=None)
        await _start(pool, run_id="rs_child", parent_run_id="rs_parent")

        insert_args = pool.calls[0][1]
        # RUN_INSERT_SQL 的第 4 位是 parent_run_id（$4）
        assert insert_args[3] == "rs_parent"
        assert _writes(pool, ROOT_RESOLVE_SQL) == [("rs_child", "", "rs_parent")]


class TestFailureIsolation:
    async def test_lineage_failure_does_not_break_the_run(self, caplog) -> None:
        """未执行迁移 0021 的库：只丢血缘，派发落库照常（与 0004/0006/0008 同一惯例）。"""
        pool = _LineagePool()
        pool.fail_on.append("root_run_id = $2 WHERE id = $1")
        import logging

        with caplog.at_level(logging.WARNING):
            await _start(pool, run_id="rs_child")

        assert _norm(RUN_INSERT_SQL) in _heads(pool)  # 起始行写成了
        assert store_module.SubagentRunStore(pool).available is True  # 没被标 broken
        assert "0021_subagent_lineage_root" in caplog.text

    async def test_warning_is_logged_once_per_store_not_per_dispatch(self, caplog) -> None:
        """闸门每回合都要读线账；读不通时不该把日志刷满。"""
        pool = _LineagePool()
        pool.fail_on.append("FROM subagent_runs WHERE root_run_id = $1")
        store = SubagentRunStore(pool)
        import logging

        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                assert await store.lineage_usage("rs_root", "t1") is None

        assert caplog.text.count("lineage_usage") == 1


class TestLineageUsageRead:
    async def test_tenant_is_part_of_the_query(self) -> None:
        pool = _LineagePool()
        pool.usage_rows.append((1200, 3))
        store = SubagentRunStore(pool)
        assert await store.lineage_usage("rs_root", "t1") == (1200, 3)
        head, args = pool.calls[-1]
        assert head == _norm(LINEAGE_USAGE_SQL)
        assert args == ("rs_root", "t1")

    @pytest.mark.parametrize(
        "root,tenant",
        [("", "t1"), ("rs_root", ""), ("", "")],
        ids=["no-root", "no-tenant", "neither"],
    )
    async def test_missing_key_returns_none_not_zero(self, root: str, tenant: str) -> None:
        """``None``（问不出来）与 ``(0, 0)``（确实没花）必须能分开。

        混成一个值，闸门就会在缺租户的派发上判成"这条线还有额度"。
        """
        pool = _LineagePool()
        store = SubagentRunStore(pool)
        assert await store.lineage_usage(root, tenant) is None
        assert pool.calls == []

    async def test_unavailable_store_does_not_query(self) -> None:
        store = SubagentRunStore(None)
        assert store.available is False
        assert await store.lineage_usage("rs_root", "t1") is None

    async def test_row_level_failure_returns_none(self) -> None:
        """列缺失（未迁移）⇒ None，而不是把整个 store 降级。

        `_degrade` 会让子 Agent 落库整体停摆；一个读失败不该有这个爆炸半径。
        """
        pool = _LineagePool()
        pool.fail_on.append("FROM subagent_runs WHERE root_run_id = $1")
        store = SubagentRunStore(pool)
        assert await store.lineage_usage("rs_root", "t1") is None
        assert store.available is True


class TestRunnerWiring:
    """`parent_run_id` 曾经只算在落库**之后**（只喂事件与运行期缓存）⇒ 库里那一列全是 NULL。

    这里驱动真实的 `SubAgentRunner.run()`，断言的是"落库收到的参数"，不是源码形状。
    """

    async def _drive(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        explicit_parent: str = "",
        context_parent: str = "",
        resumed_from: str = "",
        resume_at_step: int = 0,
    ) -> dict[str, Any]:
        import asyncio

        from app.agent import runtime as runtime_mod
        from app.agent import subagent_runner as runner_module
        from app.agent.subagent_runner import SubAgentRunner

        captured: dict[str, Any] = {}

        class _Store:
            available = True

            async def start_run(self, **kwargs: Any) -> None:
                captured.update(kwargs)

            async def add_step(self, *args: Any, **kwargs: Any) -> None:
                return None

            async def flush_steps(self, *args: Any, **kwargs: Any) -> None:
                return None

            async def finish_run(self, *args: Any, **kwargs: Any) -> None:
                return None

            async def mark_lifecycle(self, *args: Any, **kwargs: Any) -> None:
                return None

        async def _one_step_run(self: Any, task: Any):  # noqa: ANN001
            yield runtime_mod.AgentEvent(type="text", content="done")

        monkeypatch.setattr(runtime_mod.AgentRuntime, "run", _one_step_run)
        # `_context_run_id()` 读的是父层 tool contextvar；直接喂一个稳定值，别依赖用例顺序。
        monkeypatch.setattr(runner_module, "_context_run_id", lambda: context_parent)

        runner = SubAgentRunner(
            gateway=object(),
            store=_Store(),  # type: ignore[arg-type]
            parent_session_id="s-main",
            parent_run_id=explicit_parent,
            tenant_id="t1",
            user_id="u1",
            resumed_from=resumed_from,
            resume_at_step=resume_at_step,
        )
        await asyncio.wait_for(
            runner.run("跑一件事", profile_ref="", mode="normal", max_turns=1), timeout=10
        )
        return captured

    async def test_nested_delegation_records_the_context_parent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = await self._drive(monkeypatch, context_parent="rs-parent")
        assert captured["parent_run_id"] == "rs-parent"

    async def test_explicit_parent_wins_over_the_context_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = await self._drive(
            monkeypatch, explicit_parent="rs-explicit", context_parent="rs-context"
        )
        assert captured["parent_run_id"] == "rs-explicit"

    async def test_top_level_delegation_records_no_parent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """顶层委派**本来就没有父**：写空串（store 侧 ⇒ 以自己的 id 开一条新线）。"""
        captured = await self._drive(monkeypatch)
        assert captured["parent_run_id"] == ""

    async def test_resume_lineage_still_reaches_the_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """续跑血缘与父子血缘是两条独立的列，闸门靠前者优先 —— 两条都得传下去。"""
        captured = await self._drive(
            monkeypatch,
            explicit_parent="rs-parent",
            resumed_from="rs-earlier-attempt",
            resume_at_step=7,
        )
        assert captured["resumed_from"] == "rs-earlier-attempt"
        assert captured["resume_at_step"] == 7
        assert captured["parent_run_id"] == "rs-parent"


class TestMigrationContract:
    """迁移文件本身是账本单位定义的一部分：口径不该在下一轮被顺手改掉。"""

    def _sql_statements(self) -> list[str]:
        return _migration_sql_statements()

    def test_chain_is_linear_and_head_is_this_migration(self) -> None:
        src = MIGRATION_PATH.read_text(encoding="utf-8")
        assert 'revision: str = "0021_subagent_lineage_root"' in src
        assert 'down_revision: Union[str, Sequence[str], None] = "0020_memory_summary_embedding_space"' in src

    def test_backfill_walks_resumed_from_only(self) -> None:
        """口径写死：回填只沿 `resumed_from`。

        把它扩到 `rerun_of`（"按原任务再派一次"）会把用户主动重复劳动并进同一条线，那是另一个
        产品决策；沿 `parent_run_id` 也无意义 —— 历史行那一列全是 NULL，树不可重建。
        """
        backfill = [s for s in self._sql_statements() if "WITH RECURSIVE" in s]
        assert len(backfill) == 1
        sql = backfill[0]
        assert "r.resumed_from = c.id" in sql
        assert "rerun_of" not in sql
        assert "parent_run_id" not in sql
        # 去重而不是 UNION ALL：成环时让环上的行留在 NULL（"未知"），而不是让迁移转圈
        assert "UNION " in sql and "UNION ALL" not in sql

    def test_column_and_index_are_idempotent(self) -> None:
        statements = self._sql_statements()
        assert any("ADD COLUMN IF NOT EXISTS root_run_id" in s for s in statements)
        assert any(
            "CREATE INDEX IF NOT EXISTS ix_subagent_runs_root_run_id" in s for s in statements
        )

    def test_no_plpgsql_block(self) -> None:
        """本仓库的迁移从没有 `DO $$` 先例；不在这里引入未验证的写法。"""
        assert "$$" not in MIGRATION_PATH.read_text(encoding="utf-8")


@pytest.mark.integration
class TestRealPostgresSemantics:
    """真库语义：优先级、认领、求和、回填 CTE —— 这些只能由 PostgreSQL 判定。

    CI 的 real-stack job 用 alembic 建库并跑 `-m "integration or live"`；本地无库时整类
    自跳。上面的单测证明的是"Python 侧编排没写错"，这里证明的才是"SQL 表达的是同一件事"。
    """

    _created: list[str] = []

    @pytest.fixture(autouse=True)
    async def _cleanup(self):
        from app.db import get_pool

        self._created = []
        yield
        pool = get_pool()
        for run_id in self._created:
            await pool.execute("DELETE FROM subagent_runs WHERE id = $1", run_id)

    def _id(self, tag: str) -> str:
        run_id = f"lin_{uuid.uuid4().hex[:8]}_{tag}"
        self._created.append(run_id)
        return run_id

    async def _seed(
        self,
        *,
        root: str | None = "self",
        parent: str | None = None,
        resumed: str | None = None,
        tenant: str | None = "t1",
        tokens: tuple[int, int] = (0, 0),
    ) -> str:
        from app.db import get_pool

        run_id = self._id("seed")
        await get_pool().execute(
            """
            INSERT INTO subagent_runs
                (id, root_session_id, tenant_id, task, status,
                 input_tokens, output_tokens, parent_run_id, resumed_from, root_run_id)
            VALUES ($1, 's-lineage', $2, 'seed', 'completed', $3, $4, $5, $6, $7)
            """,
            run_id,
            tenant,
            tokens[0],
            tokens[1],
            parent,
            resumed,
            (run_id if root == "self" else root),
        )
        return run_id

    async def _root_of(self, run_id: str) -> str | None:
        from app.db import get_pool

        return await get_pool().fetchval(
            "SELECT root_run_id FROM subagent_runs WHERE id = $1", run_id
        )

    async def _start(self, **kwargs: Any) -> None:
        from app.db import get_pool

        store = SubagentRunStore(get_pool())
        await store.start_run(
            run_id=kwargs.pop("run_id"),
            root_session_id="s-lineage",
            task="派一件活",
            tenant_id=kwargs.pop("tenant_id", "t1"),
            **kwargs,
        )

    async def test_resume_lineage_beats_the_parent(self) -> None:
        tree_root = await self._seed()
        # ⚠ `_seed` 的 `root` 默认是 `"self"`（把自己当根）—— 这里必须显式把 child 挂到
        # `tree_root` 那条线上，否则 `child.root_run_id` 会等于 child 自己，`new_run` 的血缘
        # 解析会落到 `COALESCE` 的第二项（`resumed_row`）而不是树根。原用例漏了这个参数，
        # 一直是红的（`integration` 标记，真库出现之前从没跑过）。
        child = await self._seed(parent=tree_root, root=tree_root)
        other_tree = await self._seed()

        new_run = self._id("new")
        await self._start(
            run_id=new_run, resumed_from=child, parent_run_id=other_tree
        )

        # 取树根的线，而不是"另起一棵树"：这正是"续跑不该各自有额度"的落点
        assert await self._root_of(new_run) == tree_root
        # 两条线的根都不该被这次写动过
        assert await self._root_of(child) == tree_root
        assert await self._root_of(other_tree) == other_tree

    async def test_source_without_a_root_is_claimed_into_the_line(self) -> None:
        """来源行自己还没有根 ⇒ 把它拉进来，否则求和漏掉它花过的那一段。"""
        orphan = await self._seed(root=None)
        new_run = self._id("new")
        await self._start(run_id=new_run, resumed_from=orphan)

        assert await self._root_of(new_run) == orphan
        assert await self._root_of(orphan) == orphan

    async def test_top_level_delegation_starts_its_own_line(self) -> None:
        new_run = self._id("new")
        await self._start(run_id=new_run)
        assert await self._root_of(new_run) == new_run

    async def test_dangling_source_falls_back_to_self(self) -> None:
        """来源行不存在（被清理掉的老数据）⇒ 自己开线，而不是 NULL。

        NULL 会让闸门"问不出这条线花了多少"；开新线是**少算**，与跨线多算相比是可接受的一侧。
        """
        new_run = self._id("new")
        await self._start(run_id=new_run, resumed_from="lin_does_not_exist")
        assert await self._root_of(new_run) == new_run

    async def test_an_existing_root_is_never_moved(self) -> None:
        seeded = await self._seed(root="pre_existing_root")
        await self._start(run_id=seeded)  # INSERT 走 ON CONFLICT DO NOTHING
        assert await self._root_of(seeded) == "pre_existing_root"

    async def test_usage_sums_this_line_and_tenant_only(self) -> None:
        from app.db import get_pool

        line = self._id("line")
        a = await self._seed(root=line, tokens=(100, 50))
        b = await self._seed(root=line, tokens=(20, 10))
        other_tenant = await self._seed(root=line, tokens=(7, 7), tenant="t2")
        no_tenant = await self._seed(root=line, tokens=(999, 999), tenant=None)
        other_line = await self._seed(tokens=(500, 500))
        store = SubagentRunStore(get_pool())

        tokens, runs = await store.lineage_usage(line, "t1")
        assert (tokens, runs) == (180, 2)

        # 缺租户 ⇒ None（问不出来），不是 (0, 0)
        assert await store.lineage_usage(line, "") is None
        # 别的线读不到这条线的行
        assert await store.lineage_usage(other_line, "t1") == (1000, 1)

        for rid in (a, b, other_tenant, no_tenant, other_line):
            assert await self._root_of(rid) is not None

    async def test_backfill_walks_the_resume_chain_and_leaves_the_tree_alone(self) -> None:
        """迁移里那条递归 CTE 的真实行为：续跑链归并到链顶，父子关系**不**参与回填。

        历史行的 `parent_run_id` 全是 NULL（本轮才修好），所以旧嵌套树只能各成一线 ——
        测试把这个**已知少算**钉成显式断言，而不是留在注释里等人忘记。
        """
        from app.db import get_pool

        backfill = [s for s in _migration_sql_statements() if "WITH RECURSIVE" in s]
        assert len(backfill) == 1

        top = await self._seed(root=None)
        mid = await self._seed(root=None, resumed=top)
        leaf = await self._seed(root=None, resumed=mid)
        nested = await self._seed(root=None, parent=top)

        await get_pool().execute(backfill[0])

        assert await self._root_of(top) == top
        assert await self._root_of(mid) == top
        assert await self._root_of(leaf) == top
        # 树不可重建 ⇒ 子行自开一线（少算），但绝不并进别人的线（不多算）
        assert await self._root_of(nested) == nested

        # 幂等：再跑一次不改任何东西
        await get_pool().execute(backfill[0])
        assert await self._root_of(leaf) == top
