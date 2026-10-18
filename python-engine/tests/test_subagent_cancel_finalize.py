"""取消路径必须写入**终态**。

回归背景（实测故障）：父任务结束、SSE 断流或用户切会话都会取消子 Agent，
而 **asyncio 的取消传播会打断此后所有 await** —— 于是 `SubAgentRunner.run()` 里
正常路径的 `finish_run(...)` 从不执行：

* DB `subagent_runs.status` 永远停在 `"running"`，`finished_at`/`summary` 恒为 NULL；
* Redis 运行期缓存同理；
* 侧边栏因此永远显示"本次会话还没有子 Agent 运行"或一直"运行中"、拿不到结果。

实测证据：一个已跑 140 步的 run（`rs_9666a71befe8`）停在 `running`，`summary` 为空。

修复方式：取消分支把终态写库放进**独立任务**（不 await），让它在后台写完 ——
原则取自 ZCode：**run 的真相在 journal，观察面出问题绝不该影响它**。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent.subagent_runner import SubAgentRunner


class _RecordingStore:
    """宽松签名，避免与 SubagentRunStore 的参数名细节耦合。"""

    def __init__(self) -> None:
        self.started = False
        self.finished = False
        self.finished_status: str | None = None
        self.finished_summary: str | None = None
        self.finished_usage: dict = {}

    async def start_run(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.started = True
        return kwargs.get("run_id") or (args[0] if args else "rs_test")

    async def add_step(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None

    async def flush_steps(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None

    async def finish_run(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.finished = True
        self.finished_status = kwargs.get("status")
        self.finished_summary = kwargs.get("summary")
        self.finished_usage = {
            "input_tokens": kwargs.get("input_tokens"),
            "output_tokens": kwargs.get("output_tokens"),
            "steps": kwargs.get("steps"),
        }

    async def mark_lifecycle(self, *args, **kwargs):  # noqa: ANN002, ANN003
        """A5：记录遥测调用（真实写入由 store 自己的测试覆盖）。"""
        self.lifecycles = getattr(self, "lifecycles", [])
        self.lifecycles.append(kwargs)


class _RecordingSink:
    """只关心终态那一帧；其余 `emit_*` 由 `__getattr__` 吞掉。"""

    def __init__(self) -> None:
        self.done_events: list[dict] = []

    def emit_done(self, **kwargs):  # noqa: ANN002
        self.done_events.append(kwargs)

    def __getattr__(self, name: str):
        def _noop(*args, **kwargs):  # noqa: ANN002, ANN003
            return None

        return _noop


@pytest.mark.asyncio
async def test_cancel_persists_terminal_state(monkeypatch):
    """被取消时，DB 里的 run 必须从 running 收敛到 cancelled（而不是永远挂着）。"""
    from app.agent import runtime as runtime_mod

    started = asyncio.Event()

    async def _hung_run(self, task):  # noqa: ANN001
        started.set()
        yield runtime_mod.AgentEvent(type="text", content="先出了一段结果", input_tokens=120, output_tokens=30)
        await asyncio.sleep(3600)  # 永不结束：等外部取消
        yield runtime_mod.AgentEvent(type="text", content="unreachable")

    monkeypatch.setattr(runtime_mod.AgentRuntime, "run", _hung_run)

    store = _RecordingStore()
    runner = SubAgentRunner(
        store=store,
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

    # 终态写库发生在独立任务里 → 让出一轮事件循环让它跑完
    for _ in range(5):
        await asyncio.sleep(0)

    assert store.started, "子 Agent 应该已经启动并入账"
    assert store.finished, (
        "取消后**必须**写入终态：否则 status 永远停在 running、summary 永远为空，"
        "侧边栏永远拿不到结果（这正是被修复的故障）"
    )
    assert store.finished_status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_emits_terminal_event_with_real_usage(monkeypatch):
    """M15d：取消路径的 `subagent.done` 必须带**真实**用量，且与落库的那一份一致。

    此前它不带 `usage` ⇒ 实时流上表现为"这一段没花钱"，而几秒后 PG 里写着真实数字。
    两个面各说一套时，前端面板先显示的是那个假的。
    """
    from app.agent import runtime as runtime_mod

    started = asyncio.Event()

    async def _hung_run(self, task):  # noqa: ANN001
        started.set()
        yield runtime_mod.AgentEvent(type="text", content="花过 token 的一段", input_tokens=800, output_tokens=210)
        await asyncio.sleep(3600)
        yield runtime_mod.AgentEvent(type="text", content="unreachable")

    monkeypatch.setattr(runtime_mod.AgentRuntime, "run", _hung_run)

    store = _RecordingStore()
    sink = _RecordingSink()
    runner = SubAgentRunner(
        store=store,
        sink=sink,
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

    terminal = [e for e in sink.done_events if e.get("status") == "cancelled"]
    assert terminal, "取消必须有一帧终态，否则前端停在'运行中'"
    usage = terminal[-1].get("usage") or {}
    assert usage == {"input_tokens": 800, "output_tokens": 210, "steps": 1}, (
        f"终态帧的用量不能是空的（前端会显示 0），得到 {usage}"
    )
    # 一致性的另一半：实时流报的数 == 落库的数
    assert store.finished_usage == {
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "steps": usage["steps"],
    }
