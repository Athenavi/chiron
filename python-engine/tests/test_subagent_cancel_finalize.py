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


@pytest.mark.asyncio
async def test_cancel_persists_terminal_state(monkeypatch):
    """被取消时，DB 里的 run 必须从 running 收敛到 cancelled（而不是永远挂着）。"""
    from app.agent import runtime as runtime_mod

    started = asyncio.Event()

    async def _hung_run(self, task):  # noqa: ANN001
        started.set()
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
