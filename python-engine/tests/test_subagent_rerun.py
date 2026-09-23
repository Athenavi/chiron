"""`rerun_subagent` 工具 —— 终态 run 的重跑（含写权限照旧 + 血缘）。

回归背景：后台子 Agent 与父回合解耦后，"实例重启判 lost / 用户停止判 cancelled /
provider 报错判 failed"都是常态。这些 run 的任务与 Profile 都还在 DB 里，
重跑必须**按原样**再派一次 —— 让模型重新拼任务文本几乎必然与原来不一致，
那就不叫重跑了。同时：重跑**不能**变成提权通道（原 run 只读，重跑也必须只读）。
"""
from __future__ import annotations

import pytest

from app.tools import subagent_rerun


class _FakePool:
    def __init__(self, row=None, error: Exception | None = None):
        self._row = row
        self._error = error
        self.calls: list[tuple] = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if self._error:
            raise self._error
        return self._row


def _row(**overrides):
    row = {
        "id": "rs_old",
        "task": "把 utils.py 里的日期解析改成时区安全",
        "profile_name": "reviewer",
        "read_only": True,
        "status": "lost",
        "depth": 1,
    }
    row.update(overrides)
    return row


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setattr(subagent_rerun, "get_tenant_id", lambda: "t1")
    monkeypatch.setattr(subagent_rerun, "get_user_id", lambda: "u1")
    monkeypatch.setattr(subagent_rerun, "get_session_id", lambda: "s1")


def _patch_pool(monkeypatch, pool):
    monkeypatch.setattr("app.db.get_pool", lambda: pool)


def _patch_delegate(monkeypatch, captured: list[dict]):
    async def _fake(**kwargs):
        captured.append(kwargs)
        return {"status": "async_launched", "isAsync": True, "run_id": "rs_new",
                "result_ref": "rs_new", "note": "detail"}

    monkeypatch.setattr("app.tools.subagent.subagent", _fake)


@pytest.mark.asyncio
async def test_rerun_reuses_task_profile_and_keeps_readonly(monkeypatch, ctx):
    pool = _FakePool(_row())
    _patch_pool(monkeypatch, pool)
    captured: list[dict] = []
    _patch_delegate(monkeypatch, captured)

    result = await subagent_rerun.rerun_subagent("rs_old")

    assert result["run_id"] == "rs_new" and result["rerun_of"] == "rs_old"
    assert "note" not in result, "重跑不该把'请继续做别的事'那类提示原样带给模型"
    call = captured[0]
    assert call["task"] == "把 utils.py 里的日期解析改成时区安全"
    assert call["profile"] == "reviewer"
    assert call["run_in_background"] is True
    assert call["rerun_of"] == "rs_old"
    # 关键：原 run 只读 ⇒ 重跑仍只读（重跑不是提权通道）
    assert call["allow_write"] is False


@pytest.mark.asyncio
async def test_rerun_preserves_write_permission(monkeypatch, ctx):
    pool = _FakePool(_row(read_only=False))
    _patch_pool(monkeypatch, pool)
    captured: list[dict] = []
    _patch_delegate(monkeypatch, captured)

    await subagent_rerun.rerun_subagent("rs_old")
    assert captured[0]["allow_write"] is True


@pytest.mark.asyncio
async def test_rerun_rejects_running_run(monkeypatch, ctx):
    pool = _FakePool(_row(status="running"))
    _patch_pool(monkeypatch, pool)
    captured: list[dict] = []
    _patch_delegate(monkeypatch, captured)

    result = await subagent_rerun.rerun_subagent("rs_old")
    assert "error" in result and "only finished runs" in result["error"]
    assert not captured, "running 的 run 已经有 owner，不能再派一份并发作业"


@pytest.mark.asyncio
async def test_rerun_is_scoped_to_session_tenant_and_user(monkeypatch, ctx):
    pool = _FakePool(None)
    _patch_pool(monkeypatch, pool)
    captured: list[dict] = []
    _patch_delegate(monkeypatch, captured)

    result = await subagent_rerun.rerun_subagent("rs_other")
    assert "not found" in result["error"]
    sql, args = pool.calls[0]
    assert "root_session_id = $4" in sql and "tenant_id = $2" in sql
    assert args == ("rs_other", "t1", "u1", "s1")


@pytest.mark.asyncio
async def test_task_override_replaces_stored_task(monkeypatch, ctx):
    pool = _FakePool(_row())
    _patch_pool(monkeypatch, pool)
    captured: list[dict] = []
    _patch_delegate(monkeypatch, captured)

    await subagent_rerun.rerun_subagent("rs_old", task_override="只跑测试，别改代码")
    assert captured[0]["task"] == "只跑测试，别改代码"


@pytest.mark.asyncio
async def test_missing_column_degrades(monkeypatch, ctx):
    """老库还没有 rerun_of 列（迁移没执行）：必须报错而不是假成功。"""
    pool = _FakePool(error=RuntimeError('column "rerun_of" does not exist'))
    _patch_pool(monkeypatch, pool)

    result = await subagent_rerun.rerun_subagent("rs_old")
    assert "error" in result and "migrations" in result["error"]


@pytest.mark.asyncio
async def test_without_session_context(monkeypatch):
    monkeypatch.setattr(subagent_rerun, "get_tenant_id", lambda: "t1")
    monkeypatch.setattr(subagent_rerun, "get_user_id", lambda: "u1")
    monkeypatch.setattr(subagent_rerun, "get_session_id", lambda: "")
    _patch_pool(monkeypatch, _FakePool(_row()))

    assert "error" in await subagent_rerun.rerun_subagent("rs_old")


def test_rerun_tool_registered_as_write():
    """重跑会真的再起一个作业（消耗 token、复用写权限）→ 必须按 write 级别确认。"""
    from app.agent.tool_policy import WRITE, tool_level
    from app.tools.registry import registry

    assert "rerun_subagent" in registry.list_names()
    assert tool_level("rerun_subagent") == WRITE
