"""M15b：任务线账（run 级预算的第二道闸门）。

**这里能证明什么、不能证明什么**

Lua 脚本的算术**不在这里**判定：桩里的 Python 镜像按自己写的规则算，那不是脚本的规则，
"桩说脚本会这么算"没有证据价值（与 `tests/test_subagent_lineage.py` 同一取舍）。分层因此是：

* 单测：Python 侧编排 —— 发**哪个脚本、哪些参数**、取不到时退回什么、闸门在 runtime 的
  哪一步介入、runner 有没有把线根与预留句柄传下去；
* ``@pytest.mark.live``：**真 Redis** 判 `_RSVD_LUA` / `_USED_*_LUA` / `_RSVD_DROP_HOLDER_LUA` 的
  实际算术（剔除自己、按 score 清过期、镜像不复活、回填只用 NX、按持有者摘预留）；
* ``@pytest.mark.integration``：**真 PG** 判控制态覆盖与跨租户过滤。

门槛（`tests/conftest.py` + `pyproject.toml` 的 markers）：`integration` 用例只有在 `POSTGRES_DSN`
已设置时才跑，本地没设就**整类 skip**（不是"红"）—— 真跑由 CI 的 real-stack job 注入 DSN 并先
`alembic upgrade head`。`live` 刻意与它**分开**：这些用例只要 Redis，若也打 `integration`，就会被
那个"integration ⇒ 必须有 PG"的 autouse fixture 整类拦下，于是"本机有 Redis"永远验不到脚本的算术 ——
而上面说了，桩判不了 Lua。CI 的选择表达式 `-m "integration or live"`，两类都跑。
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
    projected_tokens,
    projection_mode,
)

_REDIS_URL = (
    os.getenv("CHIRON_TEST_REDIS_URL") or os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
)

_BASELINE = (
    Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0001_authoritative_baseline.py"
)


def _baseline_ddl() -> list[str]:
    """从基线 `_DDL` 元组取出全部 DDL 字面量（空白归一）。

    `0022_run_lineage_ledger` 已收敛进权威基线 —— 契约仍然有效，只是数据源从
    「独立迁移文件」换成「基线 `_DDL` 元组」，避免测试指向一个已不存在的文件。
    """
    tree = ast.parse(_BASELINE.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_DDL"
            and isinstance(node.value, ast.Tuple)
        ):
            return [
                " ".join(str(elt.value).split())
                for elt in node.value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]
    raise AssertionError("baseline `_DDL` tuple not found")


def _lineage_ddl() -> list[str]:
    """基线里所有触及 `run_lineage_ledger` 的 DDL（建表 / 约束 / 索引）。"""
    return [s for s in _baseline_ddl() if "run_lineage_ledger" in s]


class _FakeStore:
    """只实现 `lineage_usage` —— 线账用它取"已结算"，不自己再写一遍求和 SQL。"""

    def __init__(self, usage: tuple[int, int] | None = (0, 0)) -> None:
        self.usage = usage
        self.calls: list[tuple[str, str]] = []

    async def lineage_usage(self, root_run_id: str, tenant_id: int) -> tuple[int, int] | None:
        self.calls.append((root_run_id, tenant_id))
        return self.usage


class _FakePool:
    """`fetchrow` = 读控制态，`execute` = 写控制态。读与写的失败**分开**注入：
    读失败要 fail-open，写失败要如实返回 False —— 同一条 `error` 开关测不到两侧。
    """

    def __init__(
        self,
        row: dict[str, Any] | None = None,
        *,
        error: bool = False,
        write_error: bool = False,
        update_count: int = 1,
    ) -> None:
        self.row = row
        self.error = error
        self.write_error = write_error
        self.update_count = update_count
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((" ".join(sql.split()), args))
        if self.error:
            raise RuntimeError('relation "run_lineage_ledger" does not exist')
        return self.row

    async def execute(self, sql: str, *args: Any) -> str:
        norm = " ".join(sql.split())
        self.calls.append((norm, args))
        if self.write_error:
            raise RuntimeError('relation "run_lineage_ledger" does not exist')
        return "INSERT 0 1" if norm.startswith("INSERT") else f"UPDATE {self.update_count}"

    def updates(self) -> list[tuple[str, tuple]]:
        return [c for c in self.calls if c[0].startswith("UPDATE run_lineage_ledger")]

    def ensures(self) -> list[tuple[str, tuple]]:
        return [c for c in self.calls if c[0].startswith("INSERT INTO run_lineage_ledger")]


class _FakeRedis:
    """记录 `get` / `eval` / `zrange` 的调用；**不解释 Lua**。

    `eval` 的返回值可按脚本里的标志子串挑（`results`），挑不到用 `result` 兜底：一次 `state`
    会发两三个不同脚本（镜像回填 / 预留求和 / 终态累加），只给一个常量就没法把"结算 600 +
    预留 300"这种两笔账分开喂进去。`got` 是镜像键的当前值（``None`` = 键不存在）。
    `members` 是预留 zset 的成员（M17 的清扫要 `ZRANGE` 它）—— 同样**不参与算术**。
    `scan_error` / `drop_error` 分开注入：清扫的"读成员"与"按 holder 删"是两步，失败之后的
    退化不一样（前者只是这次没核对，后者是删不掉、判定照旧按 TTL），用一条 `error` 测不出来。
    """

    def __init__(
        self,
        *,
        result: Any = "0",
        results: dict[str, Any] | None = None,
        got: Any = None,
        members: list[str] | None = None,
        error: bool = False,
        scan_error: bool = False,
        drop_error: bool = False,
    ) -> None:
        self.result = result
        self.results = results or {}
        self.got = got
        self.members = members or []
        self.error = error
        self.scan_error = scan_error
        self.drop_error = drop_error
        self.calls: list[tuple[str, tuple]] = []
        self.gets: list[str] = []
        self.zranges: list[str] = []

    async def get(self, key: str) -> Any:
        self.gets.append(key)
        if self.error:
            raise RuntimeError("redis down")
        return self.got

    async def zrange(self, key: str, start: int, stop: int) -> list[str]:
        self.zranges.append(key)
        if self.error or self.scan_error:
            raise RuntimeError("redis down")
        return list(self.members)

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        self.calls.append((" ".join(script.split()), args))
        if self.error or (self.drop_error and "ZCARD" in script):
            raise RuntimeError("redis down")
        for marker, value in self.results.items():
            if marker in script:
                return value
        return self.result

    def args_for(self, marker: str) -> list[tuple]:
        return [a for head, a in self.calls if marker in head]

    def calls_of(self, marker: str) -> list[str]:
        """按**发生顺序**取匹配某个脚本标志的调用（用来断言先后，而不是各自计数）。"""
        return [head for head, _ in self.calls if marker in head]


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


def _set_holder(monkeypatch: pytest.MonkeyPatch, holder: str) -> None:
    """把"本实例 id"钉住：预留句柄的第三段与 drain 的匹配键都由它决定。"""
    from app.subagent import affinity

    monkeypatch.setattr(affinity, "cached_instance_id", lambda: holder)


def _participating(
    monkeypatch: pytest.MonkeyPatch, redis: Any, *, alive: set[str]
) -> LineLedger:
    """把线账放进**存活协议**里：``participates()=True``，探测只认 ``alive`` 那批实例。

    只钉这两个判据，清扫的编排（什么时候扫、扫到谁、删完要不要重读）才由被测代码决定。
    探测本身（自我豁免、缓存、未参与时返回 ``{}``）在 ``tests/test_instance_liveness.py``。
    """
    from app import instance_liveness

    monkeypatch.setattr(instance_liveness, "participates", lambda: True)

    async def _probe(holders: Any) -> dict[str, bool]:
        return {h: (h in alive) for h in holders}

    monkeypatch.setattr(instance_liveness, "holders_alive", _probe)
    return _ledger(redis=redis)


class _StubLedger:
    """按调用顺序可编程返回轴名的替身（`axes[i]` 对应第 i 次 `check`）。"""

    def __init__(
        self,
        *,
        axis: str = "",
        axes: list[str] | None = None,
        error: bool = False,
        pause_ok: bool = True,
        state_obj: LineState | None = None,
    ) -> None:
        self.axis = axis
        self.axes = axes
        self.error = error
        self.pause_ok = pause_ok
        self.state_obj = state_obj
        self.calls: list[Any] = []

    async def check(self, root: str, tenant_id: int, *, run_id: str = "", own_tokens: int = 0) -> str:
        self.calls.append(("check", root, tenant_id, run_id, own_tokens))
        if self.error:
            raise RuntimeError("boom")
        if self.axes is not None:
            idx = sum(1 for c in self.calls if c[0] == "check") - 1
            return self.axes[idx] if idx < len(self.axes) else ""
        return self.axis

    async def reserve(self, root: str, tenant_id: int, member: str, tokens: int) -> str:
        self.calls.append(("reserve", root, member, tokens))
        return f"{member}{_TAB}{tokens}"

    async def project(
        self,
        root: str,
        tenant_id: int,
        *,
        member: str,
        handle: str,
        spent: int,
        elapsed_seconds: float,
        per_run_cap: int = 0,
    ) -> str:
        self.calls.append(("project", root, tenant_id, member, handle, spent, per_run_cap))
        # 替身必须**换**句柄：tokens 编在 member 名字里，重述就是换一条 member（runner 要把
        # 返回值接住传给 settle，否则收尾摘不掉 —— 那正是本套件要钉住的接线）
        return f"{member}{_TAB}{spent + 100}"

    async def settle(self, root: str, handle: str, actual_tokens: int) -> None:
        self.calls.append(("settle", root, handle, actual_tokens))

    # ── M15c 写侧 ──

    async def pause_for_budget(self, root: str, tenant_id: int) -> bool:
        self.calls.append(("pause", root, tenant_id))
        return self.pause_ok

    async def raise_cap_and_resume(
        self, root: str, tenant_id: int, *, cap_tokens: int, updated_by: str
    ) -> bool:
        self.calls.append(("raise", root, tenant_id, cap_tokens, updated_by))
        return True

    async def state(
        self,
        root: str,
        tenant_id: int,
        *,
        exclude_run_id: str = "",
        scope: str = "delegation",
    ) -> LineState | None:
        self.calls.append(("state", root, tenant_id))
        return self.state_obj


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

    async def test_reserve_sends_handle_and_expiry(self, monkeypatch) -> None:
        redis = _FakeRedis(result="200000")
        _set_holder(monkeypatch, "engine-A")
        led = _ledger(redis=redis)
        handle = await led.reserve("rs_root", "t1", "sub_rs_1", 200_000)
        # 三段：member / tokens / **持有者实例 id**（M17）—— 清扫与 drain 都按第三段收账
        assert handle == f"sub_rs_1{_TAB}200000{_TAB}engine-A"
        args = redis.args_for("ZREMRANGEBYSCORE")
        assert len(args) == 1
        key, now, add, expiry, delete, skip, ttl = args[0]
        assert key.endswith("subagent:line:rs_root:rsv")
        assert add == handle and delete == "" and skip == "sub_rs_1"
        assert int(expiry) > int(now) and ttl == str(ledger_mod.RESERVE_TTL_SECONDS)

    async def test_a_member_with_a_tab_reserves_nothing(self, monkeypatch) -> None:
        """member 与 root 走同一份字符集校验：带 TAB 的 member 会把拼出来的记录拆成两段
        （``who`` 变成前缀、tokens 解析成 0），这条预留就此失明 —— 上限静默失效的一侧。"""
        redis = _FakeRedis()
        _set_holder(monkeypatch, "engine-A")
        led = _ledger(redis=redis)
        assert await led.reserve("rs_root", "t1", f"sub{_TAB}9", 1000) == ""
        assert redis.calls == []

    async def test_a_poisonous_instance_id_is_not_stamped(self, monkeypatch) -> None:
        """持有者来自部署配置（`INSTANCE_ID` / `POD_NAME`），**不保证**符合 id 字符集。

        `engine\tA` 会把 member 的字段协议拆坏：``_holder_of`` 读出 ``engine`` ⇒ 问的是**别人**
        的存活键、删的是按 ``engine`` 匹配的那条 —— 本实例在跑的预留会被自己扫掉（少算）。
        不署名（退化成两段 member，与滚动升级期间老实例写的同形）才是安全的方向。
        """
        redis = _FakeRedis(result="0")
        _set_holder(monkeypatch, f"engine{_TAB}A")
        led = _ledger(redis=redis)
        handle = await led.reserve("rs_root", "t1", "sub_a", 100)
        assert handle == f"sub_a{_TAB}100"
        assert ledger_mod._holder_of(handle) == "", "没署名的预留不该被任何一次核对认领"

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


# ── M18：在途预留的覆盖式重述（投影）───────────────────────────────────


class TestProjectionMath:
    """`projected_tokens` 是纯函数：两道夹紧的方向，以及"没样本就不编数"都在这里判。"""

    def test_no_sample_projects_nothing(self) -> None:
        """``spent=0`` ⇒ 返回 0 = **不写**：宁可沿用派发时那份确定上界，也不编一个数。"""
        assert projected_tokens(spent=0, elapsed_seconds=100, per_run_cap=200_000) == 0

    def test_a_negative_reading_is_not_a_sample(self) -> None:
        assert projected_tokens(spent=-5_000, elapsed_seconds=10, per_run_cap=200_000) == 0

    def test_ceiling_is_the_per_run_cap(self) -> None:
        """一个 run 不可能花超自己的上限 ⇒ 超出那部分不是"这条线多出来的风险"。"""
        assert projected_tokens(spent=180_000, elapsed_seconds=1, per_run_cap=200_000) == 200_000

    def test_a_very_slow_run_projects_to_just_what_was_spent(self) -> None:
        """跑了很久、速率很慢 ⇒ 投影就贴着"已花"：这条线看见的是真实速度，不是猜的上限。"""
        assert projected_tokens(spent=9_000, elapsed_seconds=10**9, per_run_cap=200_000) == 9_000

    def test_floor_is_what_was_already_spent(self) -> None:
        """夹紧的顺序不能反：先夹上限，再保证不低于**已花**。

        碰得到的一档是"上限比已花还小"（cap 被调低、或接管段读回来的用量已超过本段上限）——
        少了这一道，重述会把已经花掉的钱从同线其它 run 的视野里抹掉（少算，不安全的一侧）。
        """
        assert projected_tokens(spent=300_000, elapsed_seconds=60, per_run_cap=200_000) == 300_000

    def test_a_one_second_sample_does_not_extrapolate_sixty_fold(self) -> None:
        """速率分母有下限：开局 1 秒的 6k token 若按 1s 算，会被投成 60 倍。"""
        short = projected_tokens(spent=6_000, elapsed_seconds=1, per_run_cap=0)
        floored = projected_tokens(
            spent=6_000, elapsed_seconds=ledger_mod.PROJECTION_MIN_SAMPLE_SECONDS, per_run_cap=0
        )
        assert short == floored == 30_000

    def test_unlimited_per_run_axis_has_no_ceiling(self) -> None:
        """per-run token 轴不限 ⇒ 只剩下限，投出去的是一个纯估计（这仍比"完全看不见"好）。"""
        assert projected_tokens(spent=400_000, elapsed_seconds=60, per_run_cap=0) == 1_200_000

    def test_a_backwards_clock_does_not_inflate_the_rate(self) -> None:
        """``elapsed`` 为负（改过时钟 / 单调钟混用）不能把速率算成负数把预留抹掉。"""
        assert projected_tokens(spent=3_000, elapsed_seconds=-500, per_run_cap=0) == 15_000


class TestProjectionSwitch:
    def test_default_is_shadow(self, monkeypatch) -> None:
        monkeypatch.delenv("SUBAGENT_LINE_PROJECTION", raising=False)
        assert projection_mode() == "shadow"

    @pytest.mark.parametrize("raw", ["ENFORCE", " enforce "])
    def test_recognised_values_are_normalised(self, monkeypatch, raw: str) -> None:
        monkeypatch.setenv("SUBAGENT_LINE_PROJECTION", raw)
        assert projection_mode() == "enforce"

    @pytest.mark.parametrize("raw", ["", "always", "1"])
    def test_unknown_values_fall_back_to_the_default(self, monkeypatch, raw: str) -> None:
        """配置写错不该改变判定（与 `_env_int` 同一口径）。"""
        monkeypatch.setenv("SUBAGENT_LINE_PROJECTION", raw)
        assert projection_mode() == "shadow"


def _projection_outcome(outcome: str) -> float:
    """按标签读 M18 计数器的当前值（取**增量**：进程起点不由本文件决定）。"""
    from app.observability.metrics import SUBAGENT_LINE_PROJECTION

    return SUBAGENT_LINE_PROJECTION.labels(outcome=outcome)._value.get()


def _projection_observed_sum() -> float:
    """投影直方图的累计观测和 —— "有没有真的算出一个值"只有这里看得见。"""
    from app.observability.metrics import SUBAGENT_LINE_PROJECTION_TOKENS

    return SUBAGENT_LINE_PROJECTION_TOKENS._sum.get()


class TestReserveRestate:
    """重述的**编排**：什么时候写、发哪条脚本、失败退回什么、句柄怎么换。

    脚本的算术（同 ``who`` 的旧条目摘干净没有、score 推没推）不在这里判 —— 桩不解释 Lua，
    那是 ``TestRealRedisSemantics`` 的职责（与本文件开头同一分层取舍）。
    """

    #: `_RSVD_RESTATE_LUA` 独有的标志子串（`_RSVD_LUA` 里没有 `who` 这个变量名）
    MARKER = "local who = ARGV[2]"

    async def test_shadow_computes_but_writes_nothing(self, monkeypatch) -> None:
        monkeypatch.delenv("SUBAGENT_LINE_PROJECTION", raising=False)
        redis = _FakeRedis()
        led = _ledger(redis=redis)
        sum_before = _projection_observed_sum()
        seed = f"sub_a{_TAB}200000{_TAB}engine-A"
        out = await led.project(
            "rs_root", "t1", member="sub_a", handle=seed,
            spent=5_000, elapsed_seconds=20, per_run_cap=200_000,
        )
        assert out == seed, "shadow 不改判定也不改账：句柄原样还回"
        assert redis.calls == []
        assert _projection_observed_sum() == sum_before + 25_000, (
            "shadow 唯一的可见效果就是打点：没有它，运维无法知道投影值长什么样，"
            "也就没有切 enforce 的依据"
        )

    async def test_off_does_not_even_compute(self, monkeypatch) -> None:
        """`off` 与 `shadow` 的分工只在**打点**上可辨：一个连算都不算，一个算而不写。

        运维是靠 `subagent_line_projection_total{outcome=...}` 判断开关落在哪一档的，所以
        "off 也顺手算一份、往直方图里 observe 一笔"必须是红的 —— 那会在明确关掉了这个特性的
        部署里长出数据，看起来正好像"shadow 在跑"。
        """
        monkeypatch.setenv("SUBAGENT_LINE_PROJECTION", "off")
        redis = _FakeRedis()
        led = _ledger(redis=redis)
        off_before = _projection_outcome("off")
        shadow_before = _projection_outcome("shadow")
        sum_before = _projection_observed_sum()
        assert (
            await led.project(
                "rs_root", "t1", member="sub_a", handle="h", spent=5_000, elapsed_seconds=20
            )
            == "h"
        )
        assert redis.calls == []
        assert (_projection_outcome("off"), _projection_outcome("shadow")) == (
            off_before + 1,
            shadow_before,
        )
        assert _projection_observed_sum() == sum_before, "没算就不该有投影值"

    async def test_no_sample_writes_nothing(self, monkeypatch) -> None:
        monkeypatch.setenv("SUBAGENT_LINE_PROJECTION", "enforce")
        redis = _FakeRedis()
        led = _ledger(redis=redis)
        seed = f"sub_a{_TAB}200000"
        assert (
            await led.project(
                "rs_root", "t1", member="sub_a", handle=seed,
                spent=0, elapsed_seconds=5, per_run_cap=200_000,
            )
            == seed
        )
        assert redis.calls == []

    async def test_enforce_sends_the_restate_script_and_returns_a_new_handle(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("SUBAGENT_LINE_PROJECTION", "enforce")
        _set_holder(monkeypatch, "engine-A")
        redis = _FakeRedis(result="200000")  # 上一条 = 派发时那份 seed
        led = _ledger(redis=redis)
        seed = f"sub_a{_TAB}200000{_TAB}engine-A"
        out = await led.project(
            "rs_root", "t1", member="sub_a", handle=seed,
            spent=6_000, elapsed_seconds=30, per_run_cap=200_000,
        )
        assert out == f"sub_a{_TAB}30000{_TAB}engine-A"
        args = redis.args_for(self.MARKER)
        assert len(args) == 1
        key, now, who, member, expiry, ttl = args[0]
        assert key.endswith("subagent:line:rs_root:rsv")
        assert who == "sub_a", "按第一段匹配旧条目，不是拿整条 member 去 ZREM"
        assert member == out and ttl == str(ledger_mod.RESERVE_TTL_SECONDS)
        assert int(expiry) > int(now)

    async def test_a_run_that_was_never_seeded_still_gets_a_reservation(
        self, monkeypatch
    ) -> None:
        """per-run token 轴不限 ⇒ 派发**不写** seed，而"这一 run 在途时完全看不见"正是
        §5.2 ② 的另一半：有样本就照样补一条纯估计的预留上去。"""
        monkeypatch.setenv("SUBAGENT_LINE_PROJECTION", "enforce")
        _set_holder(monkeypatch, "engine-A")
        redis = _FakeRedis(result="0")
        led = _ledger(redis=redis)
        out = await led.project(
            "rs_root", "t1", member="sub_a", handle="",
            spent=4_000, elapsed_seconds=60, per_run_cap=0,
        )
        assert out == f"sub_a{_TAB}12000{_TAB}engine-A"

    async def test_write_failure_keeps_the_old_handle(self, monkeypatch) -> None:
        """Redis 抖了不改账：原样还回旧句柄（那条预留仍在求和里，方向是**多算**），也不抛。"""
        monkeypatch.setenv("SUBAGENT_LINE_PROJECTION", "enforce")
        led = _ledger(redis=_FakeRedis(error=True))
        seed = f"sub_a{_TAB}200000"
        assert (
            await led.project(
                "rs_root", "t1", member="sub_a", handle=seed,
                spent=6_000, elapsed_seconds=30, per_run_cap=200_000,
            )
            == seed
        )

    @pytest.mark.parametrize(
        "root,member,tenant",
        [("bad\troot", "sub_a", "t1"), ("rs_root", "sub\tA", "t1"), ("rs_root", "sub_a", "")],
    )
    async def test_unusable_keys_never_reach_redis(
        self, monkeypatch, root: str, member: str, tenant: str
    ) -> None:
        monkeypatch.setenv("SUBAGENT_LINE_PROJECTION", "enforce")
        redis = _FakeRedis()
        led = _ledger(redis=redis)
        assert (
            await led.project(
                root, tenant, member=member, handle="h", spent=6_000, elapsed_seconds=30
            )
            == "h"
        )
        assert redis.calls == []

    async def test_the_shutdown_registry_follows_the_new_handle(self, monkeypatch) -> None:
        """`_held` 里只能留**当前在 Redis 上那一版**：旧句柄留在册上，`settle` 就永久摘不掉它，
        登记簿只涨不落（`MAX_TRACKED_HANDLES` 那道上界迟早把新 run 的登记挤掉，drain 失明）。"""
        monkeypatch.setenv("SUBAGENT_LINE_PROJECTION", "enforce")
        _set_holder(monkeypatch, "engine-A")
        led = _ledger(redis=_FakeRedis(result="200000"))
        seed = await led.reserve("rs_root", "t1", "sub_a", 200_000)
        out = await led.project(
            "rs_root", "t1", member="sub_a", handle=seed,
            spent=6_000, elapsed_seconds=30, per_run_cap=200_000,
        )
        assert list(led._held) == [out]


# ── M17：死实例预留的清扫 ───────────────────────────────────────────────


class TestDeadHolderSweep:
    """清扫的**编排**：什么时候扫、拿谁当依据、删完要不要重读。

    三段 member 的算术（脚本怎么解释字段）不在这里判 —— 桩不解释 Lua，那是
    ``TestRealRedisSemantics`` 的职责。判据选择（自我豁免、正向缓存、未参与时回空）在
    ``tests/test_instance_liveness.py``。
    """

    async def test_no_participation_means_no_sweep_at_all(self) -> None:
        """本进程没在写存活键 ⇒ "读不到别人的键"不构成证据，一条 ``ZRANGE`` 都不该发。

        这条同时是"未参与协议时零额外 Redis 调用"的回归护栏：单实例部署（绝大多数）不该为
        这套机制多付一次命令，也不该有任何删除动作发生。
        """
        redis = _FakeRedis(result="300", members=[f"sub_a{_TAB}100{_TAB}gone"])
        led = _ledger(redis=redis)
        state = await led.state("rs_root", "t1")
        assert state is not None and state.reserved == 300
        assert redis.zranges == [] and redis.args_for("ZCARD") == []

    async def test_zero_sum_is_not_swept(self, monkeypatch) -> None:
        """线上一条正数预留都没有 ⇒ 没有可收回的东西，不必再发两条命令。"""
        redis = _FakeRedis(result="0", members=[f"sub_a{_TAB}100{_TAB}gone"])
        led = _participating(monkeypatch, redis, alive=set())
        assert await led.state("rs_root", "t1") is not None
        assert redis.zranges == []

    async def test_dead_holder_is_dropped_and_the_sum_reread(self, monkeypatch) -> None:
        redis = _FakeRedis(
            result="300",
            results={"ZCARD": "1"},
            members=[f"sub_a{_TAB}100{_TAB}gone", f"sub_b{_TAB}200{_TAB}mine"],
        )
        led = _participating(monkeypatch, redis, alive={"mine"})
        state = await led.state("rs_root", "t1")
        assert state is not None
        drops = redis.args_for("ZCARD")
        assert len(drops) == 1
        key, holder = drops[0]
        assert key.endswith("subagent:line:rs_root:rsv")
        assert holder == "gone", "只删死掉的那个持有者，一个 holder 一条脚本（不拼 IN 列表）"
        # 删完必须重读：不重读，"收回幽灵预留"对本次判定毫无影响 —— 而那是它存在的全部理由
        assert len(redis.args_for("ZREMRANGEBYSCORE")) == 2

    async def test_alive_holders_cost_one_scan_per_cooldown(self, monkeypatch) -> None:
        """全都在世 ⇒ 扫一次、什么都不删；冷却期内第二次问连扫都不扫。"""
        redis = _FakeRedis(result="300", members=[f"sub_a{_TAB}100{_TAB}mine"])
        led = _participating(monkeypatch, redis, alive={"mine"})
        assert await led.state("rs_root", "t1") is not None
        assert await led.state("rs_root", "t1") is not None
        assert len(redis.zranges) == 1, "按线节流：同一条线一个冷却周期只核对一次"
        assert redis.args_for("ZCARD") == []

    async def test_the_cooldown_is_per_line(self, monkeypatch) -> None:
        """节流按**线**而不是全局：一条新线第一次被问就该核对，不能被别的线的冷却挡住。"""
        redis = _FakeRedis(result="300", members=[f"sub_a{_TAB}100{_TAB}gone"])
        led = _participating(monkeypatch, redis, alive=set())
        await led.state("rs_root_a", "t1")
        await led.state("rs_root_b", "t1")
        assert len(redis.zranges) == 2
        assert redis.zranges[0] != redis.zranges[1], "两条线各扫各的，不能共用一份冷却"

    async def test_legacy_members_without_a_holder_are_never_touched(self, monkeypatch) -> None:
        """滚动升级：老实例写的两段 member 没有持有者可依据 ⇒ 不动，仍由 score 收口。"""
        redis = _FakeRedis(result="300", members=[f"sub_a{_TAB}100", f"sub_b{_TAB}200"])
        led = _participating(monkeypatch, redis, alive=set())
        assert await led.state("rs_root", "t1") is not None
        assert redis.zranges and redis.args_for("ZCARD") == []

    async def test_an_emptied_probe_blocks_every_removal(self, monkeypatch) -> None:
        """探测回空（协议没跑起来 / 连接没了）⇒ 一个都不删 —— 与"没参与"同一取向。"""
        from app import instance_liveness

        redis = _FakeRedis(result="300", members=[f"sub_a{_TAB}100{_TAB}gone"])
        led = _participating(monkeypatch, redis, alive=set())

        async def _unknown(_holders: Any) -> dict[str, bool]:
            return {}

        monkeypatch.setattr(instance_liveness, "holders_alive", _unknown)
        assert await led.state("rs_root", "t1") is not None
        assert redis.args_for("ZCARD") == []

    async def test_scan_failure_leaves_the_verdict_alone(self, monkeypatch) -> None:
        """读成员失败不改判定：预留数已经拿到了，扫不动就照原样判（TTL 仍是兜底）。"""
        redis = _FakeRedis(
            result="300", scan_error=True, members=[f"sub_a{_TAB}100{_TAB}gone"]
        )
        led = _participating(monkeypatch, redis, alive=set())
        state = await led.state("rs_root", "t1")
        assert state is not None and state.reserved == 300

    async def test_drop_failure_keeps_the_stale_number_but_never_raises(self, monkeypatch) -> None:
        """删不掉就**不重读**（重读只会拿到同一个数、白付一次往返），也不把异常抛进闸门。"""
        redis = _FakeRedis(
            result="300", drop_error=True, members=[f"sub_a{_TAB}100{_TAB}gone"]
        )
        led = _participating(monkeypatch, redis, alive=set())
        state = await led.state("rs_root", "t1")
        assert state is not None and state.reserved == 300
        assert len(redis.args_for("ZREMRANGEBYSCORE")) == 1

    def test_holder_field_is_the_third_segment(self) -> None:
        assert ledger_mod._holder_of(f"sub_a{_TAB}100{_TAB}engine-A") == "engine-A"
        assert ledger_mod._holder_of(f"sub_a{_TAB}100") == ""
        assert ledger_mod._holder_of("sub_a") == ""


# ── M17：优雅退出时的主动释放（drain）───────────────────────────────────


class TestDrainOnShutdown:
    async def test_drain_releases_every_root_still_held(self, monkeypatch) -> None:
        redis = _FakeRedis(result="0", results={"ZCARD": "1"})
        _set_holder(monkeypatch, "engine-A")
        led = _ledger(redis=redis)
        await led.reserve("rs_root1", "t1", "sub_a", 10)
        await led.reserve("rs_root2", "t1", "sub_b", 10)
        assert await led.drain("engine-A") == 2
        assert sorted(a[0].split(":")[-2] for a in redis.args_for("ZCARD")) == [
            "rs_root1",
            "rs_root2",
        ]
        assert {a[1] for a in redis.args_for("ZCARD")} == {"engine-A"}

    async def test_a_settled_handle_is_not_drained_again(self, monkeypatch) -> None:
        """收尾出账：drain 只该处理"还没来得及 settle"的那些。"""
        redis = _FakeRedis(result="0", results={"ZCARD": "1"})
        _set_holder(monkeypatch, "engine-A")
        led = _ledger(redis=redis)
        h1 = await led.reserve("rs_root1", "t1", "sub_a", 10)
        await led.reserve("rs_root2", "t1", "sub_b", 10)
        await led.settle("rs_root1", h1, 5)
        assert await led.drain("engine-A") == 1
        assert [a[0].split(":")[-2] for a in redis.args_for("ZCARD")] == ["rs_root2"]

    async def test_drain_forgets_what_it_released(self, monkeypatch) -> None:
        """删失败也不重试：登记簿先清，剩下的交给 TTL —— 关机不该被"还账没还干净"卡住。"""
        redis = _FakeRedis(result="0", results={"ZCARD": "1"})
        _set_holder(monkeypatch, "engine-A")
        led = _ledger(redis=redis)
        await led.reserve("rs_root", "t1", "sub_a", 10)
        await led.drain("engine-A")
        first = len(redis.args_for("ZCARD"))
        assert await led.drain("engine-A") == 0
        assert len(redis.args_for("ZCARD")) == first

    async def test_drain_without_an_instance_id_touches_nothing(self) -> None:
        redis = _FakeRedis()
        led = _ledger(redis=redis)
        assert await led.drain("") == 0
        assert redis.calls == [] and redis.zranges == []

    async def test_drain_with_nothing_held_asks_for_no_connection(self) -> None:
        """这个进程从没预留过 ⇒ 关机不该为 drain 去起一条 Redis 连接。

        关闭段**每个**实例都会走这条路，而"没派发过子 Agent"是绝大多数实例的常态；为一个空
        账去懒建连接既不必要（没有可摘的东西）也会让纯 PG 部署的关机多一条失败告警。
        """
        attempts: list[str] = []
        redis = _FakeRedis()

        async def _conn() -> Any:
            attempts.append("connect")
            return redis

        led = _ledger(redis=None)
        led._redis_conn = _conn  # type: ignore[method-assign]
        assert await led.drain("engine-A") == 0
        assert attempts == [] and redis.calls == []

    async def test_drain_failure_does_not_raise(self, monkeypatch) -> None:
        redis = _FakeRedis(result="0", drop_error=True)
        _set_holder(monkeypatch, "engine-A")
        led = _ledger(redis=redis)
        await led.reserve("rs_root", "t1", "sub_a", 10)
        assert await led.drain("engine-A") == 0

    async def test_release_helper_uses_the_process_identity(self, monkeypatch) -> None:
        """关机钩子那层的契约：拿的是本进程实例 id（与句柄第三段同一来源），且永不抛。"""
        redis = _FakeRedis(result="0", results={"ZCARD": "1"})
        led = _ledger(redis=redis)
        monkeypatch.setattr(ledger_mod, "_ledger", led)
        _set_holder(monkeypatch, "engine-A")
        await led.reserve("rs_root", "t1", "sub_a", 10)
        assert await ledger_mod.release_instance_reservations() == 1
        assert [a[1] for a in redis.args_for("ZCARD")] == ["engine-A"]


# ── 控制态的写（M15c：暂停与提额）──────────────────────────────────────


class TestControlStateWrite:
    """读侧 fail-open，**写侧必须如实返回失败** —— 这两个取向在 `set_control` 里分开。"""

    async def test_pause_stops_the_line_without_changing_the_cap(self) -> None:
        pool = _FakePool()
        assert await _ledger(pool=pool).pause_for_budget("rs_root", "t1") is True
        assert [c[1] for c in pool.ensures()] == [("rs_root", "t1")]
        assert len(pool.updates()) == 1
        root, tenant, cap, paused, reason, updated_by = pool.updates()[0][1]
        assert (root, tenant) == ("rs_root", "t1")
        assert cap is None, "暂停是停下来等人，不是额度变了 —— 动 cap 会让提额无从对账"
        assert paused is True
        assert reason == ledger_mod.PAUSE_REASON_BUDGET
        assert updated_by == ""

    async def test_raise_writes_cap_and_clears_pause_together(self) -> None:
        """上限与暂停是**一个决定的两半**：只提额不清暂停，闸门会继续报 `line_paused`。"""
        pool = _FakePool()
        led = _ledger(pool=pool)
        assert await led.raise_cap_and_resume(
            "rs_root", "t1", cap_tokens=5000, updated_by="u1"
        ) is True
        root, tenant, cap, paused, reason, updated_by = pool.updates()[0][1]
        assert (cap, paused, updated_by) == (5000, False, "u1")
        assert reason == "", "空串 = 清掉暂停原因（SQL 里 NULLIF 落 NULL），不是「不改」"

    async def test_ensure_row_runs_before_the_update(self) -> None:
        """没被设过的线在表里**没有行** ⇒ 先 `INSERT … DO NOTHING` 才能保证 UPDATE 命中 1 行。"""
        pool = _FakePool()
        await _ledger(pool=pool).pause_for_budget("rs_root", "t1")
        assert [c[0].split()[0] for c in pool.calls] == ["INSERT", "UPDATE"]

    async def test_wrong_tenant_reports_failure_and_writes_nothing_else(self) -> None:
        """主键只有 `root_run_id`，租户过滤全在那条 UPDATE 的 WHERE 上：匹配 0 行必须返回
        False（调用方据此报错），而不是"看起来成功了"。"""
        pool = _FakePool(update_count=0)
        assert await _ledger(pool=pool).pause_for_budget("rs_root", "other") is False
        assert len(pool.updates()) == 1

    async def test_write_failure_returns_false_instead_of_raising(self) -> None:
        """不 fail-open、也不抛：抛会把"通知人"变成能打断收尾的东西，fail-open 则是说谎。"""
        pool = _FakePool(write_error=True)
        assert await _ledger(pool=pool).pause_for_budget("rs_root", "t1") is False

    async def test_noop_change_does_not_touch_the_database(self) -> None:
        pool = _FakePool()
        assert await _ledger(pool=pool).set_control("rs_root", "t1") is True
        assert pool.calls == [], "什么都没要改，不该假装写了一次"

    @pytest.mark.parametrize("root,tenant", [("", "t1"), ("rs_root", ""), ("bad\troot", "t1")])
    async def test_unusable_keys_never_reach_the_database(self, root: str, tenant: str) -> None:
        pool = _FakePool()
        assert await _ledger(pool=pool).set_control(root, tenant, paused=True) is False
        assert pool.calls == []

    def test_update_sql_is_tenant_scoped_and_incremental(self) -> None:
        norm = " ".join(ledger_mod.LEDGER_UPDATE_SQL.split())
        assert "WHERE root_run_id = $1" in norm and "AND tenant_id = $2" in norm
        for col in ("cap_tokens", "paused"):
            assert f"COALESCE(${3 if col == 'cap_tokens' else 4}, {col})" in norm, col
        assert "NULLIF($5, '')::text" in norm, "要能区分「没带这个字段」与「要清掉它」"


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
        error: str = "",
        ticket_id: str = "tk_1",
    ) -> dict[str, Any]:
        import asyncio

        from app.agent import interactions as interactions_mod
        from app.agent import runtime as runtime_mod
        from app.agent.subagent_runner import SubAgentRunner
        from app.subagent.budget import TaskBudget

        stub = ledger or _StubLedger()
        monkeypatch.setattr(ledger_mod, "get_line_ledger", lambda: stub)
        seen: dict[str, Any] = {}
        tickets: list[dict[str, Any]] = []
        flushes: list[str] = []

        async def _create(**kwargs: Any) -> str:
            tickets.append(kwargs)
            return ticket_id

        monkeypatch.setattr(interactions_mod, "create", _create)

        class _Store:
            available = True

            async def start_run(self, **kwargs: Any) -> str:
                return store_root

            async def add_step(self, *args: Any, **kwargs: Any) -> None:
                return None

            async def flush_steps(self, run_id: str, *args: Any, **kwargs: Any) -> None:
                flushes.append(run_id)

            async def finish_run(self, *a: Any, **kw: Any) -> None:
                seen["finish_kwargs"] = kw

            async def mark_lifecycle(self, *args: Any, **kwargs: Any) -> None:
                return None

        async def _one_step_run(self: Any, task: Any):  # noqa: ANN001
            seen["subagent_line"] = getattr(task, "subagent_line", "")
            seen["task_id"] = task.id
            if error:
                yield runtime_mod.AgentEvent(type="error", error=error)
            else:
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
        seen["tickets"] = tickets
        seen["flushes"] = flushes
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

    # ── M18：花了钱就当场把这一格刷新（冲刷步骤缓冲 + 重述预留）──

    async def test_usage_flushes_the_steps_and_restates_the_reservation(
        self, monkeypatch
    ) -> None:
        """两件一起挂在**带用量的事件**上，因为它们是同一笔账的两半：崩在半路时，
        补账只认**已落库**的步骤行，而同线其它 run 只认 Redis 上那条预留。
        留在派发时那一格 = §5.2 ② 那笔系统性少算。"""
        seen = await self._drive(monkeypatch, store_root="rs_root")
        kinds = [c[0] for c in seen["ledger_calls"]]
        assert kinds == ["reserve", "project", "settle"], "顺序也是口径：预留要一直活着到收尾"
        assert len(seen["flushes"]) == 1
        (proj,) = [c for c in seen["ledger_calls"] if c[0] == "project"]
        assert proj[1:3] == ("rs_root", "t1")
        assert proj[3] == seen["task_id"], "member 与 reserve 用同一个键（task.id）"
        assert proj[4] == f"{seen['task_id']}{_TAB}200000", "带进去的是派发时那份 seed"
        assert proj[5:7] == (15, 200_000), "spent = 这一段看到的用量，cap = per-run token 上限"

    async def test_settle_uses_the_restatement_handle(self, monkeypatch) -> None:
        """句柄**会换**：tokens 编在 member 名字里，重述就是换一条 member。收尾拿旧句柄去
        `ZREM` 摘不掉新那条 ⇒ 它要占满一个 `RESERVE_TTL`，同线的派发被误拦（多算的一侧）。"""
        seen = await self._drive(monkeypatch, store_root="rs_root")
        (settle,) = [c for c in seen["ledger_calls"] if c[0] == "settle"]
        assert settle[2] == f"{seen['task_id']}{_TAB}115"  # = 替身 project 返回的新句柄

    async def test_events_without_usage_do_not_move_the_ledger(self, monkeypatch) -> None:
        """只带工具/错误的事件不刷：token 只在每次模型调用结束时出现一次，逐条事件都刷
        等于给工具流付两倍往返，而账上什么也没多。"""
        seen = await self._drive(monkeypatch, store_root="rs_root", error="tool_blew_up")
        assert [c for c in seen["ledger_calls"] if c[0] == "project"] == []
        assert seen["flushes"] == []

    async def test_no_line_means_no_restatement(self, monkeypatch) -> None:
        """落库不可用 ⇒ 没有线，也就没有可重述的预留（与"不假装有一条线"同一口径）。"""
        seen = await self._drive(monkeypatch, store_root="")
        assert [c for c in seen["ledger_calls"] if c[0] == "project"] == []
        assert seen["flushes"] == []

    # ── M15c：越界 ⇒ 暂停这条线 + 挂出一张提额待办 ──

    def _state(self, *, cap: int = 1000, settled: int = 1000) -> LineState:
        return LineState(
            root="rs_root",
            tenant_id="t1",
            cap=cap,
            settled=settled,
            reserved=0,
            paused=True,
            pause_reason=ledger_mod.PAUSE_REASON_BUDGET,
        )

    async def test_line_trip_pauses_the_line_and_files_one_ticket(self, monkeypatch) -> None:
        """闭环入口：越界那一段照常**失败收尾**（子 Agent 没有可接手挂起的现场），但这条线被
        暂停、并挂出一张用户答得上的票。"""
        stub = _StubLedger(state_obj=self._state())
        seen = await self._drive(
            monkeypatch, store_root="rs_root", ledger=stub, error="budget_exceeded:line_tokens"
        )
        assert [c for c in seen["ledger_calls"] if c[0] == "pause"] == [("pause", "rs_root", "t1")]
        # 票上的"已花"要包含刚刚落库的这一段 ⇒ 暂停/挂票必须排在 settle 之后，
        # 否则用户按一个偏小的数字提额，下一段立刻又被拦下。
        kinds = [c[0] for c in seen["ledger_calls"]]
        assert kinds.index("settle") < kinds.index("pause")
        assert seen["finish_kwargs"]["status"] == "failed", "越界是失败收尾，不是挂起"

        (ticket,) = seen["tickets"]
        assert ticket["kind"] == "budget"
        # 票落在**父会话**：合成会话 `sub:<父>:<run>` 没有待办列表 —— 用户看不见就等于线永远停着
        assert ticket["session_id"] == "s-main"
        assert (ticket["tenant_id"], ticket["user_id"]) == ("t1", "u1")
        # 合成 tool_call_id：一条线只有一张未决提额票（靠 ux_pending_interactions_open 去重）
        assert ticket["tool_call_id"] == "budget:rs_root"
        req = ticket["request"]
        assert req["root_run_id"] == "rs_root" and req["axis"] == "line_tokens"
        assert (req["cap_tokens"], req["settled_tokens"], req["reserved_tokens"]) == (
            1000,
            1000,
            0,
        )
        assert req["suggested_cap_tokens"] == 2000, "建议值只是预填的起点，不是决定"
        assert "1,000" in req["question"]

    async def test_paused_axis_also_files_a_ticket(self, monkeypatch) -> None:
        """`line_paused` 也要带出票 —— 线停着却没有待办，就是一个没有任何出口的状态。

        这条同时覆盖"读不到数字"（`state` 返回 None）时的取值：上限退成 0，建议值用默认的两倍，
        而不是留一个空框让人猜量级。
        """
        seen = await self._drive(
            monkeypatch, store_root="rs_root", error="budget_exceeded:line_paused"
        )
        (ticket,) = seen["tickets"]
        assert ticket["request"]["axis"] == "line_paused"
        assert ticket["request"]["suggested_cap_tokens"] == (
            ledger_mod.DEFAULT_LINE_MAX_TOKENS * 2
        )

    async def test_other_failures_do_not_pause_the_line(self, monkeypatch) -> None:
        """per-run 预算越界与"这条线花光了"是两件事：前者不该停线、也不该挂提额票。"""
        seen = await self._drive(
            monkeypatch, store_root="rs_root", error="budget_exceeded:tokens"
        )
        assert seen["tickets"] == []
        assert [c for c in seen["ledger_calls"] if c[0] in ("pause", "state")] == []

    async def test_no_line_means_no_ticket(self, monkeypatch) -> None:
        seen = await self._drive(
            monkeypatch, store_root="", error="budget_exceeded:line_tokens"
        )
        assert seen["tickets"] == [] and seen["ledger_calls"] == []

    async def test_pause_failure_still_files_the_ticket(self, monkeypatch) -> None:
        """暂停写不进去（多半是迁移 0022 没跑）时**仍然挂票**：宁可用户看见一张提额待办
        再去查为什么没生效，也不要一条既没停、也没人知道的线。"""
        seen = await self._drive(
            monkeypatch,
            store_root="rs_root",
            ledger=_StubLedger(pause_ok=False),
            error="budget_exceeded:line_tokens",
        )
        assert len(seen["tickets"]) == 1

    async def test_ticket_write_failure_does_not_break_the_run(self, monkeypatch) -> None:
        """挂票失败不改这一段已经定下的终态，也不抛 —— 但它是运维事故（线停着却没人能提额）。"""
        seen = await self._drive(
            monkeypatch,
            store_root="rs_root",
            error="budget_exceeded:line_tokens",
            ticket_id="",
        )
        assert [c for c in seen["ledger_calls"] if c[0] == "pause"]
        assert seen["finish_kwargs"]["status"] == "failed"

    async def test_pause_blowup_does_not_break_the_run(self, monkeypatch) -> None:
        class _Boom(_StubLedger):
            async def pause_for_budget(self, *a: Any, **k: Any) -> bool:
                raise RuntimeError("pg down")

        seen = await self._drive(
            monkeypatch, store_root="rs_root", ledger=_Boom(), error="budget_exceeded:line_tokens"
        )
        assert seen["tickets"] == []
        assert seen["finish_kwargs"]["status"] == "failed"


# ── 线账表的 DDL 契约（口径写进 DDL，别在下一轮被顺手改掉）───────────────
#
# 这些断言原先读 `0022_run_lineage_ledger.py`；该迁移已**收敛进权威基线**
# `0001_authoritative_baseline.py`（建表 / 约束 / 索引分别见基线 `_DDL`），
# 契约本身不变，数据源改为基线。
class TestLineageLedgerBaselineContract:
    def test_baseline_owns_the_table(self) -> None:
        """基线是根（无前驱），且表登记进 `_TABLES` —— downgrade 才能按 reversed 删掉它。"""
        src = _BASELINE.read_text(encoding="utf-8")
        assert 'revision: str = "0001_authoritative_baseline"' in src
        assert "down_revision: Union[str, Sequence[str], None] = None" in src
        assert '"run_lineage_ledger",' in src

    def test_table_stores_no_usage(self) -> None:
        """**本表不存 used / reserved**。累计一旦有第二份就一定会和明细对不上；预留则是
        会过期的协调态 —— 放表里需要一个回收任务，崩溃的 run 会把额度永久占住。

        判的是**列名**，不是子串：`updated_at` / `updated_by` 里也含 "used"，按子串禁会把
        一条正常的时间列也禁掉。
        """
        ddl = [s for s in _baseline_ddl() if "CREATE TABLE public.run_lineage_ledger" in s]
        assert len(ddl) == 1
        body = ddl[0].split("public.run_lineage_ledger", 1)[1]
        body = body[body.index("(") + 1 : body.rindex(")")]
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
            "cap_tokens bigint DEFAULT 0 NOT NULL",
            "paused boolean DEFAULT false NOT NULL",
            "updated_by",
        ):
            assert required in ddl[0], required
        # 主键在单独的 ALTER TABLE 里（pg_dump 顺序：建表 → 约束）
        assert any(
            "ADD CONSTRAINT run_lineage_ledger_pkey PRIMARY KEY (root_run_id)" in s
            for s in _lineage_ddl()
        )

    def test_paused_index_is_partial(self) -> None:
        """绝大多数线永远不会被暂停 ⇒ 只给 paused 行建索引。"""
        idx = [s for s in _lineage_ddl() if "CREATE INDEX" in s]
        assert len(idx) == 1
        assert "ix_run_lineage_ledger_paused" in idx[0]
        assert "WHERE paused" in idx[0]

    def test_control_state_query_is_tenant_scoped(self) -> None:
        assert "WHERE root_run_id = $1 AND tenant_id = $2" in " ".join(LEDGER_SELECT_SQL.split())

    def test_no_plpgsql_block(self) -> None:
        # 只判本表相关语句：基线里其它表可能含函数体
        assert all("$$" not in s for s in _lineage_ddl())

    def test_downgrade_is_the_reverse_order(self) -> None:
        src = _BASELINE.read_text(encoding="utf-8")
        down = src.split("def downgrade", 1)[1]
        assert "reversed(_TABLES)" in down
        assert "DROP TABLE IF EXISTS public.{name} CASCADE" in down


# ── 真 Redis：Lua 的算术只有 Redis 能判 ─────────────────────────────────


@pytest.fixture
async def redis_client():
    import redis.asyncio as aioredis

    client = aioredis.from_url(_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        # 环境**声明了** Redis（CI 的 real-stack job 设 REDIS_URL）却连不上 ⇒ 这是环境坏了，
        # 要红；没声明 ⇒ 这台机器/这个 job 本来就不跑真 Redis，skip 而不是静默地"通过"。
        if (os.getenv("REDIS_URL") or "").strip():
            pytest.fail(f"REDIS_URL 已设置但连不上（{_REDIS_URL}）：{exc}")
        pytest.skip(f"需要可达的 Redis（{_REDIS_URL}）才能跑线账的真脚本用例")
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


@pytest.mark.live
class TestRealRedisSemantics:
    """这些断言判的是脚本的**实际算术**：PG 侧一律用替身，保证只有 Redis 是真的。"""

    #: 钉死的持有者 id：预留句柄的第三段与 drain 的匹配键都由它决定，不钉住就只能断言前缀。
    HOLDER = "engine-livetest"

    @pytest.fixture(autouse=True)
    async def _outside_the_liveness_protocol(self, monkeypatch):
        """每条用例都从"本进程没在写存活键"开始，并把持有者钉成常量。

        ``participates()`` 与探测缓存是**模块级**状态：上一用例（包括 `test_instance_liveness.py`
        起过的心跳）留下的登记会让这里"不该扫"的用例真去扫，反之"该扫"的用例会拿到一份
        过期缓存。清扫的用例自己在用例里起心跳。
        """
        from app import instance_liveness as il

        saved_active, saved_cache = il._ACTIVE, dict(il._PROBE_CACHE)
        il._ACTIVE = None
        il._PROBE_CACHE.clear()
        _set_holder(monkeypatch, self.HOLDER)
        try:
            yield
        finally:
            il._ACTIVE = saved_active
            il._PROBE_CACHE.clear()
            il._PROBE_CACHE.update(saved_cache)

    def _ledger(self, redis: Any, *, usage: tuple[int, int] = (0, 0)) -> LineLedger:
        return LineLedger(
            redis=redis, pool=_FakePool(), store=_FakeStore(usage=usage)
        )

    async def test_reserve_excludes_the_callers_own_reservation(self, redis_client) -> None:
        root = _root_id("ex")
        led = self._ledger(redis_client)
        # 句柄三段：member、tokens、持有者实例 id（M17）—— 第三段是清扫与 drain 的匹配键
        assert await led.reserve(root, "t1", "sub_a", 100) == f"sub_a{_TAB}100{_TAB}{self.HOLDER}"
        assert await led.reserve(root, "t1", "sub_b", 250) == f"sub_b{_TAB}250{_TAB}{self.HOLDER}"
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

    # ── M17：持有者段、死实例清扫、退出 drain ──────────────────────────

    async def test_a_third_segment_does_not_void_the_reservation(self, redis_client) -> None:
        """预留求和必须**只取第二段**：三段 member 上 ``tonumber("1000\\tengine-Z")`` 是 nil。

        这是 M17 里唯一"错了还看起来一切正常"的地方 —— 那条预留按 0 计 ⇒ 上限静默失效，
        而且**只有**加上持有者段之后才发作。滚动升级窗口里老实例写的两段 member 同表核对：
        它也必须照旧计（少算的另一个来源）。
        """
        root = _root_id("seg")
        key = _rkey(f"subagent:line:{root}:rsv")
        until = int(time.time()) + 600
        await redis_client.zadd(
            key,
            {f"sub_new{_TAB}1000{_TAB}engine-Z": until, f"sub_old{_TAB}5": until},
        )
        try:
            state = await self._ledger(redis_client).state(root, "t1")
            assert state is not None and state.reserved == 1005
        finally:
            await redis_client.delete(key)

    async def test_a_sweep_drops_the_dead_holders_and_keeps_the_rest(self, redis_client) -> None:
        """真 Redis 上走完整条判据：死了的摘掉、活着的不碰、没署名的（滚动升级）留下等 TTL。

        ``engine-peer`` 的存活键是真 ``EXISTS`` 问出来的（不是自我豁免），``engine-gone``
        没有这个键 —— 这两条分支在桩里都只是字典查表，只有在真 Redis 上才是"读到了没有"。
        """
        from app import instance_liveness as il

        root = _root_id("sweep")
        key = _rkey(f"subagent:line:{root}:rsv")
        peer_alive = il.alive_key("engine-peer")
        until = int(time.time()) + 600
        await redis_client.zadd(
            key,
            {
                f"sub_dead{_TAB}700{_TAB}engine-gone": until,
                f"sub_live{_TAB}300{_TAB}engine-peer": until,
                f"sub_legacy{_TAB}100": until,
            },
        )
        await redis_client.set(peer_alive, "1", ex=il.ALIVE_TTL_SECONDS)
        hb = il.InstanceLiveness(redis=redis_client, instance_id="engine-self")
        assert await hb.start() is True
        try:
            state = await self._ledger(redis_client).state(root, "t1")
            assert state is not None and state.reserved == 400, "清扫后的重读口径不对"
            assert sorted(await redis_client.zrange(key, 0, -1)) == sorted(
                [f"sub_live{_TAB}300{_TAB}engine-peer", f"sub_legacy{_TAB}100"]
            ), "摘除必须按持有者精确到条：活着的那条与没署名的那条都不该动"
        finally:
            await hb.stop()
            await redis_client.delete(key, peer_alive, il.alive_key("engine-self"))

    async def test_no_participation_means_no_sweep(self, redis_client) -> None:
        """本进程没在写存活键 ⇒ "读不到别人的键"不是证据，一条都不许删。

        方向是**多算**（预留多占一个 TTL，派发被误拦），这是这条线可以接受的错；反过来
        （心跳没起来还照删）会把全站的在途预留扫空 —— 少算、上限静默失效，最坏的一类。
        用例里的 ``engine-gone`` 在任何存活键都查不到时依然留在全额里，就是这个安全阀。
        """
        from app import instance_liveness as il

        assert il.participates() is False
        root = _root_id("nosweep")
        key = _rkey(f"subagent:line:{root}:rsv")
        until = int(time.time()) + 600
        await redis_client.zadd(key, {f"sub_dead{_TAB}700{_TAB}engine-gone": until})
        try:
            state = await self._ledger(redis_client).state(root, "t1")
            assert state is not None and state.reserved == 700
            assert await redis_client.zrange(key, 0, -1) == [f"sub_dead{_TAB}700{_TAB}engine-gone"]
        finally:
            await redis_client.delete(key)

    # ── M18：覆盖式重述的实际算术 ────────────────────────────────────────

    async def test_a_restatement_replaces_the_seed_instead_of_adding_to_it(
        self, redis_client, monkeypatch
    ) -> None:
        """重述必须**换掉**旧那条：两条并存会一起进同线其它 run 的求和，200k 的 seed 加 30k 的
        投影 = 230k —— 一个 run 占了两个 run 的量（**多算**，会把一条正常的线判死）。"""
        monkeypatch.setenv("SUBAGENT_LINE_PROJECTION", "enforce")
        root = _root_id("proj")
        key = _rkey(f"subagent:line:{root}:rsv")
        led = self._ledger(redis_client)
        seed = await led.reserve(root, "t1", "sub_a", 200_000)
        out = await led.project(
            root,
            "t1",
            member="sub_a",
            handle=seed,
            spent=6_000,
            elapsed_seconds=30,
            per_run_cap=200_000,
        )
        assert out == f"sub_a{_TAB}30000{_TAB}{self.HOLDER}"
        assert await redis_client.zrange(key, 0, -1) == [out]
        state = await led.state(root, "t1")
        assert state is not None and state.reserved == 30_000, "别的 run 看得见的是投影值"
        own = await led.state(root, "t1", exclude_run_id="sub_a")
        assert own is not None and own.reserved == 0, "自己那份仍按实际 token 计，不能再算预留"
        await led.settle(root, out, 0)
        assert await redis_client.exists(key) == 0, "摘干净就该把键收掉"

    async def test_a_legacy_two_segment_member_is_restated_too(
        self, redis_client, monkeypatch
    ) -> None:
        """匹配只看第一段 ⇒ 老实例（或 shadow 期）写的两段 member 也换得掉，即使数值没变。

        这一条同时钉住"值不变也要重述"：留着那条没署名的，本实例退出时无从按 holder 摘它。
        """
        monkeypatch.setenv("SUBAGENT_LINE_PROJECTION", "enforce")
        root = _root_id("projleg")
        key = _rkey(f"subagent:line:{root}:rsv")
        await redis_client.zadd(key, {f"sub_a{_TAB}1000": int(time.time()) + 600})
        led = self._ledger(redis_client)
        out = await led.project(
            root,
            "t1",
            member="sub_a",
            handle="",
            spent=1_000,
            elapsed_seconds=10**6,
            per_run_cap=0,
        )
        assert out == f"sub_a{_TAB}1000{_TAB}{self.HOLDER}"
        assert await redis_client.zrange(key, 0, -1) == [out]

    async def test_a_restatement_leaves_other_runs_reservations_alone(
        self, redis_client, monkeypatch
    ) -> None:
        """同一条线上别的 run 的预留一条不动：那是它们各自的账。"""
        monkeypatch.setenv("SUBAGENT_LINE_PROJECTION", "enforce")
        root = _root_id("projpeer")
        led = self._ledger(redis_client)
        assert await led.reserve(root, "t1", "sub_a", 200_000)
        peer = await led.reserve(root, "t1", "sub_b", 50_000)
        out = await led.project(
            root,
            "t1",
            member="sub_a",
            handle=f"sub_a{_TAB}200000{_TAB}{self.HOLDER}",
            spent=6_000,
            elapsed_seconds=30,
            per_run_cap=200_000,
        )
        state = await led.state(root, "t1", exclude_run_id="sub_b")
        assert state is not None and state.reserved == 30_000, "剔掉 sub_b 后只剩重述过的那条"
        both = await led.state(root, "t1")
        assert both is not None and both.reserved == 80_000
        await led.settle(root, out, 0)
        await led.settle(root, peer, 0)

    async def test_a_restatement_pushes_the_expiry_forward(
        self, redis_client, monkeypatch
    ) -> None:
        """跑得久的合法 run 不该中途丢掉预留：重述顺带把 score 推到 ``now + RESERVE_TTL``。

        与 M17 的清扫是同一枚硬币的两面 —— 刷新让长 run 不被误当成幽灵，清扫让幽灵不被
        刷新当成活人。这里手工把 score 压到"马上到期"，重述后必须回到一小时的窗口。
        """
        monkeypatch.setenv("SUBAGENT_LINE_PROJECTION", "enforce")
        root = _root_id("projttl")
        key = _rkey(f"subagent:line:{root}:rsv")
        await redis_client.zadd(key, {f"sub_a{_TAB}1000{_TAB}{self.HOLDER}": int(time.time()) + 5})
        led = self._ledger(redis_client)
        out = await led.project(
            root,
            "t1",
            member="sub_a",
            handle=f"sub_a{_TAB}1000{_TAB}{self.HOLDER}",
            spent=8_000,
            elapsed_seconds=60,
            per_run_cap=200_000,
        )
        score = await redis_client.zscore(key, out)
        assert score is not None
        assert score > time.time() + ledger_mod.RESERVE_TTL_SECONDS - 60

    async def test_drain_releases_only_this_instances_reservations(self, redis_client) -> None:
        """优雅退出：本实例在册的两条线都腾空，别的实例在同一条线上的预留一条不动。"""
        root_a, root_b = _root_id("draina"), _root_id("drainb")
        key_a = _rkey(f"subagent:line:{root_a}:rsv")
        key_b = _rkey(f"subagent:line:{root_b}:rsv")
        led = self._ledger(redis_client)
        assert await led.reserve(root_a, "t1", "sub_a", 60)
        assert await led.reserve(root_b, "t1", "sub_b", 90)
        peer = f"sub_peer{_TAB}700{_TAB}engine-Z"
        await redis_client.zadd(key_a, {peer: int(time.time()) + 600})

        assert await led.drain(self.HOLDER) == 2
        assert await redis_client.zrange(key_a, 0, -1) == [peer], "drain 不能越走别人的预留"
        # 摘空就把键收掉：root_b 上只剩本实例那一条
        assert await redis_client.exists(key_b) == 0
        state = await led.state(root_a, "t1")
        assert state is not None and state.reserved == 700
        # 登记簿当场清空：同一次关机里重复 drain 不该把"已经还掉的"再报一遍
        assert await led.drain(self.HOLDER) == 0
        await redis_client.delete(key_a)


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
        # 140（已结算）+ 10（own）= 150，**正好触界**（判据是 `charged >= cap`）。
        # 原写的是 `own_tokens=9` ⇒ 149 < 150 并不越界，用例一直在断言一个算不出来的
        # 结果。它是 `integration` 标记，在真库出现之前从没跑过 ⇒ 一直没暴露。
        assert await led.check(root, "t1", own_tokens=10) == "line_tokens"
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

    # ── M15c 写侧：真库里的 ensure + 增量 UPDATE ──

    async def test_set_control_creates_the_row_for_an_unset_line(self, monkeypatch) -> None:
        """没被设过的线在表里**没有行** ⇒ 写侧必须先 ensure。少了那一步，UPDATE 命中 0 行、
        `set_control` 返回 False，而提额 API 会如实报错 —— 用户看到的是"提了却没提上去"。"""
        from app.db import get_pool

        monkeypatch.setenv("SUBAGENT_LINE_MAX_TOKENS", "1000")
        root = self._root("ensure")
        led = await self._ledger()
        assert await led.pause_for_budget(root, "t1") is True
        state = await led.state(root, "t1")
        assert state is not None
        assert (state.paused, state.pause_reason) == (True, ledger_mod.PAUSE_REASON_BUDGET)
        assert await led.check(root, "t1", own_tokens=0) == "line_paused"
        row = await get_pool().fetchrow(
            "SELECT tenant_id FROM run_lineage_ledger WHERE root_run_id = $1", root
        )
        assert row is not None and row["tenant_id"] == "t1"

    async def test_pause_is_idempotent(self) -> None:
        """每个失败段都会按一次暂停键 ⇒ 重复写必须没有副作用（这条线只该停一次）。"""
        root = self._root("idemp")
        led = await self._ledger()
        assert await led.pause_for_budget(root, "t1") is True
        assert await led.pause_for_budget(root, "t1") is True
        assert await led.check(root, "t1") == "line_paused"

    async def test_raise_cap_and_resume_is_one_decision_with_two_halves(self, monkeypatch) -> None:
        """提额同时清上限、清暂停、清原因并记下是谁抬的 —— 少一半就是"用户答了却没反应"。"""
        from app.db import get_pool

        monkeypatch.setenv("SUBAGENT_LINE_MAX_TOKENS", "1000")
        root = self._root("raise")
        await self._seed_run(root, tokens=900)
        led = await self._ledger()
        assert await led.pause_for_budget(root, "t1") is True
        assert await led.check(root, "t1", own_tokens=100) == "line_paused"

        assert await led.raise_cap_and_resume(
            root, "t1", cap_tokens=5000, updated_by="u1"
        ) is True
        state = await led.state(root, "t1")
        assert state is not None
        assert (state.cap, state.paused, state.pause_reason) == (5000, False, "")
        assert await led.check(root, "t1", own_tokens=100) == ""
        row = await get_pool().fetchrow(
            "SELECT updated_by FROM run_lineage_ledger WHERE root_run_id = $1", root
        )
        assert row["updated_by"] == "u1", "上限是谁抬的，出事时要能问到人"

    async def test_control_write_does_not_cross_tenants(self) -> None:
        """主键只有 `root_run_id`，那条 UPDATE 的租户条件是这条写路径唯一的护栏；
        `ensure` 用 DO NOTHING，也不会替别人的线建一行。"""
        root = self._root("crossw")
        await self._set_ledger(root, tenant="t1", cap=1000)
        led = await self._ledger()
        assert (
            await led.raise_cap_and_resume(root, "t2", cap_tokens=9_999_999, updated_by="u2")
            is False
        )
        state = await led.state(root, "t1")
        assert state is not None and state.cap == 1000, "别租户的线一个字都没被改"
