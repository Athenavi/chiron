"""`list_subagent_runs` 工具 —— 主 Agent 的「主动感知」入口。

回归背景（用户报告）：子 Agent 的后续进展只有**刷新页面**才看得到。根因之一是观测只有
"推送"通道（followup 投递 + 面板轮询），而主 Agent 这边只有 `read_subagent_result` ——
它**要求 run_id**。长对话里主 Agent 很容易忘了自己派过什么，于是"有没有子任务、它们怎么样"
只能靠人去看侧边栏。

本工具把观测补成**可查询**（设计稿 §二 原则 4），而且数据来自 DB（唯一权威）：
即使 followup 投递失败、缓存过期、面板没打开，结论依然查得到。
"""
from __future__ import annotations

import datetime

import pytest

from app.tools import subagent_list

NOW = datetime.datetime(2026, 9, 23, 15, 0, 0)


class _FakePool:
    def __init__(self, rows):
        self._rows = rows
        self.calls: list[tuple] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self._rows


@pytest.fixture
def fake_pool(monkeypatch):
    rows = [
        {
            "id": "rs_live", "parent_run_id": None, "status": "running",
            "profile_name": "reviewer", "summary": None, "steps": 12, "read_only": True,
            "input_tokens": 100, "output_tokens": 20, "error": None,
            "created_at": NOW, "finished_at": None,
        },
        {
            "id": "rs_done", "parent_run_id": None, "status": "completed",
            "profile_name": "", "summary": "x" * 900, "steps": 205, "read_only": False,
            "input_tokens": 900, "output_tokens": 300, "error": None,
            "created_at": NOW, "finished_at": NOW,
        },
    ]
    pool = _FakePool(rows)
    monkeypatch.setattr("app.db.get_pool", lambda: pool)
    monkeypatch.setattr(subagent_list, "get_tenant_id", lambda: "t1")
    monkeypatch.setattr(subagent_list, "get_user_id", lambda: "u1")
    monkeypatch.setattr(subagent_list, "get_session_id", lambda: "s1")
    return pool


@pytest.mark.asyncio
async def test_lists_runs_with_authoritative_fields(fake_pool):
    result = await subagent_list.list_subagent_runs()

    assert result["count"] == 2
    first, second = result["runs"]
    assert first["run_id"] == "rs_live" and first["status"] == "running"
    assert first["usage"] == {"input_tokens": 100, "output_tokens": 20}
    assert first["steps"] == 12 and first["read_only"] is True
    # 未结束的 run 不该伪造 finished_at
    assert "finished_at" not in first
    assert second["finished_at"] == NOW.isoformat()
    # 列表是总览：summary 必须截断（细节用 read_subagent_result 取）
    assert len(second["summary"]) == subagent_list.SUMMARY_MAX_CHARS
    # 空字段不下发（保持 payload 精简）
    assert "error" not in first and "parent_run_id" not in first


@pytest.mark.asyncio
async def test_query_is_scoped_to_tenant_user_and_session(fake_pool):
    """隔离是硬要求：tenant + user + root_session 三重校验，缺一条就会读到别人的子 Agent。"""
    await subagent_list.list_subagent_runs(status="completed", limit=7)

    sql, args = fake_pool.calls[0]
    assert "WHERE tenant_id = $1" in sql
    assert "root_session_id = $3" in sql
    assert "user_id = $2 OR user_id IS NULL" in sql
    assert args == ("t1", "u1", "s1", "completed", 7)


@pytest.mark.asyncio
async def test_limit_is_capped(fake_pool):
    await subagent_list.list_subagent_runs(limit=9999)
    assert fake_pool.calls[0][1][4] == subagent_list.MAX_LIMIT
    await subagent_list.list_subagent_runs(limit=0)
    assert fake_pool.calls[1][1][4] == subagent_list.DEFAULT_LIMIT


@pytest.mark.asyncio
async def test_unknown_status_filter_is_rejected(fake_pool):
    result = await subagent_list.list_subagent_runs(status="whatever")
    assert "error" in result
    assert not fake_pool.calls, "非法过滤值不该打到数据库"


@pytest.mark.asyncio
async def test_without_session_context_returns_error(fake_pool, monkeypatch):
    monkeypatch.setattr(subagent_list, "get_session_id", lambda: "")
    assert "error" in await subagent_list.list_subagent_runs()


@pytest.mark.asyncio
async def test_missing_table_degrades_with_hint(monkeypatch):
    class _Broken:
        async def fetch(self, sql, *args):
            raise RuntimeError('relation "subagent_runs" does not exist')

    monkeypatch.setattr("app.db.get_pool", lambda: _Broken())
    monkeypatch.setattr(subagent_list, "get_tenant_id", lambda: "t1")
    monkeypatch.setattr(subagent_list, "get_user_id", lambda: "u1")
    monkeypatch.setattr(subagent_list, "get_session_id", lambda: "s1")

    result = await subagent_list.list_subagent_runs()
    assert "error" in result and "migration" in result["error"]


def test_tool_is_registered_as_read_only():
    """必须登记为只读工具：未登记的工具按 write 处理 → auto 模式下每次查询都要用户确认，
    "主动感知"就变成了新的打扰源。"""
    from app.agent.tool_policy import READ, tool_level
    from app.tools.registry import registry

    assert "list_subagent_runs" in registry.list_names()
    assert tool_level("list_subagent_runs") == READ
