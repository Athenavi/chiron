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
    def __init__(
        self, sink: list[tuple], insert_rowcount: int = 1, sqls: list[str] | None = None
    ) -> None:
        self.sink = sink
        # 同时记下语句本身：断言若靠"参数个数"区分 messages 与 archive 行，
        # 每加一列就会让不相干的用例误报 —— 按 SQL 内容认行才稳。
        self.sqls = sqls if sqls is not None else []
        # `ON CONFLICT DO NOTHING` 命中时 asyncpg 返回 `INSERT 0 0`；fake 必须
        # 回一个状态串，否则被测代码解析不出影响行数、会把每条都当冲突。
        self.insert_rowcount = insert_rowcount

    def transaction(self) -> _FakeTx:
        # 事务开在**连接**上（`async with conn.transaction()`），不是池上。
        return _FakeTx()

    async def execute(self, sql: str, *args: Any) -> str:
        self.sink.append(args)
        self.sqls.append(sql)
        return f"INSERT 0 {self.insert_rowcount}"

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

    def __init__(
        self,
        owned: bool = True,
        existing_ids: list[str] | None = None,
        insert_rowcount: int = 1,
    ) -> None:
        self.owned = owned
        self.existing_ids = existing_ids or []
        self.inserted: list[tuple] = []
        self.sqls: list[str] = []
        self.insert_rowcount = insert_rowcount

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        return {"ok": 1} if self.owned else None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [{"tool_call_id": i} for i in self.existing_ids]

    def acquire(self) -> _FakeConn:
        return _FakeConn(self.inserted, insert_rowcount=self.insert_rowcount, sqls=self.sqls)

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
            _Row({"role": "tool", "content": "b", "tool_calls": "", "turn_id": "", "tool_call_id": "c2", "id": "m2", "seq": 2}),
            _Row({"role": "user", "content": "a", "tool_calls": "", "turn_id": "t1", "tool_call_id": "", "id": "m1", "seq": 1}),
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
                    "id": "m1",
                    "seq": 1,
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

    @pytest.mark.asyncio
    async def test_reads_archive_when_blob_ref_present(self, monkeypatch):
        """`blob_ref` 非空时原文在对象存储，content 只是截断体 —— 必须读回全文。

        不读归档的话，>4MB 的消息取回来只有前 64k + 一句标记，
        "原文可召回"这个承诺就是空的。
        """
        full = "原文" * 100

        class _Pool(_FakePool):
            async def fetchrow(self, sql, *args):
                # 同一个 fetchrow 既做归属校验又读消息，必须按 SQL 区分，
                # 否则 fake 会把归属校验也当成消息查询。
                if "FROM sessions" in sql:
                    return _Row({"ok": 1})
                assert "message_archives" in sql, "必须 JOIN 归档表拿 storage_key"
                return _Row({"content": "截断体", "storage_key": "archives/arc_x.txt"})

        class _Store:
            def read_at(self, key: str) -> bytes | None:
                assert key == "archives/arc_x.txt"
                return full.encode("utf-8")

        _patch_pool(monkeypatch, _Pool())
        monkeypatch.setattr("app.media.store.get_store", lambda: _Store(), raising=False)

        got = await history_writer.load_message_content("s1", 1, "m1")

        assert got is not None
        chunk, total = got
        assert chunk == full, "应从对象存储读回全文，而不是返回截断体"
        assert total == len(full)

    @pytest.mark.asyncio
    async def test_archive_read_failure_falls_back_to_stored_content(self, monkeypatch):
        """对象丢了不能返回空：退回 content（前 64k + 标记）总比什么都取不回好。"""
        class _Pool(_FakePool):
            async def fetchrow(self, sql, *args):
                return _Row({"content": "前64k[archived]", "storage_key": "archives/arc_x.txt"})

        class _BrokenStore:
            def read_at(self, key: str) -> bytes | None:
                raise OSError("object gone")

        _patch_pool(monkeypatch, _Pool())
        monkeypatch.setattr("app.media.store.get_store", lambda: _BrokenStore(), raising=False)

        got = await history_writer.load_message_content("s1", 1, "m1")

        assert got is not None
        assert got[0] == "前64k[archived]", "归档读失败应退回库里的截断体"

    @pytest.mark.asyncio
    async def test_no_blob_ref_does_not_touch_object_store(self, monkeypatch):
        """普通消息不该白白触发一次对象存储往返。"""
        class _Pool(_FakePool):
            async def fetchrow(self, sql, *args):
                return _Row({"content": "短正文", "storage_key": None})

        class _Store:
            def read_at(self, key: str) -> bytes | None:
                raise AssertionError("没有 blob_ref 时不该读对象存储")

        _patch_pool(monkeypatch, _Pool())
        monkeypatch.setattr("app.media.store.get_store", lambda: _Store(), raising=False)

        got = await history_writer.load_message_content("s1", 1, "m1")

        assert got == ("短正文", 3)


