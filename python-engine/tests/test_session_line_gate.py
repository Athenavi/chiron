"""M26 会话线闸门：主 run 的回合累计也要有线级上限。

**现场**（论文 §5.2）：线账的账本是 `subagent_runs` 按 `root_run_id` 的求和，而主 run 不写那张
表 ⇒ 只有主 run 的会话（最常见形态）线上 `settled` 恒 0，cap 对它零约束。可线账要拦的
"续跑次数 × 每 run 额度 = 无上限"说的正是主 run 的反复续跑。

三条不变量：

1. 会话线的账本来自 `turns`（按 `session_id`），**不是** `subagent_runs`；
2. `shadow`（默认）只算、打点，**不改判定**；`enforce` 越界才返回轴名（由调用方硬拦 + 挂票）；
3. 问不出来（无 session_id / `state` 为 `None`）一律放行 —— 闸门抖动不该停掉全站。
"""
from __future__ import annotations

import pytest

from app.observability.metrics import SESSION_LINE_GATE
from app.subagent.line_ledger import LineLedger, LineState


def _count(outcome: str, axis: str) -> float:
    """读 counter（规划 §4 同族第 ⑨ 条的读法：行为不可辨的分支要用可观测副作用钉住）。"""
    return SESSION_LINE_GATE.labels(outcome=outcome, axis=axis)._value.get()


def _state(*, cap: int = 100, settled: int = 100, paused: bool = False) -> LineState:
    return LineState(
        root="sess-1",
        tenant_id="t1",
        cap=cap,
        settled=settled,
        reserved=0,
        paused=paused,
        pause_reason="",
    )


class _StubStore:
    """只实现 `session_usage`：`turns` 求和的替身。"""

    def __init__(self, *, tokens: int | None = 0) -> None:
        self.tokens = tokens
        self.seen: list[tuple[str, str]] = []

    async def session_usage(self, session_id: str, tenant_id: int) -> tuple[int, int] | None:
        self.seen.append((session_id, tenant_id))
        if self.tokens is None:
            return None
        return self.tokens, 1


class _StubLedger(LineLedger):
    """`state` 换成可编程返回 —— 单测闸门的三档，不碰 PG/Redis。"""

    def __init__(self, *, state_obj: LineState | None) -> None:
        super().__init__()
        self._state_obj = state_obj
        self.scopes: list[str] = []

    async def state(
        self,
        root: str,
        tenant_id: int,
        *,
        exclude_run_id: str = "",
        scope: str = "delegation",
    ) -> LineState | None:
        self.scopes.append(scope)
        return self._state_obj


async def test_off_mode_does_not_even_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_LINE_GATE", "off")
    led = _StubLedger(state_obj=_state())

    assert await led.check_session("sess-1", "t1") == ""
    assert led.scopes == [], "off 档不该去问账本（那是每轮一次 PG 聚合）"
    assert _count("off", "-") >= 1


async def test_shadow_counts_but_never_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认档：越界只打点，**判定不变** —— 这条是"默认什么都不做"约束的落点。"""
    monkeypatch.delenv("SESSION_LINE_GATE", raising=False)
    led = _StubLedger(state_obj=_state(cap=100, settled=100))

    assert await led.check_session("sess-1", "t1", own_tokens=10) == ""
    assert led.scopes == ["session"], "会话线必须用 session 账本"
    assert _count("shadow", "line_tokens") >= 1


async def test_enforce_returns_the_axis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_LINE_GATE", "enforce")
    led = _StubLedger(state_obj=_state(cap=100, settled=100))

    assert await led.check_session("sess-1", "t1", own_tokens=10) == "line_tokens"
    assert _count("tripped", "line_tokens") >= 1


async def test_paused_line_is_reported_on_its_own_axis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_LINE_GATE", "enforce")
    led = _StubLedger(state_obj=_state(cap=1000, settled=0, paused=True))

    assert await led.check_session("sess-1", "t1") == "line_paused"


async def test_unknown_state_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """问不出来（表缺失 / 库抖）一律放行 —— 与 check() 同口径。"""
    monkeypatch.setenv("SESSION_LINE_GATE", "enforce")
    led = _StubLedger(state_obj=None)

    assert await led.check_session("sess-1", "t1") == ""
    assert _count("unknown", "-") >= 1


async def test_ok_state_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_LINE_GATE", "enforce")
    led = _StubLedger(state_obj=_state(cap=1000, settled=10))

    assert await led.check_session("sess-1", "t1") == ""
    assert _count("ok", "-") >= 1


async def test_session_scope_reads_turns_not_subagent_runs() -> None:
    """账本来源是**会话**：`scope="session"` 必须走 `session_usage`（turns），
    而不是 `lineage_usage`（subagent_runs）—— 后者里根本没有主 run 的行。"""
    store = _StubStore(tokens=1234)
    led = LineLedger(store=store)

    assert await led._settled("sess-1", "t1", scope="session") == 1234
    assert store.seen == [("sess-1", "t1")]


async def test_session_settled_is_unknown_without_an_answer() -> None:
    """`session_usage` 说"问不出来"时必须传成 `None`（不是 0）——
    否则闸门会把"查不到"判成"还有额度"。"""
    led = LineLedger(store=_StubStore(tokens=None))

    assert await led._settled("sess-1", "t1", scope="session") is None
