"""L3 巩固的**生产侧**节拍（`MemoryService.on_turn_complete` / `on_session_end`）。

装配接通之后，剩下的问题全是节拍：什么时候值得投一次巩固任务。此前 `producer` 从未被注入
`MemoryService`，全库唯一的 `memory_consolidate` 生产者一次也没跑过 —— 所以这一组用例钉的
是"该投的时候投了"，而不只是"投了的东西格式对"。
"""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.memory.service import MemoryService
from app.memory.session_meta import SessionMetaStore
from tests.fakes import InMemoryProfileStore


class _FakeProducer:
    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raises = raises

    async def enqueue(self, **kwargs: Any) -> None:
        if self._raises:
            raise RuntimeError("redis is down")
        self.calls.append(kwargs)

    def types(self) -> list[str]:
        return [c["task_type"] for c in self.calls]

    def of(self, task_type: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["task_type"] == task_type]


def _svc(producer: _FakeProducer | None) -> MemoryService:
    return MemoryService(
        store=InMemoryProfileStore(),
        session_meta_store=SessionMetaStore(),
        producer=producer,
    )


async def _run_turns(svc: MemoryService, session_id: str, n: int) -> None:
    for _ in range(n):
        await svc.on_turn_complete(session_id)


class TestBatching:
    """`memory_consolidate_batch` 从此是节拍本身，而不是一条读不到的配置。"""

    async def test_enqueue_happens_only_on_the_batch_boundary(self, monkeypatch):
        monkeypatch.setattr(settings, "memory_consolidate_batch", 3)
        producer = _FakeProducer()
        svc = _svc(producer)
        await svc.on_session_start("s1", "t1", "u1")

        await _run_turns(svc, "s1", 5)

        assert producer.types() == ["memory_consolidate"], "5 个回合里只有第 3 个到节拍"

    async def test_batch_of_one_means_every_turn(self, monkeypatch):
        """`<=1` 是"不攒批"的意思，不是"关掉"——关掉请用 producer=None。"""
        monkeypatch.setattr(settings, "memory_consolidate_batch", 1)
        producer = _FakeProducer()
        svc = _svc(producer)
        await svc.on_session_start("s1", "t1", "u1")

        await _run_turns(svc, "s1", 3)

        assert len(producer.of("memory_consolidate")) == 3

    async def test_rollup_covers_the_tail(self, monkeypatch):
        """不足一批就结束的会话仍然要被巩固：rollup 取的是整段会话。"""
        monkeypatch.setattr(settings, "memory_consolidate_batch", 32)
        producer = _FakeProducer()
        svc = _svc(producer)
        await svc.on_session_start("s1", "t1", "u1")

        await _run_turns(svc, "s1", 2)
        await svc.on_session_end("s1")

        assert producer.of("memory_consolidate") == []
        assert len(producer.of("memory_rollup")) == 1

    async def test_consolidate_payload_is_what_the_worker_reads(self, monkeypatch):
        """信封形状由 worker 的 handler 决定：`turn_count` 是它记 lineage 用的区间上界。"""
        monkeypatch.setattr(settings, "memory_consolidate_batch", 1)
        producer = _FakeProducer()
        svc = _svc(producer)
        await svc.on_session_start("s1", "t1", "u1")
        await svc.on_turn_complete("s1")

        task = producer.calls[0]
        assert task["tenant_id"] == "t1"
        assert task["payload"] == {
            "session_id": "s1",
            "user_id": "u1",
            "turn_count": 1,
            "trigger": "turn_complete",
        }

    async def test_rollup_is_higher_priority_than_a_turn(self, monkeypatch):
        """会话只结束一次，而巩固会反复投；结束时的那一条应该先被拿走。"""
        monkeypatch.setattr(settings, "memory_consolidate_batch", 1)
        producer = _FakeProducer()
        svc = _svc(producer)
        await svc.on_session_start("s1", "t1", "u1")
        await svc.on_turn_complete("s1")
        await svc.on_session_end("s1")

        consolidate = producer.of("memory_consolidate")[0]
        rollup = producer.of("memory_rollup")[0]
        assert rollup["priority"] > consolidate["priority"]


class TestNoProducer:
    """没有队列时这一路必须**安静但不装死**。"""

    async def test_turn_completion_does_not_explode_without_a_producer(self):
        svc = _svc(None)
        await svc.on_session_start("s1", "t1", "u1")

        await _run_turns(svc, "s1", 3)
        await svc.on_session_end("s1")

    async def test_other_l1_work_still_happens_without_a_producer(self):
        """入队只是 L1 钩子顺带做的事：没有队列，记账与清理照常。"""
        svc = _svc(None)
        await svc.on_session_start("s1", "t1", "u1")

        await svc.on_turn_complete("s1")
        meta = svc._session_meta.get("s1")

        assert meta is not None and meta.turn_count == 1

        await svc.on_session_end("s1")
        assert svc._session_meta.get("s1") is None


class TestProducerFailure:
    async def test_enqueue_failure_is_not_a_turn_failure(self, caplog, monkeypatch):
        """Redis 抖动不该让用户的这一回合失败 —— 记忆是顺带做的，但要说出来。"""
        monkeypatch.setattr(settings, "memory_consolidate_batch", 1)
        producer = _FakeProducer(raises=True)
        svc = _svc(producer)
        await svc.on_session_start("s1", "t1", "u1")

        with caplog.at_level("WARNING", logger="app.memory.service"):
            await svc.on_turn_complete("s1")

        assert "Failed to enqueue consolidate task" in caplog.text

    async def test_session_end_still_forgets_the_session_when_enqueue_fails(self):
        producer = _FakeProducer(raises=True)
        svc = _svc(producer)
        await svc.on_session_start("s1", "t1", "u1")

        await svc.on_session_end("s1")

        assert svc._session_meta.get("s1") is None