class TestTieredArchiving:
    """分级归档（§2.3）：≤4MB 原样入库，>4MB 外置。

    这一组守的是"原文不会永久丢失"：第 10 步之前 256k 字符以上一律截断，
    而设计要求的 64KB~4MB 区间本该原样保留。
    """

    @pytest.mark.asyncio
    async def test_content_below_threshold_is_stored_verbatim(
        self, monkeypatch: pytest.MonkeyPatch, no_privacy
    ):
        """256k 字符以上、4MB 以下的正文必须**原样**入库，不能被旧上限截断。"""
        pool = _FakePool()
        _patch_pool(monkeypatch, pool)
        big = "x" * 300_000  # 超出旧 MESSAGE_CONTENT_MAX_CHARS，但远小于 4MB

        await history_writer.persist_messages("s1", 1, [{"role": "tool", "content": big}])

        # 参数序：(session_id, role, content, tool_calls, tool_call_id, tid, blob_ref)
        assert pool.inserted[0][2] == big, "300k 字符的正文被截断了，64KB~4MB 区间会永久丢失原文"
        assert pool.inserted[0][6] is None, "未超阈值不该产生 blob_ref"

    @pytest.mark.asyncio
    async def test_content_above_threshold_is_archived(
        self, monkeypatch: pytest.MonkeyPatch, no_privacy
    ):
        """>4MB 正文写对象存储，content 只留前 64k + 标记，blob_ref 指向归档。"""
        pool = _FakePool()
        _patch_pool(monkeypatch, pool)
        written: dict[str, bytes] = {}

        class _Store:
            def write_at(self, key: str, content: bytes) -> None:
                written[key] = content

        monkeypatch.setattr("app.media.store.get_store", lambda: _Store(), raising=False)

        big = "y" * (history_writer.ARCHIVE_THRESHOLD_BYTES + 1)

        await history_writer.persist_messages("s1", 1, [{"role": "tool", "content": big}])

        # 归档行先于消息行写入 —— 先落对象再写行，避免库里留下指向不存在对象的
        # blob_ref。按语句内容挑出 messages 那条，而不是按参数个数：后者每加一列
        # 都会误伤这条断言，且失败信息完全指错方向。
        rows = [
            args
            for sql, args in zip(pool.sqls, pool.inserted, strict=True)
            if "INSERT INTO messages" in sql
        ]
        assert len(rows) == 1, f"应只写一条 messages，实际 {len(pool.inserted)} 条语句"
        row = rows[0]
        # 参数序：(session_id, role, content, tool_calls, tool_call_id, tid, blob_ref, source)
        assert row[6], "超阈值必须写 blob_ref"
        assert row[6].startswith("arc_")
        assert row[2].startswith("y" * 100), "content 应保留一段真原文供摘要使用"
        assert len(row[2]) < len(big), "content 不该再存全文"
        assert "ref=" in row[2], "content 里要带归档提示，否则模型不知道原文在哪"
        assert written[f"archives/{row[6]}.txt"] == big.encode("utf-8"), "归档必须是全文"
        # 归档元数据行也要落，否则 blob_ref 指向的归档无法被清理与校验
        assert any(
            "INSERT INTO message_archives" in sql and args[0] == row[6]
            for sql, args in zip(pool.sqls, pool.inserted, strict=True)
        ), "缺少 message_archives 记录"

    @pytest.mark.asyncio
    async def test_archive_failure_falls_back_to_truncation(
        self, monkeypatch: pytest.MonkeyPatch, no_privacy
    ):
        """归档挂了不能连累消息写入：退回截断，宁可丢原文也不能丢整条历史。"""
        pool = _FakePool()
        _patch_pool(monkeypatch, pool)

        class _BrokenStore:
            def write_at(self, key: str, content: bytes) -> None:
                raise OSError("disk full")

        monkeypatch.setattr("app.media.store.get_store", lambda: _BrokenStore(), raising=False)

        big = "z" * (history_writer.ARCHIVE_THRESHOLD_BYTES + 1)

        n = await history_writer.persist_messages("s1", 1, [{"role": "tool", "content": big}])

        assert n == 1, "归档失败不该导致整条消息丢失"
        # 参数序：(session_id, role, content, tool_calls, tool_call_id, tid, blob_ref)
        assert pool.inserted[0][6] is None, "归档失败时不能留下指向不存在对象的 blob_ref"
        assert "truncated" in pool.inserted[0][2]


