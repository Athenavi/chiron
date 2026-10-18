"""M15b：任务线账（run 级预算的第二道闸门）。

**这里能证明什么、不能证明什么**

Lua 脚本的算术**不在这里**判定：桩里的 Python 镜像按自己写的规则算，那不是脚本的规则，
"桩说脚本会这么算"没有证据价值（与 `tests/test_subagent_lineage.py` 同一取舍）。分层因此是：

* 单测：Python 侧编排 —— 发**哪个脚本、哪些参数**、取不到时退回什么、闸门在 runtime 的
  哪一步介入、runner 有没有把线根与预留句柄传下去；
* ``@pytest.mark.integration``：**真 Redis** 判 `_RSVD_LUA` / `_USED_*_LUA` 的实际算术
  （剔除自己、按 score 清过期、镜像不复活、回填只用 NX），**真 PG** 判控制态覆盖与跨租户过滤。

标记的门槛在 `tests/conftest.py`：`integration` 用例只有在 `POSTGRES_DSN` 已设置时才跑，本地
没设就**整类 skip**（不是"红"）—— 真跑由 CI 的 real-stack job 注入 DSN 并先 `alembic upgrade head`。
`redis_client` 那个 fixture 另有一层：DSN 设了但 Redis 连不上时它是 `pytest.fail`，不会静默跳。
"""

from __future__ import annotations

import ast
import os
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.subagent import line_ledger as ledger_mod
from app.subagent.line_ledger import (
    _TAB,
    CAP_UNLIMITED,
    LEDGER_SELECT_SQL,
    LineLedger,
    LineState,
    _usable_root,
    default_cap,
)

_REDIS_URL = (
    os.getenv("CHIRON_TEST_REDIS_URL") or os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
)

_MIGRATION_0022 = (
    Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0022_run_lineage_ledger.py"
)


