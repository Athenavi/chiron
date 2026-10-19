"""M15e：接管 / 续跑那一段**不再从 0 起算预算**（论文 §5.2「续跑次数 × 每 run 额度」）。

**这里证明什么**

`turns` 与 `steps` 两轴早就跨续跑继承起点了（A4），但 `usage` 格里的 token 计数一直没人读
—— 于是崩溃接管的那一段拿着一份全新的 per-run 额度起算；而接管路径
（`app/main.py` 的 `_auto_resume_runner`）**根本不走网关**，租户额度池连估算都不记 ⇒
这一条路径上两件事叠成"没有任何上限"。

分层因此是：

* 起点**读得对不对**（快照 → `ResumeState`）在 `tests/test_run_resume.py`；
* 本文件只驱动真 `runtime.run`，钉四件事：越界判定读的是**全程累计**、`done` 的扁平字段
  仍是**本段**（那是网关的计费入口，改成全程会把已报过账的那一段扣两遍）、回合末落盘的
  `usage` 是**赋值式的全程累计**（接管链上不会数两遍）、wall 轴的起点也被拨回去了。

预留（`LineLedger.reserve`）在这里一次都不该出现 —— 见最后一条用例的说明。
"""

from __future__ import annotations

from typing import Any

from app.agent.resume import ResumeState
from app.gateway.provider import ChatResponse, ToolCall

#: 每次 LLM 调用回给你的用量（input+output=40），够小够清楚
_CALL_INPUT = 30
_CALL_OUTPUT = 10


