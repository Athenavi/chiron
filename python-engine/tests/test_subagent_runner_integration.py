"""P1 集成测试：SubAgentRunner 的完整事件链路（不依赖真实 LLM）。

用 monkeypatch 替换 ``AgentRuntime.run``，断言三件事：
1. 子 Agent 的进度事件确实携带层级字段（run_id/parent_run_id/depth/profile）进入旁路；
2. ``[thinking]`` 包装的思考分流到 reasoning 频道，**不进入 L2 输出**；
3. 收尾唯一终态（subagent.done）带 usage，且发生在预览之后。
"""
from __future__ import annotations

import pytest

from app.agent.event_sink import EV_DONE, EV_REASONING, EV_STARTED, EV_STATUS, EV_TEXT, EventSink
from app.agent.subagent_runner import SubAgentRunner


def _fake_run_factory(events):
    async def _fake_run(self, task):  # noqa: ANN001 - 模拟 AgentRuntime.run 的签名
        for event in events:
            yield event

    return _fake_run


@pytest.mark.asyncio
async def test_runner_emits_hierarchy_events_and_separates_reasoning(monkeypatch):
    from app.agent import runtime as runtime_mod

    events = [
        runtime_mod.AgentEvent(type="text", content="[thinking]先读文件[/thinking]"),
        runtime_mod.AgentEvent(type="text", content="结论：改动没问题"),
        runtime_mod.AgentEvent(type="tool_call", tool_name="read_file", tool_call_id="c1"),
        runtime_mod.AgentEvent(type="text", content="", input_tokens=30, output_tokens=12),
    ]
    monkeypatch.setattr(runtime_mod.AgentRuntime, "run", _fake_run_factory(events))

    sink = EventSink(merge_window=0.0)
    runner = SubAgentRunner(
        gateway=object(),          # 摘要会失败并回落提取式（本测试不关心摘要质量）
        sink=sink,
        parent_session_id="s1",
        parent_run_id="rs_parent",
        tenant_id="t1",
        user_id="u1",
    )
    result = await runner.run("看看这个改动", profile_ref="", mode="normal", max_turns=3)

    # 1) L2 输出：只含正文 + 不可信包装；思考被剥离
    assert "结论：改动没问题" in result.output
    assert "先读文件" not in result.output
    assert result.output.startswith("<subagent-result")
    assert result.status == "completed"

    # 2) 旁路事件：层级字段齐全 + 顺序正确
    drained = sink.drain()
    types = [e.type for e in drained]
    assert types[0] == EV_STARTED
    assert types[-1] == EV_DONE
    assert EV_REASONING in types and EV_TEXT in types and EV_STATUS in types
    for event in drained:
        assert event.run_id == result.run_id
        assert event.depth == 1
        assert event.parent_run_id == "rs_parent"

    reasoning = next(e for e in drained if e.type == EV_REASONING)
    assert reasoning.content == "先读文件"

    text = next(e for e in drained if e.type == EV_TEXT)
    assert text.content == "结论：改动没问题"

    done = drained[-1]
    assert done.usage.get("output_tokens") == 12


@pytest.mark.asyncio
async def test_runner_depth_guard_blocks_further_delegation(monkeypatch):
    """max_depth=1 且当前深度已 1 → 直接失败（不再起子 Agent）。"""
    runner = SubAgentRunner(gateway=object(), depth=1, parent_session_id="s1")
    result = await runner.run("再委派一层")
    assert result.status == "failed"
    assert "depth" in result.error


@pytest.mark.asyncio
async def test_runner_reports_failure_without_output(monkeypatch):
    from app.agent import runtime as runtime_mod

    events = [runtime_mod.AgentEvent(type="error", error="provider 500")]
    monkeypatch.setattr(runtime_mod.AgentRuntime, "run", _fake_run_factory(events))

    sink = EventSink(merge_window=0.0)
    runner = SubAgentRunner(gateway=object(), sink=sink, parent_session_id="s1")
    result = await runner.run("会失败的任务")

    assert result.status == "failed"
    assert "provider 500" in result.error
    # 失败也要有唯一终态，且 notice 告知原因
    types = [e.type for e in sink.drain()]
    assert types[-1] == EV_DONE
    assert "subagent.notice" in types


# ── R2：写路径仲裁的**接线**（模块有测试 ≠ 接上了 —— 这两条钉的是接线本身）──


def _blocking_run_factory(started, go):
    """跑起来先停住（等 ``go``），好让测试观察"槽在 run 期间被占"。"""
    from app.agent import runtime as runtime_mod

    async def _fake_run(self, task):  # noqa: ANN001 - 模拟 AgentRuntime.run 的签名
        started.set()
        await go.wait()
        yield runtime_mod.AgentEvent(type="text", content="写完了")

    return _fake_run