def _migration_sql_statements() -> list[str]:
    """把迁移里每条 ``op.execute`` 的 SQL 字面量取出来（空白归一），而不是在测试里重打一遍。"""
    tree = ast.parse(_MIGRATION_0022.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            out.append(" ".join(str(node.args[0].value).split()))
    return out


class _FakeStore:
    """只实现 `lineage_usage` —— 线账用它取"已结算"，不自己再写一遍求和 SQL。"""

    def __init__(self, usage: tuple[int, int] | None = (0, 0)) -> None:
        self.usage = usage
        self.calls: list[tuple[str, str]] = []

    async def lineage_usage(self, root_run_id: str, tenant_id: str) -> tuple[int, int] | None:
        self.calls.append((root_run_id, tenant_id))
        return self.usage


class _FakePool:
    def __init__(self, row: dict[str, Any] | None = None, *, error: bool = False) -> None:
        self.row = row
        self.error = error
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((" ".join(sql.split()), args))
        if self.error:
            raise RuntimeError('relation "run_lineage_ledger" does not exist')
        return self.row


class _FakeRedis:
    """记录 `get` / `eval` 的调用；**不解释 Lua**。

    `eval` 的返回值可按脚本里的标志子串挑（`results`），挑不到用 `result` 兜底：一次 `state`
    会发两三个不同脚本（镜像回填 / 预留求和 / 终态累加），只给一个常量就没法把"结算 600 +
    预留 300"这种两笔账分开喂进去。`got` 是镜像键的当前值（``None`` = 键不存在）。
    """

    def __init__(
        self,
        *,
        result: Any = "0",
        results: dict[str, Any] | None = None,
        got: Any = None,
        error: bool = False,
    ) -> None:
        self.result = result
        self.results = results or {}
        self.got = got
        self.error = error
        self.calls: list[tuple[str, tuple]] = []
        self.gets: list[str] = []

    async def get(self, key: str) -> Any:
        self.gets.append(key)
        if self.error:
            raise RuntimeError("redis down")
        return self.got

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        self.calls.append((" ".join(script.split()), args))
        if self.error:
            raise RuntimeError("redis down")
        for marker, value in self.results.items():
            if marker in script:
                return value
        return self.result

    def args_for(self, marker: str) -> list[tuple]:
        return [a for head, a in self.calls if marker in head]


async def _no_redis() -> Any:
    raise RuntimeError("no redis configured")


def _ledger(
    *,
    pool: _FakePool | None = None,
    redis: Any = None,
    store: _FakeStore | None = None,
) -> LineLedger:
    """默认**没有 Redis** 的线账：`_redis_conn` 钉成"连不上"，走纯 PG 口径的分支。"""
    led = LineLedger(pool=pool or _FakePool(), store=store or _FakeStore(), redis=redis)
    if redis is None:
        led._redis_conn = _no_redis  # type: ignore[method-assign]
    return led


class _StubLedger:
    """按调用顺序可编程返回轴名的替身（`axes[i]` 对应第 i 次 `check`）。"""

    def __init__(self, *, axis: str = "", axes: list[str] | None = None, error: bool = False) -> None:
        self.axis = axis
        self.axes = axes
        self.error = error
        self.calls: list[Any] = []

    async def check(self, root: str, tenant_id: str, *, run_id: str = "", own_tokens: int = 0) -> str:
        self.calls.append(("check", root, tenant_id, run_id, own_tokens))
        if self.error:
            raise RuntimeError("boom")
        if self.axes is not None:
            idx = sum(1 for c in self.calls if c[0] == "check") - 1
            return self.axes[idx] if idx < len(self.axes) else ""
        return self.axis

    async def reserve(self, root: str, tenant_id: str, member: str, tokens: int) -> str:
        self.calls.append(("reserve", root, member, tokens))
        return f"{member}{_TAB}{tokens}"

    async def settle(self, root: str, handle: str, actual_tokens: int) -> None:
        self.calls.append(("settle", root, handle, actual_tokens))


# ── 算术与优先级（纯函数，不需要任何依赖）───────────────────────────────


class TestLineStateMath:
    def _state(self, **kw: Any) -> LineState:
        base: dict[str, Any] = dict(
            root="rs_root", tenant_id="t1", cap=1000, settled=0, reserved=0,
            paused=False, pause_reason="",
        )
        base.update(kw)
        return LineState(**base)

    def test_charged_adds_all_three_terms(self) -> None:
        assert self._state(settled=100, reserved=50).charged(30) == 180

    def test_negative_own_tokens_do_not_buy_back_budget(self) -> None:
        """`own` 为负（上游算错）不能把已花掉的额度"退"回去。"""
        assert self._state(settled=100).charged(-999) == 100

    def test_trip_is_at_or_above_cap(self) -> None:
        assert self._state(cap=100, settled=99).violation(0) == ""
        assert self._state(cap=100, settled=100).violation(0) == "line_tokens"

    def test_paused_beats_tokens(self) -> None:
        """人按下的暂停与"数算到了上限"是两件事，上层要据此决定是等人还是判失败。"""
        assert self._state(paused=True, cap=100, settled=1).violation(0) == "line_paused"

    @pytest.mark.parametrize("cap", [0, CAP_UNLIMITED])
    def test_cap_zero_or_minus_one_never_trips(self, cap: int) -> None:
        assert self._state(cap=cap, settled=10**9).violation(10**9) == ""

    def test_describe_carries_the_numbers_the_operator_needs(self) -> None:
        text = self._state(settled=7, reserved=3, cap=10, paused=True, pause_reason="budget").describe()
        assert "root=rs_root" in text and "settled=7" in text and "reserved=3" in text
        assert "cap=10" in text and "paused=1" in text and "budget" in text


class TestCapResolution:
    async def test_no_row_means_follow_the_default(self) -> None:
        """没有控制态行 ≠ 问不出来：它就是"这条线没被单独设过"。"""
        led = _ledger(store=_FakeStore(usage=(123, 1)))
        state = await led.state("rs_root", "t1")
        assert state is not None
        assert (state.settled, state.cap, state.paused) == (123, default_cap(), False)

    async def test_row_cap_zero_falls_through_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("SUBAGENT_LINE_MAX_TOKENS", "4321")
        led = _ledger(pool=_FakePool(row={"cap_tokens": 0, "paused": False, "pause_reason": None}))
        state = await led.state("rs_root", "t1")
        assert state is not None and state.cap == 4321

    async def test_explicit_row_cap_wins_over_default(self, monkeypatch) -> None:
        monkeypatch.setenv("SUBAGENT_LINE_MAX_TOKENS", "4321")
        led = _ledger(pool=_FakePool(row={"cap_tokens": 7000, "paused": False, "pause_reason": ""}))
        state = await led.state("rs_root", "t1")
        assert state is not None and state.cap == 7000

    async def test_unlimited_marker_disables_the_axis(self) -> None:
        """`-1`（明确不限）与 `0`（没设过 ⇒ 跟随默认）必须能区分 —— 否则改默认值会悄悄
        关掉一条被显式放行的大线。"""
        led = _ledger(
            pool=_FakePool(row={"cap_tokens": CAP_UNLIMITED, "paused": False, "pause_reason": None})
        )
        state = await led.state("rs_root", "t1")
        assert state is not None
        assert state.cap == 0 and state.violation(10**9) == ""

    def test_default_cap_env_override_and_garbage(self, monkeypatch) -> None:
        monkeypatch.delenv("SUBAGENT_LINE_MAX_TOKENS", raising=False)
        assert default_cap() == ledger_mod.DEFAULT_LINE_MAX_TOKENS
        monkeypatch.setenv("SUBAGENT_LINE_MAX_TOKENS", "12345")
        assert default_cap() == 12345
        monkeypatch.setenv("SUBAGENT_LINE_MAX_TOKENS", "not-a-number")
        assert default_cap() == ledger_mod.DEFAULT_LINE_MAX_TOKENS


# ── 问不出来 ⇒ 放行（fail-open）──────────────────────────────────────────


class TestUnaskableFailsOpen:
    @pytest.mark.parametrize("root,tenant", [("", "t1"), ("rs_root", ""), ("", "")])
    async def test_missing_keys_ask_nothing(self, root: str, tenant: str) -> None:
        pool = _FakePool()
        store = _FakeStore()
        led = _ledger(pool=pool, store=store)
        assert await led.state(root, tenant) is None
        assert pool.calls == [] and store.calls == []  # 连查询都不该发

    async def test_pg_failure_is_fail_open(self) -> None:
        led = _ledger(pool=_FakePool(error=True))
        assert await led.state("rs_root", "t1") is None
        assert await led.check("rs_root", "t1") == ""

    async def test_usage_aggregate_unavailable_is_fail_open(self) -> None:
        """求和问不出来（0021 的列还没迁移）⇒ 整条判定退回"没有这条线"，
        既不判超限也不炸。"""
        led = _ledger(store=_FakeStore(usage=None))
        assert await led.state("rs_root", "t1") is None

    def test_root_must_be_a_key_safe_id(self) -> None:
        """run id 会进 Redis 键名与 Lua 参数：带 TAB / 空白 / 控制字符的 id 能把拼出来的
        预留 member 变成另一条记录（键空间与账目的注入面）。"""
        assert _usable_root("rs_0123abcdef") is True
        for bad in ("", "rs\t1", "rs 1", "rs\n1", "x" * 65, "rs/../etc"):
            assert _usable_root(bad) is False, bad

    async def test_state_ignores_an_unusable_root(self) -> None:
        pool = _FakePool()
        redis = _FakeRedis()
        led = _ledger(pool=pool, redis=redis)
        assert await led.state("bad\troot", "t1") is None
        assert pool.calls == [] and redis.calls == []


# ── Redis 编排（发哪个脚本、什么参数；失败退回什么）────────────────────


class TestRedisOrchestration:
    async def test_mirror_miss_asks_pg_and_backfills_with_nx(self) -> None:
        redis = _FakeRedis()  # got=None ⇒ 键不存在
        store = _FakeStore(usage=(777, 3))
        led = _ledger(redis=redis, store=store)
        state = await led.state("rs_root", "t1")
        assert state is not None and state.settled == 777
        assert redis.gets and redis.gets[0].endswith("subagent:line:rs_root:used")
        backfill = redis.args_for("'NX'")
        assert len(backfill) == 1
        assert backfill[0][0].endswith("subagent:line:rs_root:used")
        assert backfill[0][1:] == ("777", str(ledger_mod.USED_TTL_SECONDS))

    async def test_mirror_hit_never_asks_pg(self) -> None:
        """快路径的全部意义：镜像还在就**一次都不问**求和 —— fan-out 时闸门每轮都被问。"""
        redis = _FakeRedis(got="777")
        store = _FakeStore(usage=(9999, 9))
        led = _ledger(redis=redis, store=store)
        state = await led.state("rs_root", "t1")
        assert state is not None and state.settled == 777
        assert store.calls == []
        # 命中时**不问 PG、也不回填**；预留求和脚本照发（那是另一条键、另一笔账）
        assert [a for head, a in redis.calls if a[0].endswith("subagent:line:rs_root:used")] == []

    async def test_garbage_mirror_value_falls_back_to_pg(self) -> None:
        redis = _FakeRedis(got="not-a-number")
        led = _ledger(redis=redis, store=_FakeStore(usage=(420, 1)))
        state = await led.state("rs_root", "t1")
        assert state is not None and state.settled == 420

    async def test_mirror_failure_keeps_the_pg_value(self) -> None:
        """Redis 抖了不该让闸门失明：读不到镜像就按 PG 的权威值继续。"""
        led = _ledger(redis=_FakeRedis(error=True), store=_FakeStore(usage=(900, 2)))
        state = await led.state("rs_root", "t1")
        assert state is not None and state.settled == 900

    async def test_no_redis_at_all_still_reports_settled(self) -> None:
        led = _ledger(store=_FakeStore(usage=(900, 2)))
        state = await led.state("rs_root", "t1")
        assert state is not None and (state.settled, state.reserved) == (900, 0)

    async def test_reserve_sends_handle_and_expiry(self) -> None:
        redis = _FakeRedis(result="200000")
        led = _ledger(redis=redis)
        handle = await led.reserve("rs_root", "t1", "sub_rs_1", 200_000)
        assert handle == f"sub_rs_1{_TAB}200000"
        args = redis.args_for("ZREM")
        assert len(args) == 1
        key, now, add, expiry, delete, skip, ttl = args[0]
        assert key.endswith("subagent:line:rs_root:rsv")
        assert add == handle and delete == "" and skip == "sub_rs_1"
        assert int(expiry) > int(now) and ttl == str(ledger_mod.RESERVE_TTL_SECONDS)

    async def test_zero_token_run_reserves_nothing(self) -> None:
        """per-run token 轴为 0（不限）时**不预留**：预留一个猜出来的值只会把并发卡死，
        而这一 run 自己本来就没有 token 轴可自控。"""
        redis = _FakeRedis()
        led = _ledger(redis=redis)
        assert await led.reserve("rs_root", "t1", "sub_rs_1", 0) == ""
        assert redis.calls == []

    async def test_reserve_failure_degrades_to_settled_only(self) -> None:
        led = _ledger(redis=_FakeRedis(error=True))
        assert await led.reserve("rs_root", "t1", "sub_rs_1", 1000) == ""

    async def test_settle_releases_then_bumps(self) -> None:
        redis = _FakeRedis(result="0")
        led = _ledger(redis=redis)
        await led.settle("rs_root", f"sub_rs_1{_TAB}200000", 4321)
        assert len(redis.args_for("ZREM")) == 1
        bumps = redis.args_for("EXISTS")
        assert len(bumps) == 1
        assert bumps[0][0].endswith("subagent:line:rs_root:used")
        assert bumps[0][1:] == ("4321", str(ledger_mod.USED_TTL_SECONDS))
        # 顺序：先摘自己的预留，再累实际值（比的是**两次调用的先后**，不是脚本内的偏移）
        at = {
            marker: next(i for i, (head, _) in enumerate(redis.calls) if marker in head)
            for marker in ("ZREM", "EXISTS")
        }
        assert at["ZREM"] < at["EXISTS"]

    async def test_settle_without_handle_only_bumps(self) -> None:
        redis = _FakeRedis(result="0")
        led = _ledger(redis=redis)
        await led.settle("rs_root", "", 10)
        assert redis.args_for("ZREM") == []
        assert len(redis.args_for("EXISTS")) == 1

    async def test_zero_actual_tokens_does_not_touch_the_mirror(self) -> None:
        redis = _FakeRedis(result="0")
        led = _ledger(redis=redis)
        await led.settle("rs_root", "", 0)
        assert redis.args_for("EXISTS") == []

    async def test_settle_never_raises(self) -> None:
        """预留句柄摘不掉没关系：score 到点自己消失。这里必须**不**把异常抛进收尾链路。"""
        led = _ledger(redis=_FakeRedis(error=True))
        await led.settle("rs_root", "x", 10)

    async def test_check_uses_the_states_arithmetic(self) -> None:
        """`check` 只看 `state` 的算术，不自己再拼一遍口径。

        两笔账分开喂：镜像读（`GET`）给已结算 600，预留求和（`ZRANGE`）给在途 300 ——
        上限 1000 时"自己再花 99"仍在线内、"花 100"正好触到 `>=`。
        """
        led = _ledger(
            pool=_FakePool(row={"cap_tokens": 1000, "paused": False, "pause_reason": None}),
            redis=_FakeRedis(results={"ZRANGE": "300"}),
            store=_FakeStore(usage=(600, 3)),
        )
        assert await led.check("rs_root", "t1", run_id="sub_x", own_tokens=99) == ""
        assert await led.check("rs_root", "t1", run_id="sub_x", own_tokens=100) == "line_tokens"


# ── runtime 的介入点 ────────────────────────────────────────────────────


class _FakeGateway:
    """只提供 `chat_stream`；用例自己往里塞 generator。"""

    chat_stream: Any = None


class TestRuntimeGate:
    def _task(self, **kw: Any) -> Any:
        from app.agent.runtime import AgentTask

        base: dict[str, Any] = dict(
            id="sub_rs_child", tenant_id="t7", user_id="u", session_id="s", content="hi"
        )
        base.update(kw)
        return AgentTask(**base)

    async def test_no_line_means_no_ledger_touch(self, monkeypatch) -> None:
        from app.agent.runtime import AgentRuntime

        stub = _StubLedger(axis="line_tokens")
        monkeypatch.setattr(ledger_mod, "get_line_ledger", lambda: stub)
        runtime = AgentRuntime(gateway=None)
        assert await runtime._line_violation(self._task(), own_tokens=10) == ""
        assert stub.calls == []

    async def test_gate_passes_line_tenant_run_and_own_tokens(self, monkeypatch) -> None:
        from app.agent.runtime import AgentRuntime

        stub = _StubLedger(axis="line_tokens")
        monkeypatch.setattr(ledger_mod, "get_line_ledger", lambda: stub)
        runtime = AgentRuntime(gateway=None)
        task = self._task(subagent_line="rs_root")
        assert await runtime._line_violation(task, own_tokens=300) == "line_tokens"
        # run_id 传的是 task.id —— 它同时是预留 member，闸门据此把自己从预留求和里剔掉
        assert stub.calls == [("check", "rs_root", "t7", "sub_rs_child", 300)]

    async def test_ledger_blowup_does_not_stop_the_turn(self, monkeypatch) -> None:
        from app.agent.runtime import AgentRuntime

        stub = _StubLedger(error=True)
        monkeypatch.setattr(ledger_mod, "get_line_ledger", lambda: stub)
        runtime = AgentRuntime(gateway=None)
        task = self._task(subagent_line="rs_root")
        assert await runtime._line_violation(task, own_tokens=1) == ""

    async def _drive_run(
        self, monkeypatch, *, stub: _StubLedger, task: Any, tool_turns: int = 0
    ) -> tuple[list[Any], list[dict]]:
        """跑真 `runtime.run`，闸门换成替身。`tool_turns` 决定前几轮返回工具调用（=继续下一轮），
        最后一轮才 `stop` —— 不设这个参数，模型一轮就收尾，"每轮问一次"根本无从计数。
        """
        from app.agent.runtime import AgentRuntime
        from app.gateway.provider import ChatResponse, ToolCall

        monkeypatch.setattr(ledger_mod, "get_line_ledger", lambda: stub)
        monkeypatch.setattr(ledger_mod, "default_cap", lambda: 10**12)  # 别让 env 干扰

        calls: list[dict] = []

        async def _fake_stream(**kwargs):
            calls.append(kwargs)
            if len(calls) <= tool_turns:
                # 一个不存在的工具名：结果会报错，但循环照往常下一轮 —— 这里要数的是轮次。
                yield ChatResponse(
                    content="",
                    finish_reason="tool_calls",
                    tool_calls=[
                        ToolCall(id=f"c{len(calls)}", name="__absent_tool__", arguments="{}")
                    ],
                )
            else:
                yield ChatResponse(content="still burning", finish_reason="stop")

        gw = _FakeGateway()
        gw.chat_stream = _fake_stream
        runtime = AgentRuntime(gateway=gw)

        async def _noop(*a: Any, **k: Any) -> None:
            return None

        monkeypatch.setattr(runtime, "_mark_run_status", _noop)
        events = [e async for e in runtime.run(task)]
        return events, calls

    async def test_trip_ends_the_run_before_any_llm_call(self, monkeypatch) -> None:
        """越界必须**拦在调用模型之前** —— 否则每一轮都还能再白烧一次补量。"""
        stub = _StubLedger(axis="line_tokens")
        task = self._task(subagent_line="rs_root", max_turns=3)
        events, calls = await self._drive_run(monkeypatch, stub=stub, task=task)
        assert calls == [], "闸门越界后不该再调模型"
        assert any(e.type == "error" and e.error == "budget_exceeded:line_tokens" for e in events)
        assert stub.calls == [("check", "rs_root", "t7", "sub_rs_child", 0)]

    async def test_per_run_budget_is_judged_first(self, monkeypatch) -> None:
        """同一轮里 per-run 预算与线账同时越界 ⇒ 报 `tokens`：那是**这一段**的账，
        归因更直接，不该被"线"的解释盖住。

        这里把 `TaskBudget.exceeded` 钉成"永远越界"来构造同时越界 —— 真跑要在第一轮前
        就攒够 token，而线账在第 0 轮就会拦下来，那种用例只会一直测不到顺序。
        """
        from app.agent import task_budget as task_budget_mod

        stub = _StubLedger(axis="line_tokens")
        monkeypatch.setattr(task_budget_mod.TaskBudget, "exceeded", lambda *a, **k: "tokens")
        task = self._task(subagent_line="rs_root", max_turns=2)
        events, calls = await self._drive_run(monkeypatch, stub=stub, task=task)
        assert stub.calls == [] and calls == []
        assert any(e.type == "error" and e.error == "budget_exceeded:tokens" for e in events)

    async def test_gate_asks_once_per_turn_not_per_token(self, monkeypatch) -> None:
        """判定挂在**轮次边界**：一次 run 里跑 N 轮就问 N 次，不多（每轮一次 LLM 调用）
        也不少（只在委派入口判等于给每段放行一整轮额度）。"""
        stub = _StubLedger()
        task = self._task(subagent_line="rs_root", max_turns=3)
        events, calls = await self._drive_run(monkeypatch, stub=stub, task=task, tool_turns=2)
        assert len(calls) == 3, "模型应当跑满 3 轮（没越界）"
        assert [c[0] for c in stub.calls] == ["check"] * 3
        assert any(e.type == "done" for e in events)


# ── runner 的接线（线根 / 预留句柄 / 收尾顺序）─────────────────────────


class TestRunnerWiring:
    async def _drive(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        store_root: str,
        budget_tokens: int = 200_000,
        ledger: _StubLedger | None = None,
    ) -> dict[str, Any]:
        import asyncio

        from app.agent import runtime as runtime_mod
        from app.agent.subagent_runner import SubAgentRunner
        from app.subagent.budget import TaskBudget

        stub = ledger or _StubLedger()
        monkeypatch.setattr(ledger_mod, "get_line_ledger", lambda: stub)
        seen: dict[str, Any] = {}

        class _Store:
            available = True

            async def start_run(self, **kwargs: Any) -> str:
                return store_root

            async def add_step(self, *args: Any, **kwargs: Any) -> None:
                return None

            async def flush_steps(self, *args: Any, **kwargs: Any) -> None:
                return None

            async def finish_run(self, *a: Any, **kw: Any) -> None:
                seen["finish_kwargs"] = kw

            async def mark_lifecycle(self, *args: Any, **kwargs: Any) -> None:
                return None

        async def _one_step_run(self: Any, task: Any):  # noqa: ANN001
            seen["subagent_line"] = getattr(task, "subagent_line", "")
            seen["task_id"] = task.id
            yield runtime_mod.AgentEvent(
                type="text", content="done", input_tokens=10, output_tokens=5
            )

        monkeypatch.setattr(runtime_mod.AgentRuntime, "run", _one_step_run)
        runner = SubAgentRunner(
            gateway=object(),
            store=_Store(),  # type: ignore[arg-type]
            parent_session_id="s-main",
            tenant_id="t1",
            user_id="u1",
            budget=TaskBudget(tokens=budget_tokens),
        )
        await asyncio.wait_for(
            runner.run("跑一件事", profile_ref="", mode="normal", max_turns=1), timeout=10
        )
        seen["ledger_calls"] = list(stub.calls)
        return seen

    async def test_line_root_reaches_the_runtime_and_is_reserved(self, monkeypatch) -> None:
        seen = await self._drive(monkeypatch, store_root="rs_root")
        assert seen["subagent_line"] == "rs_root"
        reserve = [c for c in seen["ledger_calls"] if c[0] == "reserve"]
        # member 用的是 task.id：runtime 那侧按同一个键把自己从预留求和里剔掉
        assert reserve == [("reserve", "rs_root", seen["task_id"], 200_000)]

    async def test_no_store_root_means_no_line(self, monkeypatch) -> None:
        """落库不可用 ⇒ 不假装有一条线（Redis 里不会多出无人认领的预留）。"""
        seen = await self._drive(monkeypatch, store_root="")
        assert seen["subagent_line"] == ""
        assert seen["ledger_calls"] == []

    async def test_zero_token_axis_reserves_nothing_but_still_carries_the_line(
        self, monkeypatch
    ) -> None:
        seen = await self._drive(monkeypatch, store_root="rs_root", budget_tokens=0)
        assert seen["subagent_line"] == "rs_root"
        assert [c for c in seen["ledger_calls"] if c[0] == "reserve"] == []

    async def test_settle_reports_the_actual_tokens_after_the_row_is_written(
        self, monkeypatch
    ) -> None:
        """顺序是口径的一部分：先落库、再累镜像；数字是该 run 的真实用量。"""
        order: list[str] = []

        class _Ledger(_StubLedger):
            async def settle(self, root: str, handle: str, actual_tokens: int) -> None:
                order.append(f"settle:{root}:{actual_tokens}")

        from app.subagent import store as store_mod

        stub = _Ledger()

        async def _start(self: Any, **kwargs: Any) -> str:  # noqa: ANN001
            return "rs_root"

        async def _finish(self: Any, run_id: str, **kwargs: Any) -> None:  # noqa: ANN001
            order.append("finish_run")

        async def _quiet(self: Any, *a: Any, **k: Any) -> None:  # noqa: ANN001
            return None

        monkeypatch.setattr(store_mod.SubagentRunStore, "start_run", _start)
        monkeypatch.setattr(store_mod.SubagentRunStore, "finish_run", _finish)
        monkeypatch.setattr(store_mod.SubagentRunStore, "add_step", _quiet)
        monkeypatch.setattr(store_mod.SubagentRunStore, "flush_steps", _quiet)
        monkeypatch.setattr(store_mod.SubagentRunStore, "mark_lifecycle", _quiet)
        monkeypatch.setattr(ledger_mod, "get_line_ledger", lambda: stub)

        import asyncio

        from app.agent import runtime as runtime_mod
        from app.agent.subagent_runner import SubAgentRunner
        from app.subagent.budget import TaskBudget

        async def _one_step_run(self: Any, task: Any):  # noqa: ANN001
            yield runtime_mod.AgentEvent(
                type="text", content="done", input_tokens=10, output_tokens=5
            )

        monkeypatch.setattr(runtime_mod.AgentRuntime, "run", _one_step_run)
        runner = SubAgentRunner(
            gateway=object(),
            store=store_mod.SubagentRunStore(object()),  # type: ignore[arg-type]
            parent_session_id="s-main",
            tenant_id="t1",
            budget=TaskBudget(tokens=1000),
        )
        await asyncio.wait_for(
            runner.run("跑一件事", profile_ref="", mode="normal", max_turns=1), timeout=10
        )
        assert order == ["finish_run", "settle:rs_root:15"]

    async def test_reserve_failure_does_not_break_the_run(self, monkeypatch) -> None:
        class _Boom(_StubLedger):
            async def reserve(self, *a: Any, **k: Any) -> str:
                raise RuntimeError("redis down")

        seen = await self._drive(monkeypatch, store_root="rs_root", ledger=_Boom())
        assert seen["subagent_line"] == "rs_root"  # 线根照样传下去了，只是没预留成


# ── 迁移 0022 的契约（口径写进 DDL，别在下一轮被顺手改掉）───────────────


class TestMigration0022Contract:
    def test_chain_head(self) -> None:
        src = _MIGRATION_0022.read_text(encoding="utf-8")
        assert 'revision: str = "0022_run_lineage_ledger"' in src
        assert '"0021_subagent_lineage_root"' in src

    def test_table_is_idempotent_and_stores_no_usage(self) -> None:
        """**本表不存 used / reserved**。累计一旦有第二份就一定会和明细对不上；预留则是
        会过期的协调态 —— 放表里需要一个回收任务，崩溃的 run 会把额度永久占住。

        判的是**列名**，不是子串：`updated_at` / `updated_by` 里也含 "used"，按子串禁会把
        一条正常的时间列也禁掉（那种断言要么写成一长串 replace 补丁，要么迟早被误改）。
        """
        ddl = [s for s in _migration_sql_statements() if "CREATE TABLE" in s]
        assert len(ddl) == 1
        assert "CREATE TABLE IF NOT EXISTS run_lineage_ledger" in ddl[0]
        body = ddl[0].split("run_lineage_ledger", 1)[1]
        body = body[body.index("(") + 1 : body.rindex("PRIMARY KEY")]
        cols = [part.strip().split()[0] for part in body.split(",") if part.strip()]
        assert {"root_run_id", "tenant_id", "cap_tokens", "paused"} <= set(cols), cols
        for forbidden in (
            "used",
            "token_used",
            "tokens",
            "settled",
            "reserved",
            "total",
            "cost_cents",
        ):
            assert forbidden not in cols, forbidden
        for required in (
            "cap_tokens bigint NOT NULL DEFAULT 0",
            "paused boolean NOT NULL DEFAULT false",
            "updated_by",
            "PRIMARY KEY (root_run_id)",
        ):
            assert required in ddl[0], required

    def test_paused_index_is_partial(self) -> None:
        """绝大多数线永远不会被暂停 ⇒ 只给 paused 行建索引。"""
        idx = [s for s in _migration_sql_statements() if "CREATE INDEX" in s]
        assert len(idx) == 1
        assert "IF NOT EXISTS ix_run_lineage_ledger_paused" in idx[0]
        assert "WHERE paused" in idx[0]

    def test_control_state_query_is_tenant_scoped(self) -> None:
        assert "WHERE root_run_id = $1 AND tenant_id = $2" in " ".join(LEDGER_SELECT_SQL.split())

    def test_no_plpgsql_block(self) -> None:
        assert "$$" not in _MIGRATION_0022.read_text(encoding="utf-8")

    def test_downgrade_is_the_reverse_order(self) -> None:
        src = _MIGRATION_0022.read_text(encoding="utf-8")
        down = src.split("def downgrade", 1)[1]
        assert "ix_run_lineage_ledger_paused" in down and "DROP TABLE IF EXISTS run_lineage_ledger" in down


# ── 真 Redis：Lua 的算术只有 Redis 能判 ─────────────────────────────────


@pytest.fixture
async def redis_client():
    import redis.asyncio as aioredis

    client = aioredis.from_url(_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        pytest.fail(f"需要可达的 Redis（{_REDIS_URL}）才能跑线账的集成用例：{exc}")
    try:
        yield client
    finally:
        await client.aclose()


def _rkey(name: str) -> str:
    from app.redis_keys import rkey

    return rkey(name)


def _root_id(tag: str) -> str:
    # `_usable_root` 只收 [A-Za-z0-9_.-]，且 ≤64：测试 id 必须自己先合规
    return f"rsit{uuid.uuid4().hex[:12]}{tag}"[:60]


@pytest.mark.integration
class TestRealRedisSemantics:
    """这些断言判的是脚本的**实际算术**：PG 侧一律用替身，保证只有 Redis 是真的。"""

    def _ledger(self, redis: Any, *, usage: tuple[int, int] = (0, 0)) -> LineLedger:
        return LineLedger(
            redis=redis, pool=_FakePool(), store=_FakeStore(usage=usage)
        )

    async def test_reserve_excludes_the_callers_own_reservation(self, redis_client) -> None:
        root = _root_id("ex")
        led = self._ledger(redis_client)
        assert await led.reserve(root, "t1", "sub_a", 100) == f"sub_a{_TAB}100"
        assert await led.reserve(root, "t1", "sub_b", 250) == f"sub_b{_TAB}250"
        state = await led.state(root, "t1", exclude_run_id="sub_a")
        assert state is not None
        # 350 里减去自己那 100 ⇒ 250：自己的花销按**实际 token** 计，预留不能再算一遍
        assert state.reserved == 250
        other = await led.state(root, "t1", exclude_run_id="sub_b")
        assert other is not None and other.reserved == 100

    async def test_expired_reservations_are_purged_and_the_key_gone(self, redis_client) -> None:
        root = _root_id("ttl")
        key = _rkey(f"subagent:line:{root}:rsv")
        await redis_client.zadd(key, {f"sub_dead{_TAB}9999": int(time.time()) - 1})
        led = self._ledger(redis_client)
        state = await led.state(root, "t1")
        assert state is not None and state.reserved == 0
        # 空了就把键删掉：留着"存在但为 0"的键会让排障时看不清这条线到底有没有在途
        assert await redis_client.exists(key) == 0

    async def test_live_reservation_survives_a_purge(self, redis_client) -> None:
        root = _root_id("live")
        led = self._ledger(redis_client)
        assert await led.reserve(root, "t1", "sub_a", 77)
        state = await led.state(root, "t1")
        assert state is not None and state.reserved == 77

    async def test_settle_releases_the_handle(self, redis_client) -> None:
        root = _root_id("settle")
        led = self._ledger(redis_client)
        a = await led.reserve(root, "t1", "sub_a", 60)
        b = await led.reserve(root, "t1", "sub_b", 40)
        await led.settle(root, a, 0)
        state = await led.state(root, "t1")
        assert state is not None and state.reserved == 40
        await led.settle(root, b, 0)
        assert await redis_client.exists(_rkey(f"subagent:line:{root}:rsv")) == 0

    async def test_used_mirror_backfills_once_then_serves_from_redis(self, redis_client) -> None:
        """快路径的全部意义：键还活着就不去问 PG（一次求和都不发）。"""
        root = _root_id("used")
        key = _rkey(f"subagent:line:{root}:used")
        await redis_client.delete(key)
        store = _FakeStore(usage=(500, 2))
        pool = _FakePool()
        led = LineLedger(redis=redis_client, pool=pool, store=store)
        state = await led.state(root, "t1")
        assert state is not None and state.settled == 500
        assert await redis_client.get(key) == "500"
        assert await redis_client.ttl(key) > 0

        store.usage = (9999, 9)  # 若再去问 PG，这里就会变
        state2 = await led.state(root, "t1")
        assert state2 is not None and state2.settled == 500
        assert len(store.calls) == 1

    async def test_backfill_does_not_clobber_a_newer_mirror(self, redis_client) -> None:
        """并发里另一个 run 刚把键建起来并 INCRBY 过 ⇒ 回填必须是 NX，覆盖会把那段实际用量
        抹掉（**少算**，对上限是不安全的一侧）。"""
        root = _root_id("nx")
        key = _rkey(f"subagent:line:{root}:used")
        await redis_client.set(key, "888", ex=60)
        led = LineLedger(redis=redis_client, pool=_FakePool(), store=_FakeStore(usage=(10, 1)))
        state = await led.state(root, "t1")
        assert state is not None and state.settled == 888
        assert await redis_client.get(key) == "888"
        await redis_client.delete(key)

    async def test_bump_does_not_resurrect_a_missing_key(self, redis_client) -> None:
        """镜像没了 ⇒ 什么都不加：那一段已经在 PG 的行里，下一次读会从求和回填。
        在这里把键"救活"会让闸门从此只看这份缺了历史的数。"""
        root = _root_id("bump")
        key = _rkey(f"subagent:line:{root}:used")
        await redis_client.delete(key)
        await self._ledger(redis_client).settle(root, "", 123)
        assert await redis_client.exists(key) == 0

    async def test_bump_adds_onto_a_live_mirror(self, redis_client) -> None:
        root = _root_id("bump2")
        key = _rkey(f"subagent:line:{root}:used")
        await redis_client.set(key, "100", ex=60)
        await self._ledger(redis_client).settle(root, "", 123)
        assert await redis_client.get(key) == "223"

    async def test_run_id_with_a_tab_cannot_poison_the_ledger(self, redis_client) -> None:
        root = f"bad{_TAB}root"
        led = self._ledger(redis_client, usage=(1, 1))
        assert await led.state(root, "t1") is None
        assert await led.reserve(root, "t1", f"x{_TAB}9", 5) == ""
        await led.settle(root, f"x{_TAB}9", 5)  # 不抛
        assert await redis_client.exists(_rkey(f"subagent:line:{root}:rsv")) == 0


# ── 真 PG：控制态覆盖与跨租户过滤 ──────────────────────────────────────


@pytest.mark.integration
class TestRealPostgresLedger:
    """控制态读的是真库：cap 覆盖、租户过滤、paused 优先，桩一律判不了。"""

    _created: list[str] = []

    @pytest.fixture(autouse=True)
    async def _cleanup(self):
        from app.db import get_pool

        self._created = []
        yield
        pool = get_pool()
        for root in self._created:
            await pool.execute("DELETE FROM run_lineage_ledger WHERE root_run_id = $1", root)
            await pool.execute("DELETE FROM subagent_runs WHERE root_run_id = $1", root)

    def _root(self, tag: str = "") -> str:
        root = f"lgd{uuid.uuid4().hex[:12]}{tag}"[:60]
        self._created.append(root)
        return root

    async def _set_ledger(
        self, root: str, *, tenant: str = "t1", cap: int = 0,
        paused: bool = False, reason: str | None = None,
    ) -> None:
        from app.db import get_pool

        await get_pool().execute(
            """
            INSERT INTO run_lineage_ledger
                (root_run_id, tenant_id, cap_tokens, paused, pause_reason)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (root_run_id) DO UPDATE
              SET cap_tokens = EXCLUDED.cap_tokens,
                  paused = EXCLUDED.paused,
                  pause_reason = EXCLUDED.pause_reason,
                  updated_at = now()
            """,
            root, tenant, cap, paused, reason,
        )

    async def _seed_run(self, root: str, *, tenant: str = "t1", tokens: int = 100) -> None:
        from app.db import get_pool

        await get_pool().execute(
            """
            INSERT INTO subagent_runs
                (id, root_session_id, tenant_id, task, status,
                 input_tokens, output_tokens, root_run_id)
            VALUES ($1, 's-lineage', $2, 'seed', 'completed', $3, 0, $4)
            """,
            f"lgdRun{uuid.uuid4().hex[:10]}", tenant, tokens, root,
        )

    async def _ledger(self, redis: Any = None) -> LineLedger:
        from app.db import get_pool
        from app.subagent.store import SubagentRunStore

        led = LineLedger(pool=get_pool(), store=SubagentRunStore(get_pool()), redis=None)
        if redis is None:
            led._redis_conn = _no_redis  # type: ignore[method-assign]
        else:
            led._redis = redis
        return led

    async def test_missing_row_follows_default(self, monkeypatch) -> None:
        monkeypatch.setenv("SUBAGENT_LINE_MAX_TOKENS", "1000000")
        root = self._root("default")
        await self._seed_run(root, tokens=50)
        state = await (await self._ledger()).state(root, "t1")
        assert state is not None
        assert (state.settled, state.cap, state.paused) == (50, 1000000, False)

    async def test_row_override_is_used_for_the_aggregate(self, monkeypatch) -> None:
        monkeypatch.setenv("SUBAGENT_LINE_MAX_TOKENS", "1000000")
        root = self._root("override")
        await self._set_ledger(root, cap=120)
        await self._seed_run(root, tokens=100)
        led = await self._ledger()
        assert await led.check(root, "t1", own_tokens=19) == ""
        assert await led.check(root, "t1", own_tokens=20) == "line_tokens"

    async def test_default_cap_applies_when_the_row_is_absent(self, monkeypatch) -> None:
        """没有行 ⇒ 用 env 默认（不是"不限"）—— 缺陷正文要拦的就是"没人给它设过上限"。"""
        monkeypatch.setenv("SUBAGENT_LINE_MAX_TOKENS", "150")
        root = self._root("defcap")
        await self._seed_run(root, tokens=140)
        led = await self._ledger()
        assert await led.check(root, "t1", own_tokens=9) == "line_tokens"
        assert await led.check(root, "t1", own_tokens=8) == ""

    async def test_default_of_zero_disables_the_axis(self, monkeypatch) -> None:
        """默认也可以是"整条线闸门关掉"：``SUBAGENT_LINE_MAX_TOKENS=0``。"""
        monkeypatch.setenv("SUBAGENT_LINE_MAX_TOKENS", "0")
        root = self._root("nocap")
        await self._seed_run(root, tokens=10**6)
        led = await self._ledger()
        assert await led.check(root, "t1", own_tokens=10**6) == ""

    async def test_a_row_for_another_tenant_is_not_readable(self, monkeypatch) -> None:
        """闸门拿到的只是一个 run id：不带租户过滤就能读到别租户抬过的上限。"""
        monkeypatch.setenv("SUBAGENT_LINE_MAX_TOKENS", "1000")
        root = self._root("cross")
        await self._set_ledger(root, tenant="t2", cap=CAP_UNLIMITED)  # t2 明确不限
        await self._seed_run(root, tenant="t1", tokens=900)
        await self._seed_run(root, tenant="t2", tokens=5000)
        state = await (await self._ledger()).state(root, "t1")
        assert state is not None
        assert state.cap == 1000, "用的是 t1 的默认，而不是 t2 的'不限'"
        assert state.settled == 900, "求和同样按租户收：t2 那 5000 不该算进 t1 的线"

    async def test_paused_beats_an_increased_cap(self, monkeypatch) -> None:
        """人工暂停优先：cap 已经提上去了但暂停还没清 ⇒ 仍然停。"""
        monkeypatch.setenv("SUBAGENT_LINE_MAX_TOKENS", "1000")
        root = self._root("paused")
        await self._set_ledger(root, cap=10**9, paused=True, reason="budget_exhausted")
        await self._seed_run(root, tokens=10)
        led = await self._ledger()
        assert await led.check(root, "t1", own_tokens=0) == "line_paused"

    async def test_clearing_pause_lets_the_line_run_again(self, monkeypatch) -> None:
        monkeypatch.setenv("SUBAGENT_LINE_MAX_TOKENS", "1000")
        root = self._root("resumed")
        await self._set_ledger(root, cap=500, paused=True)
        await self._seed_run(root, tokens=100)
        led = await self._ledger()
        assert await led.check(root, "t1", own_tokens=0) == "line_paused"
        await self._set_ledger(root, cap=500, paused=False)
        assert await led.check(root, "t1", own_tokens=0) == ""

    async def test_settled_follows_the_line_not_the_tree(self, monkeypatch) -> None:
        """求和按 ``root_run_id`` ⇒ 同线的多段（续跑 / 嵌套）并进同一个数 —— 这正是缺陷的落点。"""
        monkeypatch.setenv("SUBAGENT_LINE_MAX_TOKENS", "1000")
        root = self._root("line")
        await self._seed_run(root, tokens=300)
        await self._seed_run(root, tokens=300)
        state = await (await self._ledger()).state(root, "t1")
        assert state is not None and state.settled == 600

    async def test_gate_with_real_sum_and_real_mirror(self, monkeypatch, redis_client) -> None:
        """PG 求和 + Redis 预留两件事一起跑：闸门判定用的是同一份账。"""
        monkeypatch.setenv("SUBAGENT_LINE_MAX_TOKENS", "1000")
        root = self._root("e2e")
        await self._seed_run(root, tokens=600)
        led = await self._ledger(redis=redis_client)
        assert await led.check(root, "t1", run_id="sub_me", own_tokens=400) == "line_tokens"
        handle = await led.reserve(root, "t1", "sub_other", 200)
        assert handle
        state = await led.state(root, "t1", exclude_run_id="sub_me")
        assert state is not None and (state.settled, state.reserved) == (600, 200)
        await led.settle(root, handle, 0)
        state2 = await led.state(root, "t1", exclude_run_id="sub_me")
        assert state2 is not None and state2.reserved == 0
