"""history_writer 的边界行为。

这些断言守的是**安全与可用性**，不是功能完整性：
租户越界必须拒绝、隐私模式必须不落盘、角色过滤必须防重复写、
tool 消息必须带 tool_call_id 否则读回来会被当孤儿删掉。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent import history_writer


class _Row(dict):
    """模拟 asyncpg 的 Record：同时支持字符串键与整数位置索引。

    `load_messages` 两种都用（`row["role"]` 与 `row[2]`），fake 必须一起支持，
    否则测的是"fake 的形状"而不是被测代码。
    """

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class _FakeConn:
    def __init__(self, sink: list[tuple]) -> None:
        self.sink = sink

    def transaction(self) -> _FakeTx:
        # 事务开在**连接**上（`async with conn.transaction()`），不是池上。
        return _FakeTx()

    async def execute(self, sql: str, *args: Any) -> None:
        self.sink.append(args)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    """只够 persist_messages 用：fetchrow（归属）、fetch（去重）、acquire（写）。"""

    def __init__(self, owned: bool = True, existing_ids: list[str] | None = None) -> None:
        self.owned = owned
        self.existing_ids = existing_ids or []
        self.inserted: list[tuple] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        return {"ok": 1} if self.owned else None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [{"tool_call_id": i} for i in self.existing_ids]

    def acquire(self) -> _FakeConn:
        return _FakeConn(self.inserted)

    def transaction(self) -> _FakeTx:
        return _FakeTx()


@pytest.fixture
def no_privacy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "app.middleware.privacy_middleware.is_no_retention", lambda: False, raising=False
    )


def _patch_pool(monkeypatch: pytest.MonkeyPatch, pool: Any) -> None:
    import app.db

    monkeypatch.setattr(app.db, "get_pool", lambda: pool, raising=False)


def test_tenant_id_is_coerced_not_raised():
    """`tenant_id` 在构造路径上可能是任意值；这里抛错会打挂整轮 run。"""
    assert history_writer._coerce_tenant(1) == 1
    assert history_writer._coerce_tenant("2") == 2
    assert history_writer._coerce_tenant(" 3 ") == 3
    for bad in ("t", "", None, 0, -1, True, [], {}):
        assert history_writer._coerce_tenant(bad) == 0


class TestTenantBoundary:
    @pytest.mark.asyncio
    async def test_refuses_write_when_session_not_owned(
        self, monkeypatch: pytest.MonkeyPatch, no_privacy
    ):
        """跨租户写入必须 fail-closed —— 这是引擎直连 PG 时唯一的边界。"""
        pool = _FakePool(owned=False)
        _patch_pool(monkeypatch, pool)

        written = await history_writer.persist_messages(
            "s1", 1, [{"role": "tool", "content": "x", "tool_call_id": "c1"}], roles={"tool"}
        )

        assert written == 0
        assert pool.inserted == [], "归属校验失败时不得写入任何行"

    @pytest.mark.asyncio
    async def test_refuses_load_when_session_not_owned(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _patch_pool(monkeypatch, _FakePool(owned=False))
        assert await history_writer.load_messages("s1", 1) == []

    @pytest.mark.asyncio
    async def test_invalid_tenant_skips_without_touching_db(
        self, monkeypatch: pytest.MonkeyPatch, no_privacy
    ):
        pool = _FakePool()
        _patch_pool(monkeypatch, pool)

        assert await history_writer.persist_messages("s1", "t", [{"role": "tool", "content": "x"}]) == 0
        assert pool.inserted == []


class TestPrivacyMode:
    @pytest.mark.asyncio
    async def test_no_retention_never_persists(self, monkeypatch: pytest.MonkeyPatch):
        """隐私模式是合规要求，不是性能开关。"""
        pool = _FakePool()
        _patch_pool(monkeypatch, pool)
        monkeypatch.setattr(
            "app.middleware.privacy_middleware.is_no_retention", lambda: True, raising=False
        )

        written = await history_writer.persist_messages(
            "s1", 1, [{"role": "tool", "content": "secret"}], roles={"tool"}
        )

        assert written == 0
        assert pool.inserted == [], "no_retention 下不得落盘"


class TestRoleFilter:
    @pytest.mark.asyncio
    async def test_roles_filter_prevents_double_write(
        self, monkeypatch: pytest.MonkeyPatch, no_privacy
    ):
        """user/assistant 由 Go 写；引擎再写一遍会产生重复行。"""
        pool = _FakePool()
        _patch_pool(monkeypatch, pool)

        written = await history_writer.persist_messages(
            "s1",
            1,
            [
                {"role": "user", "content": "问"},
                {"role": "assistant", "content": "答"},
                {"role": "tool", "content": "结果", "tool_call_id": "c1"},
            ],
            roles={"tool"},
        )

        assert written == 1
        assert len(pool.inserted) == 1
        assert pool.inserted[0][1] == "tool", "只应写入 tool 角色"

    @pytest.mark.asyncio
    async def test_system_messages_are_never_persisted(
        self, monkeypatch: pytest.MonkeyPatch, no_privacy
    ):
        """system 段每轮重建（技能目录/记忆都会变），存下来只会互相矛盾。"""
        pool = _FakePool()
        _patch_pool(monkeypatch, pool)

        written = await history_writer.persist_messages(
            "s1",
            1,
            [{"role": "system", "content": "旧提示词"}, {"role": "tool", "content": "r", "tool_call_id": "c1"}],
        )

        assert written == 1
        assert all(row[1] != "system" for row in pool.inserted)


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_existing_tool_call_ids_are_skipped(
        self, monkeypatch: pytest.MonkeyPatch, no_privacy
    ):
        """overflow 重试会重复走收尾路径；重复插入会让同一个结果出现两次。"""
        pool = _FakePool(existing_ids=["c1"])
        _patch_pool(monkeypatch, pool)

        written = await history_writer.persist_messages(
            "s1",
            1,
            [
                {"role": "tool", "content": "重复", "tool_call_id": "c1"},
                {"role": "tool", "content": "新的", "tool_call_id": "c2"},
            ],
            roles={"tool"},
        )

        assert written == 1
        assert pool.inserted[0][4] == "c2", "应只写入未落库的那一条"


class TestToolCallIdRoundTrip:
    @pytest.mark.asyncio
    async def test_tool_call_id_is_written_and_read_back(
        self, monkeypatch: pytest.MonkeyPatch, no_privacy
    ):
        """缺了 tool_call_id，读回来的 tool 消息会被 _ensure_valid_tool_sequence 当孤儿删掉。"""
        pool = _FakePool()
        _patch_pool(monkeypatch, pool)

        await history_writer.persist_messages(
            "s1", 1, [{"role": "tool", "content": "out", "tool_call_id": "call_1"}], roles={"tool"}
        )

        assert pool.inserted[0][4] == "call_1"

    @pytest.mark.asyncio
    async def test_load_preserves_tool_call_id_and_order(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        rows = [
            _Row({"role": "tool", "content": "b", "tool_calls": "", "turn_id": "", "tool_call_id": "c2"}),
            _Row({"role": "user", "content": "a", "tool_calls": "", "turn_id": "t1", "tool_call_id": ""}),
        ]

        class _LoadPool(_FakePool):
            async def fetch(self, sql: str, *args: Any):
                return rows

        _patch_pool(monkeypatch, _LoadPool())

        out = await history_writer.load_messages("s1", 1)

        assert [m["role"] for m in out] == ["user", "tool"], "应按时间升序返回"
        assert out[1]["tool_call_id"] == "c2"
        assert out[0]["turn_id"] == "t1"


class TestStoredToolCallsNormalization:
    """Go 写进 `messages.tool_calls` 的是 `[]string`，引擎侧全按对象取值。

    `submit_handler.go:757` 是 `json.Marshal(turnToolCallIDs)`，落库形如
    `["call_1"]`；而 `message_codec.make_message` 与
    `compaction._ensure_valid_tool_sequence` 都调 `tc.get("id")`。
    对字符串调 `.get()` 抛 `AttributeError` —— 读一次历史就能打挂整轮对话。
    """

    def test_string_ids_become_objects(self):
        out = history_writer._normalize_stored_tool_calls(["call_1", "call_2"])

        assert out == [
            {"id": "call_1", "name": "", "arguments": ""},
            {"id": "call_2", "name": "", "arguments": ""},
        ]

    def test_every_item_supports_get(self):
        """这条断言直接对着崩溃点：归一后必须能安全 `.get("id")`。"""
        out = history_writer._normalize_stored_tool_calls(["call_1", {"id": "call_2"}])

        assert [tc.get("id") for tc in out] == ["call_1", "call_2"]

    def test_object_form_is_preserved(self):
        out = history_writer._normalize_stored_tool_calls(
            [{"id": "c1", "name": "read_file", "arguments": '{"path":"a"}'}]
        )

        assert out == [{"id": "c1", "name": "read_file", "arguments": '{"path":"a"}'}]

    def test_openai_wrapped_form_is_flattened(self):
        out = history_writer._normalize_stored_tool_calls(
            [{"id": "c1", "type": "function", "function": {"name": "shell_exec", "arguments": "{}"}}]
        )

        assert out == [{"id": "c1", "name": "shell_exec", "arguments": "{}"}]

    def test_garbage_is_dropped_not_crashing(self):
        out = history_writer._normalize_stored_tool_calls(["", None, 42, {}, {"no_id": 1}])

        assert out == [], "认不出的条目直接丢弃，不得抛错"

    @pytest.mark.asyncio
    async def test_load_normalizes_and_backfills_names(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """端到端：库里是 id 数组，读出来必须带名字（来自 tool_calls 表）。"""
        rows = [
            _Row(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": '["call_1"]',
                    "turn_id": "",
                    "tool_call_id": "",
                }
            )
        ]

        class _Pool(_FakePool):
            async def fetch(self, sql: str, *args: Any):
                if "FROM tool_calls" in sql:
                    return [_Row({"id": "call_1", "tool_name": "read_file", "input": '{"path":"x"}'})]
                return rows

        _patch_pool(monkeypatch, _Pool())

        out = await history_writer.load_messages("s1", 1)

        assert out[0]["tool_calls"] == [
            {"id": "call_1", "name": "read_file", "arguments": '{"path":"x"}'}
        ]
class TestRecallByMessageId:
    """`read_archive` 的底层通道：按 messages.id 取原文，必须带会话归属校验。"""

    @pytest.mark.asyncio
    async def test_refuses_when_session_not_owned(self, monkeypatch):
        _patch_pool(monkeypatch, _FakePool(owned=False))
        got = await history_writer.load_message_content("s1", 1, "m1")
        assert got is None, "跨租户按 id 取原文必须 fail-closed"

    @pytest.mark.asyncio
    async def test_requires_message_id(self, monkeypatch):
        _patch_pool(monkeypatch, _FakePool())
        assert await history_writer.load_message_content("s1", 1, "") is None

    @pytest.mark.asyncio
    async def test_bad_tenant_returns_none(self, monkeypatch):
        _patch_pool(monkeypatch, _FakePool())
        assert await history_writer.load_message_content("s1", "t", "m1") is None