class TestPersistFolds:
    """`session_folds` 的写入契约。

    没有这一层，`folds` 收集器白算：区间算出来却没人落库，库里永远看不出
    "这段原文被哪次折叠覆盖过"，跨副本重建投影也无从拼接尾部。
    """

    @pytest.mark.asyncio
    async def test_writes_one_row_per_fold(
        self, monkeypatch: pytest.MonkeyPatch, no_privacy
    ):
        pool = _FakePool()
        _patch_pool(monkeypatch, pool)

        n = await history_writer.persist_folds(
            "s1",
            1,
            [
                {
                    "covered_from": 3,
                    "covered_to": 17,
                    "covered_ids": ["m1", "m2"],
                    "body_suffix": 42,
                }
            ],
        )

        assert n == 1
        row = pool.inserted[0]
        # 参数序：(id, session_id, covered_from, covered_to, digest,
        #          digest_model, digest_prompt, body_suffix)
        assert len(row) == 8
        assert row[1] == "s1"
        assert row[2] == 3 and row[3] == 17, "区间必须原样落库，半开区间错一位就会误覆盖"
        assert row[7] == 42, "body_suffix 丢了，投影拼接就会错位"
        assert row[0], "id 必须生成（无主键行写不进去）"

    @pytest.mark.asyncio
    async def test_conflict_does_not_count_as_write(
        self, monkeypatch: pytest.MonkeyPatch, no_privacy
    ):
        """`ON CONFLICT DO NOTHING` 命中时不得计入写入数。

        这是规则二（`python-engine/app/agent/history_writer.py`）的落地点：跨副本用
        不同模型摘要同一区间时，先到者胜、后到者被静默丢弃。若仍无条件 `written += 1`，
        调用方会以为摘要更新了，实际读到的还是旧那份 —— 冲突变成不可观测。
        """
        pool = _FakePool(insert_rowcount=0)
        _patch_pool(monkeypatch, pool)

        n = await history_writer.persist_folds(
            "s1",
            1,
            [{"covered_from": 3, "covered_to": 17, "body_suffix": 42}],
        )

        assert n == 0, "冲突未写入却计入写入数，会让冲突彻底不可观测"
        assert len(pool.inserted) == 1, "语句本身仍然执行了（由唯一索引裁决）"

    @pytest.mark.asyncio
    async def test_refuses_when_session_not_owned(
        self, monkeypatch: pytest.MonkeyPatch, no_privacy
    ):
        """跨租户不能往别人的会话里塞折叠记录。"""
        pool = _FakePool(owned=False)
        _patch_pool(monkeypatch, pool)

        n = await history_writer.persist_folds(
            "s1", 1, [{"covered_from": 1, "covered_to": 2, "body_suffix": 0}]
        )

        assert n == 0
        assert pool.inserted == []

    @pytest.mark.asyncio
    async def test_empty_folds_is_noop(self, monkeypatch: pytest.MonkeyPatch, no_privacy):
        pool = _FakePool()
        _patch_pool(monkeypatch, pool)

        assert await history_writer.persist_folds("s1", 1, []) == 0
        assert pool.inserted == []

    @pytest.mark.asyncio
    async def test_no_retention_skips_write(self, monkeypatch: pytest.MonkeyPatch):
        """合规优先：no_retention 下任何历史痕迹都不落库。"""
        monkeypatch.setattr(
            "app.middleware.privacy_middleware.is_no_retention", lambda: True, raising=False
        )
        pool = _FakePool()
        _patch_pool(monkeypatch, pool)

        n = await history_writer.persist_folds(
            "s1", 1, [{"covered_from": 1, "covered_to": 2, "body_suffix": 0}]
        )

        assert n == 0
        assert pool.inserted == []

    @pytest.mark.asyncio
    async def test_digest_falls_back_without_covered_ids(
        self, monkeypatch: pytest.MonkeyPatch, no_privacy
    ):
        """摘要缺失时不能写空串：`digest` 是 NOT NULL 且有排查价值。"""
        pool = _FakePool()
        _patch_pool(monkeypatch, pool)

        await history_writer.persist_folds(
            "s1", 1, [{"covered_from": 5, "covered_to": 9, "body_suffix": 1}]
        )

        assert pool.inserted[0][4], "digest 不能为空"
