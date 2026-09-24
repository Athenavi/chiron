"""副作用账本（app/agent/side_effect_ledger.py）。

回归保护：账本要回答"这一轮改了什么、能不能撤"。两个最容易搞错的地方：

* **不能**把只读操作也记进去 —— 噪声会淹没真正要紧的信号；
* **不能**对撤不回的操作假装可撤销 —— 撤不回就明说（诚实优先，与 undo_stack 同一原则）。
"""
import time

import pytest

from app.agent.side_effect_ledger import (
    KIND_DELETE,
    KIND_EXTERNAL,
    KIND_FILE_WRITE,
    ROLLBACK_AUTO,
    ROLLBACK_NONE,
    SideEffect,
    confirmation_warning,
    kind_for,
    rollback_capability,
    target_of,
)


def test_kind_maps_from_action_level():
    assert kind_for("write", "write_file") == KIND_FILE_WRITE
    assert kind_for("delete", "shell_exec") == KIND_DELETE
    assert kind_for("external", "web_fetch") == KIND_EXTERNAL
    assert kind_for("read", "read_file") == ""  # 只读没有副作用类别


def test_only_file_writes_are_auto_rollback():
    assert rollback_capability("write", "write_file") == ROLLBACK_AUTO
    assert rollback_capability("write", "edit_file") == ROLLBACK_AUTO
    # 删除与外部调用一律"不可撤销" —— 不猜、不承诺
    assert rollback_capability("delete", "shell_exec") == ROLLBACK_NONE
    assert rollback_capability("external", "web_fetch") == ROLLBACK_NONE
    assert rollback_capability("write", "git_commit") == ROLLBACK_NONE


def test_confirmation_warning_only_for_irreversible():
    assert "不可撤销" in confirmation_warning("external", "web_fetch")
    assert "不可撤销" in confirmation_warning("delete", "shell_exec")
    # 可撤销 / 只读：不啰嗦（满屏警告等于没有警告）
    assert confirmation_warning("write", "write_file") == ""
    assert confirmation_warning("read", "read_file") == ""


def test_target_of_prefers_meaningful_keys():
    assert target_of({"path": "./a.txt"}, "write_file") == "./a.txt"
    assert target_of({"url": "https://x/y"}, "web_fetch") == "https://x/y"
    assert target_of({"command": "rm -rf /tmp"}, "shell_exec") == "rm -rf /tmp"
    assert target_of({}, "mystery") == "mystery"
    assert target_of(None, "mystery") == "mystery"
    # 折行的命令压成一行（账本是给人看的）
    assert target_of({"command": "a\n  b"}, "shell_exec") == "a b"


def test_describe_states_rollback_capability():
    auto = SideEffect(
        kind=KIND_FILE_WRITE, tool="write_file", target="a.txt", rollback=ROLLBACK_AUTO, at=time.time()
    )
    none = SideEffect(
        kind=KIND_EXTERNAL, tool="web_fetch", target="https://x", rollback=ROLLBACK_NONE, at=time.time()
    )
    assert "可撤销" in auto.describe()
    assert "不可撤销" in none.describe()


class _FakeRedis:
    def __init__(self):
        self.lists: dict[str, list[str]] = {}

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key, start, end):
        self.lists[key] = self.lists.get(key, [])[: end + 1]

    async def expire(self, key, ttl):
        return True

    async def lrange(self, key, start, end):
        return self.lists.get(key, [])[start : end + 1]


@pytest.mark.asyncio
async def test_record_then_read_back_most_recent_first(monkeypatch):
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.redis_client.get_redis", _get_redis)

    from app.agent.side_effect_ledger import recent, record

    first = SideEffect(
        kind=KIND_FILE_WRITE, tool="write_file", target="a", rollback=ROLLBACK_AUTO, at=1.0
    )
    second = SideEffect(
        kind=KIND_EXTERNAL, tool="web_fetch", target="b", rollback=ROLLBACK_NONE, at=2.0
    )
    await record("s1", first)
    await record("s1", second)

    entries = await recent("s1")
    assert [entry.tool for entry in entries] == ["web_fetch", "write_file"]
    assert entries[0].rollback == ROLLBACK_NONE
