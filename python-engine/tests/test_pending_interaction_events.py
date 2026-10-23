"""M21：待办通知的两半 —— 落票时的事件广播与**跨会话**汇总列表。

与 `test_pending_interactions.py` 的分工：那个文件钉"票本身"的语义（唯一索引、身份谓词、
单次消费、失败降级），这里钉 M21 新增的两件事：

1. `create` 落票后广播一条 `interaction_pending` —— **失败绝不影响落票**，且事件里
   **不带审批参数**（它会经 Redis 与 SSE 到浏览器，而 `arguments` 可能含敏感内容）；
2. `list_open_for_user` 的身份谓词与按会话那条**同源**：只放宽 `session_id` 作用域，
   不放宽 `tenant_id` + `user_id`（少了任何一条，这个端点就成了"列出所有人的待办"）。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from app.agent import interactions as ix


class _FakePool:
    """替身池：只记录 SQL 与参数，按 `row` / `rows` 模拟 asyncpg 返回。"""

    def __init__(
        self,
        *,
        row: dict[str, Any] | None = None,
        rows: list[dict[str, Any]] | None = None,
        status: str = "UPDATE 1",
        raise_on: str = "",
    ) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.row = row
        self.rows = rows or []
        self.status = status
        self.raise_on = raise_on

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((sql, args))
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError("db down")
        return self.row

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError("db down")
        return self.status

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError("db down")
        return self.rows

    @property
    def last_sql(self) -> str:
        return self.calls[-1][0]

    @property
    def last_args(self) -> tuple[Any, ...]:
        return self.calls[-1][1]


class _FakeRedis:
    def __init__(self, *, raise_on_publish: bool = False) -> None:
        self.published: list[tuple[str, str]] = []
        self.raise_on_publish = raise_on_publish

    async def publish(self, channel: str, payload: str) -> int:
        if self.raise_on_publish:
            raise RuntimeError("redis down")
        self.published.append((channel, payload))
        return 1


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> _FakePool:
    fake = _FakePool()
    monkeypatch.setattr(ix, "_pool", lambda: fake)
    return fake


def _patch_redis(monkeypatch: pytest.MonkeyPatch, fake: _FakeRedis) -> None:
    import app.redis_client as redis_client

    async def _get_redis() -> Any:
        return fake

    monkeypatch.setattr(redis_client, "get_redis", _get_redis)


def _norm(sql: str) -> str:
    return " ".join(sql.split())


async def _create(**overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "tenant_id": "t1",
        "user_id": "u1",
        "session_id": "s1",
        "kind": ix.KIND_APPROVAL,
        "tool_call_id": "tc1",
        "tool_name": "shell",
        "args_hash": "h1",
        "request": {"question": "请求执行 shell"},
    }
    kwargs.update(overrides)
    return await ix.create(**kwargs)


# ── 落票时的事件广播 ──────────────────────────────────────────────────────


async def test_create_publishes_a_pending_event(
    pool: _FakePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool.row = {"id": "int-9"}
    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)

    got = await _create(request={"question": "是否执行 shell？", "arguments": {"cmd": "rm -rf /"}})

    assert got == "int-9"
    assert len(fake.published) == 1, "落票后必须广播一条事件"
    channel, payload = fake.published[0]
    assert channel.endswith(ix.INTERACTION_EVENTS_CHANNEL)
    data = json.loads(payload)
    assert data["type"] == "interaction_pending"
    assert data["interaction_id"] == "int-9"
    assert data["session_id"] == "s1"
    assert data["user_id"] == "u1"
    assert data["kind"] == ix.KIND_APPROVAL
    assert data["title"] == "是否执行 shell？"
    # 安全：审批参数不得随事件外泄（这条事件会经 Redis 与 SSE 到浏览器）。
    assert "rm -rf" not in payload
    assert "arguments" not in data


async def test_publish_failure_still_returns_the_ticket_id(
    pool: _FakePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """广播只是"更快被看见"，票已经在 PG 里 —— 推送失败绝不能把落票也拖失败。"""
    pool.row = {"id": "int-10"}
    _patch_redis(monkeypatch, _FakeRedis(raise_on_publish=True))

    assert await _create() == "int-10"


async def test_no_publish_without_a_user_id(
    pool: _FakePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有 user_id 就没有用户通道可路由：宁可少一条推送，也不发一条无主事件。"""
    pool.row = {"id": "int-11"}
    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)

    assert await _create(user_id="") == "int-11"
    assert fake.published == []


async def test_no_publish_when_redis_is_unavailable(
    pool: _FakePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool.row = {"id": "int-12"}
    _patch_redis(monkeypatch, None)  # type: ignore[arg-type]

    assert await _create() == "int-12"


# ── 跨会话汇总列表 ────────────────────────────────────────────────────────


async def test_list_open_for_user_keeps_the_identity_predicate(pool: _FakePool) -> None:
    pool.rows = [
        {
            "id": "i1",
            "session_id": "s9",
            "kind": "approval",
            "status": "pending",
            "request": "{}",
            "decision": None,
        }
    ]

    out = await ix.list_open_for_user(tenant_id=1, user_id="u1", limit=5)

    norm = _norm(pool.last_sql)
    assert "WHERE user_id = $1" in norm
    assert "tenant_id = $2" in norm
    # 跨会话 = **只**去掉会话那一维；身份谓词一字不改。
    assert "session_id = $" not in norm
    assert "status IN ('pending', 'answered')" in norm
    assert pool.last_args == ("u1", 1, 5)
    assert out and out[0]["session_id"] == "s9", "跨会话列表必须带 session_id（前端靠它跳转）"


async def test_list_open_for_user_without_identity_does_not_query(pool: _FakePool) -> None:
    assert await ix.list_open_for_user(tenant_id=1, user_id="") == []
    assert pool.calls == [], "身份不明时不该碰库"


async def test_list_open_for_user_degrades_to_empty_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ix, "_pool", lambda: _FakePool(raise_on="FROM pending_interactions"))

    assert await ix.list_open_for_user(tenant_id="t1", user_id="u1") == []
