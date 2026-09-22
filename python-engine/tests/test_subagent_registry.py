"""子 Agent 运行时治理（registry）：取消语义、看门狗阈值、会话维度隔离。

重点不是覆盖率，而是把**几条硬约定**钉住：
  1. 取消幂等：未知/已结束的 run 返回 False，不抛错；
  2. 看门狗只打"真的卡住"的 run —— 还在产出事件的长任务不能被误杀；
  3. 会话级取消不越界（另一个会话的 run 不受影响）；
  4. 反注册要同时清掉会话索引，不能留死引用。
"""
import asyncio
import time

from app.subagent import registry


def _pending_task() -> asyncio.Task:
    async def _forever() -> None:
        await asyncio.sleep(3600)

    return asyncio.create_task(_forever())


def _reset() -> None:
    registry._RUNS.clear()
    registry._BY_SESSION.clear()


def test_未知_run_取消返回_False_且幂等():
    assert registry.cancel("rs_missing", registry.REASON_USER) is False
    assert registry.cancel("", registry.REASON_USER) is False


def test_注册后取消_并记录原因():
    async def _run() -> None:
        task = _pending_task()
        registry.register("rs_1", task, session_id="s1", tenant_id="t1", profile="reviewer")
        assert registry.cancel("rs_1", registry.REASON_USER) is True
        assert registry.reason_of("rs_1") == registry.REASON_USER
        # 反注册后不可再取消（幂等），且会话索引被清干净
        registry.unregister("rs_1")
        assert registry.cancel("rs_1", registry.REASON_USER) is False
        assert registry._BY_SESSION == {}
        task.cancel()
        _reset()

    asyncio.run(_run())


def test_会话级取消不越界():
    async def _run() -> None:
        a, b = _pending_task(), _pending_task()
        registry.register("rs_a", a, session_id="s1")
        registry.register("rs_b", b, session_id="s2")
        assert registry.cancel_session("s1", registry.REASON_SESSION) == 1
        assert registry.reason_of("rs_a") == registry.REASON_SESSION
        assert registry.reason_of("rs_b") == ""
        a.cancel()
        b.cancel()
        _reset()

    asyncio.run(_run())


def test_touch_刷新心跳():
    async def _run() -> None:
        task = _pending_task()
        registry.register("rs_touch", task, session_id="s1")
        registry._RUNS["rs_touch"].last_event_at = time.time() - 999
        registry.touch("rs_touch")
        assert time.time() - registry._RUNS["rs_touch"].last_event_at < 1
        task.cancel()
        _reset()

    asyncio.run(_run())


def test_看门狗_空闲与超时长分别命中():
    async def _run() -> None:
        idle, long_running = _pending_task(), _pending_task()
        registry.register("rs_idle", idle, session_id="s1")
        registry.register("rs_long", long_running, session_id="s1")
        now = time.time()
        registry._RUNS["rs_idle"].last_event_at = now - 400      # 400s 没有事件
        registry._RUNS["rs_long"].started_at = now - 2000        # 已跑 2000s
        registry._RUNS["rs_long"].last_event_at = now            # 但刚有事件（不该判空闲）

        fired = await registry.watchdog_tick(idle_timeout=300, max_runtime=1800, now=now)

        assert set(fired) == {"rs_idle", "rs_long"}
        assert registry.reason_of("rs_idle") == registry.REASON_IDLE
        assert registry.reason_of("rs_long") == registry.REASON_MAX_RUNTIME
        idle.cancel()
        long_running.cancel()
        _reset()

    asyncio.run(_run())


def test_看门狗不误杀_仍在产出事件的_run():
    async def _run() -> None:
        task = _pending_task()
        registry.register("rs_healthy", task, session_id="s1")
        fired = await registry.watchdog_tick(idle_timeout=300, max_runtime=1800)
        assert fired == []
        assert registry.reason_of("rs_healthy") == ""
        task.cancel()
        _reset()

    asyncio.run(_run())


def test_阈值关闭时不动作():
    async def _run() -> None:
        task = _pending_task()
        registry.register("rs_keep", task, session_id="s1")
        registry._RUNS["rs_keep"].last_event_at = time.time() - 99999
        assert await registry.watchdog_tick(idle_timeout=0, max_runtime=0) == []
        task.cancel()
        _reset()

    asyncio.run(_run())


def test_list_active_可按会话过滤():
    async def _run() -> None:
        a, b = _pending_task(), _pending_task()
        registry.register("rs_x", a, session_id="s1", profile="p1")
        registry.register("rs_y", b, session_id="s2")
        assert [r["run_id"] for r in registry.list_active("s1")] == ["rs_x"]
        assert len(registry.list_active()) == 2
        a.cancel()
        b.cancel()
        _reset()

    asyncio.run(_run())
