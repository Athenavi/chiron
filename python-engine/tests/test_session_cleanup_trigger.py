"""N1：会话结束的**触发点**（评审 03 §1.2 / §10）。

缺口是这样的：`on_session_end` 的语义是**会话级**收尾（入队 L3 rollup + 丢 L1），但全仓只有
`runtime.py` 的 finally 一处调用它，而那条路径只在**异常/中断退出**时走到 —— 正常的会话结束
（用户关掉页面、长时间不活动）没有任何人通知引擎。于是：

* 会话级 rollup **永不入队**；
* L1 簿记在进程里一直涨（`start_periodic_cleanup` 压根没被启动过）。

修法就是补上触发点：L1 的周期清理本就要找出空闲会话，顺手通知即可。本文件钉住三件事 ——
**先通知再清理**、通知失败不拖垮清理、以及它真的会被启动（§装配）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from app.memory.service import MemoryService
from app.memory.session_meta import IDLE_TTL, SessionMetaStore
from tests.fakes import InMemoryProfileStore


def _expired(store: SessionMetaStore, session_id: str) -> None:
    """把某个会话的"最后活动时间"推到 TTL 之外。

    注意用 `store._store` 直取而**不是** `get()` / `get_all()`：后两者都带"顺手清理"副作用
    （`get` 命中过期会话会直接删掉它），那样就没法验证"回调时簿记还在"这个顺序了。
    """
    meta = store._store[session_id]
    meta.last_active_at = time.time() - (IDLE_TTL + 10)


def _seed(store: SessionMetaStore, session_id: str) -> None:
    store.create(session_id=session_id, tenant_id="t", user_id="u")


# ── 找出 ≠ 清理 ─────────────────────────────────────────────────────────


def test_expired_sessions_finds_without_removing():
    store = SessionMetaStore()
    _seed(store, "s1")
    _expired(store, "s1")

    found = store._expired_sessions()

    assert found == ["s1"]
    assert "s1" in store._store, "只找不移除 —— 通知方才有机会看到它"


def test_active_sessions_are_not_expired():
    store = SessionMetaStore()
    _seed(store, "s1")

    assert store._expired_sessions() == []


# ── 触发点：先通知、再清理 ──────────────────────────────────────────────


async def test_callback_runs_before_the_entry_is_removed():
    """**顺序**是这个修复的要点：簿记消失之后，就没人知道那些会话曾经存在过。"""
    store = SessionMetaStore()
    _seed(store, "s1")
    _expired(store, "s1")
    seen: list[tuple[str, bool]] = []

    async def on_expired(session_id: str) -> None:
        seen.append((session_id, session_id in store._store))

    await store.start_periodic_cleanup(interval=0.01, on_expired=on_expired)
    await asyncio.sleep(0.06)
    await store.stop_periodic_cleanup()

    assert seen == [("s1", True)], "回调时簿记必须还在"
    assert "s1" not in store._store, "通知完才清理"


async def test_callback_failure_does_not_block_cleanup():
    """一个会话的收尾失败，不该让整批空闲会话堆在内存里。"""
    store = SessionMetaStore()
    _seed(store, "s1")
    _expired(store, "s1")

    async def boom(_session_id: str) -> None:
        raise RuntimeError("rollup enqueue failed")

    await store.start_periodic_cleanup(interval=0.01, on_expired=boom)
    await asyncio.sleep(0.06)
    await store.stop_periodic_cleanup()

    assert "s1" not in store._store


async def test_cleanup_without_callback_still_works():
    """不接回调时行为与从前一致（只是清理）—— 这是一条向后兼容的保证。"""
    store = SessionMetaStore()
    _seed(store, "s1")
    _expired(store, "s1")

    await store.start_periodic_cleanup(interval=0.01)
    await asyncio.sleep(0.06)
    await store.stop_periodic_cleanup()

    assert "s1" not in store._store


async def test_second_start_is_ignored():
    """周期任务只能有一个（重复启动会把同一批会话通知两遍）。"""
    store = SessionMetaStore()

    await store.start_periodic_cleanup(interval=1)
    task = store._cleanup_task
    await store.start_periodic_cleanup(interval=1)

    assert store._cleanup_task is task
    await store.stop_periodic_cleanup()


async def test_stop_is_idempotent():
    store = SessionMetaStore()
    await store.start_periodic_cleanup(interval=1)

    await store.stop_periodic_cleanup()
    await store.stop_periodic_cleanup()  # 不该抛


# ── 记忆服务侧的委托 ────────────────────────────────────────────────────


async def test_memory_service_delegates_stop():
    store = SessionMetaStore()
    svc = MemoryService(store=InMemoryProfileStore(), session_meta_store=store)
    await store.start_periodic_cleanup(interval=1)
    task = store._cleanup_task  # stop 之后引用会被置回 None，所以先抓住

    await svc.stop_periodic_session_cleanup()

    assert task is not None and (task.cancelled() or task.done())


async def test_memory_service_without_session_meta_is_safe():
    svc = MemoryService(store=InMemoryProfileStore())

    await svc.stop_periodic_session_cleanup()  # 不该抛


# ── 装配：它真的会被启动 ────────────────────────────────────────────────


def test_assembly_starts_the_cleanup_task():
    """`start_periodic_cleanup` 此前**从未被调用** —— 这条断言防它再退回那种状态。

    这里读源码而不是起 lifespan：起一次完整 app 需要 PG/Redis，代价远大于这条断言的价值；
    而"有没有接线"恰好是静态可判的。
    """
    src = (
        Path(__file__).resolve().parents[1] / "app" / "main.py"
    ).read_text(encoding="utf-8")

    assert "start_periodic_cleanup(on_expired=" in src, (
        "L1 清理任务没被启动 ⇒ 会话级 rollup 永不入队（N1 回归）"
    )
    assert "stop_periodic_session_cleanup" in src, "停机时也要停它"
