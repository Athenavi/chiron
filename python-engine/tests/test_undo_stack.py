"""文件写入的撤销栈（app/agent/undo_stack.py）。

回归保护：`/undo` 此前是**空壳** —— 只把 metadata 里一个字符串 pop 出来回显
（`Undone: ...`），文件没有任何变化。用户以为撤销了其实没有。这组断言钉住三件事：

1. 快照能**真实恢复**文件（写入前的状态）；
2. 那次写入**新建**的文件，撤销时应当被删除；
3. 恢复前校验工作区边界 —— 快照存在 Redis，不能据此写任意路径。
"""
import time

import pytest

from app.agent.undo_stack import Snapshot, pop, push, restore


def _patch_workspace(monkeypatch, path):
    monkeypatch.setattr("app.tools.sandbox.workspace_dir", lambda: path)


def test_restore_rewrites_previous_content(tmp_path, monkeypatch):
    _patch_workspace(monkeypatch, tmp_path)
    target = tmp_path / "a.txt"
    target.write_text("current", encoding="utf-8")

    snapshot = Snapshot(
        path=str(target), existed=True, content="original", tool="write_file", at=time.time()
    )
    detail = restore(snapshot)

    assert target.read_text(encoding="utf-8") == "original"
    assert "restored" in detail


def test_restore_removes_file_created_by_that_write(tmp_path, monkeypatch):
    _patch_workspace(monkeypatch, tmp_path)
    target = tmp_path / "new.txt"
    target.write_text("created", encoding="utf-8")

    snapshot = Snapshot(
        path=str(target), existed=False, content="", tool="write_file", at=time.time()
    )
    detail = restore(snapshot)

    assert not target.exists()
    assert "removed" in detail


def test_restore_refuses_path_outside_workspace(tmp_path, monkeypatch):
    # 工作区是 tmp_path/ws，而快照指向 tmp_path/outside.txt —— 必须拒绝
    _patch_workspace(monkeypatch, tmp_path / "ws")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")

    snapshot = Snapshot(
        path=str(outside), existed=True, content="tampered", tool="write_file", at=time.time()
    )
    detail = restore(snapshot)

    assert "refused" in detail
    assert outside.read_text(encoding="utf-8") == "keep"  # 原文件未被改动


def test_describe_mentions_tool_and_path(tmp_path):
    snapshot = Snapshot(
        path=str(tmp_path / "x.txt"), existed=True, content="abc", tool="edit_file", at=time.time()
    )
    text = snapshot.describe()
    assert "edit_file" in text
    assert "x.txt" in text


class _FakeRedis:
    """只实现 undo_stack 用到的 list 命令。"""

    def __init__(self):
        self.lists: dict[str, list[str]] = {}

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key, start, end):
        self.lists[key] = self.lists.get(key, [])[: end + 1]

    async def expire(self, key, ttl):
        return True

    async def lpop(self, key):
        items = self.lists.get(key, [])
        return items.pop(0) if items else None

    async def lrange(self, key, start, end):
        return self.lists.get(key, [])[start : end + 1]


@pytest.mark.asyncio
async def test_push_pop_is_lifo(monkeypatch):
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.redis_client.get_redis", _get_redis)

    first = Snapshot(path="/ws/a", existed=True, content="1", tool="write_file", at=1.0)
    second = Snapshot(path="/ws/b", existed=True, content="2", tool="edit_file", at=2.0)
    assert await push("s1", first) is True
    assert await push("s1", second) is True

    # LIFO：最近一次写入先被撤销
    popped = await pop("s1")
    assert popped is not None and popped.path == "/ws/b"
    popped = await pop("s1")
    assert popped is not None and popped.path == "/ws/a"
    assert await pop("s1") is None
