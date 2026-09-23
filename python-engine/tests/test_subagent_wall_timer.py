"""子 Agent 的 wall 定时器必须与"事件流是否前进"无关。

回归背景 —— 这正是 `DEFAULT_SYNC_MAX_SECONDS` 曾经失效的机械原因：

    wall 预算检查原先写在 `async for evt in runtime.run(child)` 循环体内。
    子 Agent 一旦卡在某个 await 上（上游 LLM "建连成功但不返回"、工具阻塞、
    审批等待），事件流就不再前进 → 检查永远不执行 → wall <= 300s 形同不存在，
    父 turn 跟着一起无限等。

所以这个定时器是"卡住的子 Agent 也必须给出结果"的最后保障，必须有直接测试盯着 ——
不能只靠端到端测试间接覆盖（它已经失效过一次，而且长期没有被发现）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent import subagent_runner as sr
from app.subagent import registry as reg


@pytest.mark.asyncio
async def test_wall_timer_cancels_run_without_any_event(monkeypatch):
    """到点必须发起取消 —— 完全不涉及任何事件。"""
    calls: list[tuple[str, str]] = []

    def _fake_cancel(run_id: str, reason: str) -> bool:
        calls.append((run_id, reason))
        return True

    monkeypatch.setattr(reg, "cancel", _fake_cancel)

    await sr._watch_wall_budget("rs_1", 0.05)

    assert calls == [("rs_1", "wall_timeout")]


@pytest.mark.asyncio
async def test_wall_timer_reason_is_wall_timeout(monkeypatch):
    """原因码必须是 wall_timeout：前端据此把"超时中止"与"用户停止"分开显示。"""
    seen: dict[str, str] = {}

    def _fake_cancel(run_id: str, reason: str) -> bool:
        seen["reason"] = reason
        return True

    monkeypatch.setattr(reg, "cancel", _fake_cancel)

    await sr._watch_wall_budget("rs_2", 0.02)

    assert seen["reason"] == "wall_timeout"


@pytest.mark.asyncio
async def test_wall_timer_is_cancellable_and_silent(monkeypatch):
    """正常结束时 runner 会取消它；被取消时不得触发任何取消动作。"""
    calls: list[tuple[str, str]] = []

    def _fake_cancel(run_id: str, reason: str) -> bool:
        calls.append((run_id, reason))
        return True

    monkeypatch.setattr(reg, "cancel", _fake_cancel)

    task = asyncio.create_task(sr._watch_wall_budget("rs_3", 30))
    await asyncio.sleep(0.02)
    task.cancel()
    await task  # 内部捕获 CancelledError 后正常返回，不向外抛

    assert calls == [], "被取消的定时器不该对 run 做任何事"


@pytest.mark.asyncio
async def test_wall_timer_ignores_already_finished_run(monkeypatch):
    """run 已结束（cancel 返回 False）时不该报错，也不该有副作用。"""

    def _fake_cancel(run_id: str, reason: str) -> bool:
        return False  # registry.cancel 对未知/已结束的 run 幂等返回 False

    monkeypatch.setattr(reg, "cancel", _fake_cancel)

    await sr._watch_wall_budget("rs_4", 0.02)  # 不应抛出
