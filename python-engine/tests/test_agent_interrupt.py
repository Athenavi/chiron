"""真取消：引擎侧的中断信号 + run 循环的响应 + 控制端点的校验。

这是 ACP `session/cancel` 的前置 —— "假取消"（客户端停止读 SSE）会让引擎继续烧 token、继续写
文件。本文件钉住三件事：

1. **信号语义**：本地快路径 + Redis 跨副本；**clear 之后绝不残留**（残留会误伤下一次会话）；
2. **run 的响应**：在**轮次边界**停下，发 `cancelled` **再补 `done`**（只发 cancelled 会让下游
   SSE 挂住），且**不再调用模型**；
3. **端点校验**：会话归属 + 调用者身份 + run token —— 与 approval / answer 同一套。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.agent import interrupt
from app.agent.interrupt import (
    clear_interrupt,
    is_interrupted,
    request_interrupt,
    request_interrupt_local,
)
from app.agent.runtime import AgentRuntime, AgentTask
from app.gateway.provider import ChatResponse


@pytest.fixture(autouse=True)
def _clean_local_flags():
    """每个用例前后都清干净本地标志（它是模块级状态，跨用例会串）。"""
    interrupt._LOCAL.clear()
    yield
    interrupt._LOCAL.clear()


class _FakeRedis:
    """最小 Redis 替身：只要 `setex` / `get` / `delete` 三个方法。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.fail = False

    async def setex(self, key: str, _ttl: int, value: str) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        self.store[key] = value

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(key)

    async def delete(self, key: str) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        self.store.pop(key, None)


# ── 信号语义 ────────────────────────────────────────────────────────────


async def test_local_flag_works_without_redis():
    """没有 Redis 也要能用：同副本的 run 靠本地标志就能停下。"""
    assert await is_interrupted("s1", redis=None) is False

    request_interrupt_local("s1")

    assert await is_interrupted("s1", redis=None) is True


async def test_redis_flag_is_read_across_replicas():
    """run 在别的副本时，只有 Redis 里的信号能被读到（本地标志是空的）。"""
    redis = _FakeRedis()

    assert await request_interrupt("s1", redis=redis) is True
    interrupt._LOCAL.clear()  # 模拟"这个副本上没有该 run 的本地标志"

    assert await is_interrupted("s1", redis=redis) is True


async def test_clear_removes_both_local_and_redis():
    """**不清会误伤下一次会话** —— 同一 session 的下一次 run 会一上来就被取消。"""
    redis = _FakeRedis()
    await request_interrupt("s1", redis=redis)

    await clear_interrupt("s1", redis=redis)

    assert await is_interrupted("s1", redis=redis) is False
    assert redis.store == {}


async def test_redis_failure_does_not_break_local_path():
    """Redis 挂了也要能取消同副本的 run，且读取时不抛错（当没被取消处理）。"""
    redis = _FakeRedis()
    redis.fail = True

    assert await request_interrupt("s1", redis=redis) is False  # 没置上 Redis
    assert await is_interrupted("s1", redis=redis) is True  # 本地仍生效
    await clear_interrupt("s1", redis=redis)  # 不该抛


async def test_empty_session_id_is_ignored():
    assert await request_interrupt("", redis=_FakeRedis()) is False
    assert await is_interrupted("", redis=_FakeRedis()) is False


# ── run 循环的响应 ──────────────────────────────────────────────────────