async def _wait_until(predicate, *, attempts: int = 300, interval: float = 0.01) -> None:
    """把事件循环推进到 ``predicate`` 为真。

    比"睡固定的几次"强：'第二个委派已经进入排队' 本身是**正面证据**，
    而"睡了几次它还没开始"只是没观察到 —— 断言得更弱。

    用**真实**的小睡眠而不是 `sleep(0)`：`run()` 在跑到 runtime 之前会走运行期缓存与归属
    租约（Redis），只让出控制权推不动真实 I/O。
    """
    import asyncio

    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not met within scheduling budget")


@pytest.mark.asyncio
async def test_write_slot_is_taken_and_released_around_run(monkeypatch):
    """开启仲裁后：槽在 run 期间**确实被占**，run 结束后**必然释放**（finally）。"""
    import asyncio

    from app.agent import runtime as runtime_mod
    from app.config import settings
    from app.subagent import scheduler as scheduler_mod
    from app.subagent.scheduler import get_scheduler
    from app.tools.sandbox import workspace_dir

    monkeypatch.setattr(settings, "subagent_write_arbitration", True)
    monkeypatch.setattr(settings, "subagent_max_writers", 1)
    scheduler_mod.reset_schedulers()

    started, go = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(runtime_mod.AgentRuntime, "run", _blocking_run_factory(started, go))

    runner = SubAgentRunner(
        gateway=object(),
        parent_session_id="sess-slot",
        tenant_id="t1",
        user_id="u1",
        # 可写且未声明路径 ⇒ 整工作区声明 ⇒ 占 writer 槽
        allow_write=True,
    )
    task = asyncio.create_task(runner.run("写点东西", max_turns=1))
    await asyncio.wait_for(started.wait(), timeout=2.0)

    sched = get_scheduler("sess-slot", workspace_root=str(workspace_dir()))
    assert sched is not None
    assert sched.active_counts() == (1, 1)      # 拿到槽才开跑

    go.set()
    result = await asyncio.wait_for(task, timeout=2.0)
    assert result.status == "completed"
    assert sched.active_counts() == (0, 0)      # 释放是 finally 的必然结果
    scheduler_mod.reset_schedulers()


@pytest.mark.asyncio
async def test_concurrent_writers_serialize_through_the_runner(monkeypatch):
    """同一会话的两个**可写**委派：第二个必须排队，不能同时开跑（否则会静默互相覆盖）。"""
    import asyncio

    from app.agent import runtime as runtime_mod
    from app.config import settings
    from app.subagent import scheduler as scheduler_mod
    from app.subagent.scheduler import get_scheduler
    from app.tools.sandbox import workspace_dir

    monkeypatch.setattr(settings, "subagent_write_arbitration", True)
    scheduler_mod.reset_schedulers()

    go = asyncio.Event()
    started = asyncio.Event()
    spawned: list[str] = []

    async def _fake_run(self, task):  # noqa: ANN001 - 模拟 AgentRuntime.run 的签名
        # runtime.run 收到的是 AgentTask（不是字符串）—— 取它的 content 做记号
        spawned.append(getattr(task, "content", ""))
        started.set()
        await go.wait()
        yield runtime_mod.AgentEvent(type="text", content="ok")

    monkeypatch.setattr(runtime_mod.AgentRuntime, "run", _fake_run)

    def _runner() -> SubAgentRunner:
        return SubAgentRunner(
            gateway=object(),
            parent_session_id="sess-serial",
            tenant_id="t1",
            user_id="u1",
            allow_write=True,
        )

    sched = get_scheduler("sess-serial", workspace_root=str(workspace_dir()))
    assert sched is not None

    first = asyncio.create_task(_runner().run("任务一", max_turns=1))
    await asyncio.wait_for(started.wait(), timeout=5.0)
    assert spawned == ["任务一"]
    assert sched.active_counts() == (1, 1)

    second = asyncio.create_task(_runner().run("任务二", max_turns=1))
    # 正面证据：第二个**已经进入排队**（不是"还没轮到调度"）
    await _wait_until(lambda: sched.pending_waiter_count() == 1)
    assert spawned == ["任务一"]                # 还在等槽：**没有**同时开跑

    go.set()
    results = await asyncio.wait_for(asyncio.gather(first, second), timeout=5.0)
    assert [r.status for r in results] == ["completed", "completed"]
    assert spawned == ["任务一", "任务二"]
    assert sched.active_counts() == (0, 0)
    scheduler_mod.reset_schedulers()