class _ScriptedGateway:
    """按脚本回用量：前 `tool_turns` 次返回工具调用（⇒ 续到下一轮），之后 `stop`。"""

    def __init__(self, *, tool_turns: int = 0, cached_tokens: int = 0) -> None:
        self._tool_turns = tool_turns
        self._cached = cached_tokens
        self.calls: list[dict[str, Any]] = []

    async def chat_stream(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        n = len(self.calls)
        if n <= self._tool_turns:
            yield ChatResponse(
                content="",
                finish_reason="tool_calls",
                # 不存在的工具名：结果报错，但循环照常下一轮 —— 这里要数的不是工具成败
                tool_calls=[ToolCall(id=f"c{n}", name="__absent_tool__", arguments="{}")],
                input_tokens=_CALL_INPUT,
                output_tokens=_CALL_OUTPUT,
                cached_tokens=self._cached,
            )
        else:
            yield ChatResponse(
                content="收尾",
                finish_reason="stop",
                input_tokens=_CALL_INPUT,
                output_tokens=_CALL_OUTPUT,
                cached_tokens=self._cached,
            )


class _LedgerSpy:
    """记录线账被怎么调用（接管段一条都不该有）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def check(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(("check", args))
        return ""

    async def reserve(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(("reserve", args))
        return ""

    async def settle(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("settle", args))


async def _drive(
    monkeypatch: Any,
    *,
    resume: ResumeState | None,
    cap_tokens: int,
    gateway: Any,
    max_turns: int = 3,
) -> tuple[list[Any], list[dict[str, Any]], _LedgerSpy]:
    """跑真 `runtime.run`：现场换成注入的 `ResumeState`，落盘与终态写入只记录不连库。

    预算各轴只有 tokens 这一条参与判定（其余显式设成 0 = 不限），这样"越界"只能是
    token 轴，断言不会被别的轴抢先。
    """
    import app.subagent.line_ledger as ledger_mod
    from app.agent.runtime import AgentRuntime, AgentTask

    monkeypatch.setenv("AGENT_RUN_MAX_TOKENS", str(cap_tokens))
    monkeypatch.setenv("AGENT_MAX_STEPS", "0")
    monkeypatch.setenv("AGENT_RUN_MAX_TURNS", "0")
    monkeypatch.setenv("AGENT_MAX_SECONDS", "0")

    spy = _LedgerSpy()
    monkeypatch.setattr(ledger_mod, "get_line_ledger", lambda: spy)

    runtime = AgentRuntime(gateway=gateway)

    async def _load(_task: Any) -> ResumeState | None:
        return resume

    saved: list[dict[str, Any]] = []

    async def _save(
        _task: Any, _messages: list[Any], *, turn_index: int, done_tools: list[str], usage: dict[str, int]
    ) -> None:
        saved.append({"turn_index": turn_index, "usage": usage, "done_tools": list(done_tools)})

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(runtime, "_load_resume_state", _load)
    monkeypatch.setattr(runtime, "_save_checkpoint", _save)
    monkeypatch.setattr(runtime, "_mark_run_status", _noop)

    task = AgentTask(
        id="run_m15e",
        tenant_id="t7",
        user_id="u",
        session_id="s1",
        content="继续",
        max_turns=max_turns,
    )
    events = [event async for event in runtime.run(task)]
    return events, saved, spy


def _error_of(events: list[Any]) -> str:
    for event in events:
        if event.type == "error":
            return str(event.error or "")
    return ""


def _done(events: list[Any]) -> Any:
    return next((e for e in events if e.type == "done"), None)


async def test_takeover_trips_on_the_run_total_not_the_segment(monkeypatch) -> None:
    """继承的 970 + 一次调用 40 ⇒ 第二次轮首就越界。

    同一份脚本、同一个上限，**没有**继承起点时这段能跑满 3 轮再正常收尾 —— 那正是缺陷本身：
    每次接管都白送一整份 per-run 额度。
    """
    gateway = _ScriptedGateway(tool_turns=2)
    resume = ResumeState(
        messages=[{"role": "user", "content": "继续"}],
        turn_index=0,
        input_tokens=970,
    )

    events, _saved, _spy = await _drive(
        monkeypatch, resume=resume, cap_tokens=1000, gateway=gateway, max_turns=3
    )

    assert _error_of(events) == "budget_exceeded:tokens"
    assert len(gateway.calls) == 1, "起点已经吃掉额度，就不能再放行两轮"


async def test_run_without_resume_still_starts_at_zero(monkeypatch) -> None:
    """没有现场 ⇒ 一切照旧：起点 0，本段花多少算多少（不能让正常 run 凭空背一笔账）。"""
    gateway = _ScriptedGateway(tool_turns=2)

    events, _saved, _spy = await _drive(
        monkeypatch, resume=None, cap_tokens=1000, gateway=gateway, max_turns=3
    )

    done = _done(events)
    assert _error_of(events) == "" and done is not None
    assert len(gateway.calls) == 3
    assert done.budget["tokens"]["used"] == 3 * (_CALL_INPUT + _CALL_OUTPUT)


async def test_done_reports_the_segment_while_budget_reports_the_run(monkeypatch) -> None:
    """两条口径**必须分开**，否则会多扣钱。

    网关按终态事件计费（Go 的 `usageTotals.withEvent` 只并 `done` 的扁平字段）。人工介入挂起
    那一段已经用它自己的 `done` 报过一次账；续跑这段如果把全程累计塞进同一批字段，同一段
    token 就会被 `DeductTokens` 扣两遍。所以：扁平字段 = 本段，`budget.used` = 全程。
    """
    gateway = _ScriptedGateway()
    resume = ResumeState(
        messages=[{"role": "user", "content": "继续"}],
        input_tokens=100,
        output_tokens=20,
    )

    events, _saved, _spy = await _drive(
        monkeypatch, resume=resume, cap_tokens=10**9, gateway=gateway, max_turns=2
    )

    done = _done(events)
    assert (done.input_tokens, done.output_tokens) == (_CALL_INPUT, _CALL_OUTPUT)
    assert done.budget["tokens"]["used"] == 100 + 20 + _CALL_INPUT + _CALL_OUTPUT


async def test_checkpoint_stores_the_run_total_and_never_double_counts(monkeypatch) -> None:
    """落盘那格是**赋值式的全程累计**：第二轮写的是「起点 + 本段两轮」，不是「起点 ×2 + 本段」。

    快照是同一行的同一格，每回合末整体覆写一次；改成相加会让接管链上的同一批 token
    每续跑一次就多算一遍 —— 那是比少算更糟的谎（它会用一笔根本没花过的钱把线判死）。
    """
    # tool_turns=2 ⇒ 前两轮各落一次盘（回合末落盘只在"还要继续下一轮"时发生），第三轮收尾。
    gateway = _ScriptedGateway(tool_turns=2)
    resume = ResumeState(
        messages=[{"role": "user", "content": "继续"}],
        input_tokens=700,
        output_tokens=50,
        cached_tokens=33,
        tool_steps=4,
    )

    _events, saved, _spy = await _drive(
        monkeypatch, resume=resume, cap_tokens=10**9, gateway=gateway, max_turns=3
    )

    assert [row["turn_index"] for row in saved] == [1, 2], "每个续跑的回合末各落一次盘"
    first, second = (row["usage"] for row in saved)
    assert first["input_tokens"] == 700 + _CALL_INPUT
    assert second["input_tokens"] == 700 + 2 * _CALL_INPUT, "起点不能被数两遍"
    assert second["output_tokens"] == 50 + 2 * _CALL_OUTPUT
    assert second["cached_tokens"] == 33, "本段没有命中缓存 ⇒ 累计仍是继承来的那份"
    assert second["tool_steps"] == 4 + 2, "起点同样并进 steps（A4 的既有口径）"
    assert second["wall_seconds"] >= 0


async def test_wall_axis_start_is_pushed_back(monkeypatch) -> None:
    """wall 轴与 tokens 同一处理：上一段的 500s 要接着算，不是每次接管重拨一块表。"""
    gateway = _ScriptedGateway()
    resume = ResumeState(
        messages=[{"role": "user", "content": "继续"}],
        wall_seconds=500,
    )

    events, _saved, _spy = await _drive(
        monkeypatch, resume=resume, cap_tokens=10**9, gateway=gateway, max_turns=2
    )

    assert _done(events).budget["wall"]["used"] >= 500


async def test_takeover_segment_opens_no_line_reservation(monkeypatch) -> None:
    """4B 的取舍钉在这里：接管段**不新增预留**。

    预留按 per-run 额度占住一条线，而"一段还不知道会跑多久的续跑"先占一份额度，正是
    "续跑次数 × 预留额"那个缺陷的形态；再说主 run 那条线在 `subagent_runs` 里根本没有行
    （线账的账本单位是委派树），它唯一的账就是上面那一格 `agent_runs.checkpoint.usage`。
    代价如实认下来：接管段对突发没有防护，只按**已发生量**判停。
    """
    gateway = _ScriptedGateway()
    resume = ResumeState(
        messages=[{"role": "user", "content": "继续"}],
        input_tokens=10,
    )

    _events, _saved, spy = await _drive(
        monkeypatch, resume=resume, cap_tokens=10**9, gateway=gateway, max_turns=2
    )

    assert spy.calls == [], "接管段既不查线账也不留预留（它没有线），更不能预留出一份额度"