async def _run_with_interrupt(session_id: str = "s1") -> tuple[list[Any], list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    gateway = MagicMock()

    async def fake_stream(**kwargs: Any):
        calls.append(kwargs)
        yield ChatResponse(content="ok", finish_reason="stop")

    gateway.chat_stream = fake_stream
    runtime = AgentRuntime(gateway=gateway)
    task = AgentTask(
        id="t1",
        tenant_id="t",
        user_id="u",
        session_id=session_id,
        content="hi",
        system_prompt="base",
        llm_config={"mode": "normal"},
        max_turns=3,
    )
    request_interrupt_local(session_id)
    events = [event async for event in runtime.run(task)]
    return events, calls


async def test_run_stops_at_turn_boundary_and_emits_cancelled_then_done():
    events, calls = await _run_with_interrupt()

    types = [e.type for e in events]
    assert "cancelled" in types, "要明确告诉下游这是取消，不是失败"
    assert types[-1] == "done", "只发 cancelled 会让下游的 SSE 挂住"
    assert calls == [], "取消就该在轮次边界停住，不该再调模型"


async def test_interrupt_signal_is_cleared_after_the_run():
    """run 结束（含被取消）后信号必须清掉，否则下一次会话一上来就被取消。"""
    await _run_with_interrupt("s-clear")

    assert await is_interrupted("s-clear", redis=None) is False


async def test_run_without_interrupt_proceeds_normally():
    calls: list[dict[str, Any]] = []
    gateway = MagicMock()

    async def fake_stream(**kwargs: Any):
        calls.append(kwargs)
        yield ChatResponse(content="ok", finish_reason="stop")

    gateway.chat_stream = fake_stream
    runtime = AgentRuntime(gateway=gateway)
    task = AgentTask(
        id="t1",
        tenant_id="t",
        user_id="u",
        session_id="s-normal",
        content="hi",
        system_prompt="base",
        llm_config={"mode": "normal"},
        max_turns=1,
    )
    events = [event async for event in runtime.run(task)]

    assert calls, "没有取消信号时应当正常调用模型"
    assert "cancelled" not in [e.type for e in events]


# ── 控制端点的校验 ──────────────────────────────────────────────────────


async def _post_interrupt(body: dict[str, Any], *, caller: str = "") -> dict[str, Any]:
    import httpx
    from httpx import ASGITransport

    import app.main as main

    headers = {"x-user-id": caller} if caller else {}
    async with httpx.AsyncClient(
        transport=ASGITransport(app=main.create_app()), base_url="http://t", headers=headers
    ) as client:
        resp = await client.post("/v1/agent/interrupt", json=body)
        return resp.json()


async def test_endpoint_requires_session_id():
    out = await _post_interrupt({})

    assert out["ok"] is False and "session_id" in out["error"]


async def test_endpoint_rejects_non_owner(monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main, "_ACTIVE_RUNTIMES", {"s1": (MagicMock(), "owner-u", "tok")}, raising=False
    )

    out = await _post_interrupt({"session_id": "s1"}, caller="someone-else")

    assert out["ok"] is False and "owner" in out["error"]


async def test_endpoint_sets_signal_for_owner(monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main, "_ACTIVE_RUNTIMES", {"s2": (MagicMock(), "owner-u", "tok")}, raising=False
    )
    faked = _FakeRedis()
    monkeypatch.setattr(main, "_redis", faked, raising=False)

    out = await _post_interrupt({"session_id": "s2"}, caller="owner-u")

    assert out["ok"] is True
    assert await is_interrupted("s2", redis=faked) is True


async def test_endpoint_rejects_stale_run_token(monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main, "_ACTIVE_RUNTIMES", {"s3": (MagicMock(), "owner-u", "tok-new")}, raising=False
    )

    out = await _post_interrupt(
        {"session_id": "s3", "run_token": "tok-old"}, caller="owner-u"
    )

    assert out["ok"] is False and "run token" in out["error"]


async def test_endpoint_falls_back_to_run_registry_when_run_is_elsewhere(monkeypatch):
    """本副本没有该 run（在别的副本）时也要能置信号 —— 这才是跨副本取消。"""
    import app.main as main

    monkeypatch.setattr(main, "_ACTIVE_RUNTIMES", {}, raising=False)
    faked = _FakeRedis()
    monkeypatch.setattr(main, "_redis", faked, raising=False)

    async def fake_owner_of(_redis: Any, _session_id: str) -> dict[str, Any]:
        return {"instance_id": "other", "owner_uid": "owner-u", "run_token": "tok"}

    monkeypatch.setattr("app.run_registry.owner_of", fake_owner_of)

    out = await _post_interrupt({"session_id": "s4"}, caller="owner-u")

    assert out["ok"] is True
    assert await is_interrupted("s4", redis=faked) is True


async def test_endpoint_reports_no_active_run(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_ACTIVE_RUNTIMES", {}, raising=False)

    async def fake_owner_of(_redis: Any, _session_id: str) -> None:
        return None

    monkeypatch.setattr("app.run_registry.owner_of", fake_owner_of)

    out = await _post_interrupt({"session_id": "s5"}, caller="owner-u")

    assert out["ok"] is False and "no active agent" in out["error"]
