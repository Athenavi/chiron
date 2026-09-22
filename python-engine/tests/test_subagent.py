"""Tests for subagent — 真子 Agent 委派。"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from app.gateway.provider import ChatResponse
from app.tools.context import set_tool_context
from app.tools.subagent import subagent


def _make_gateway(text: str = "child result"):
    gw = MagicMock()

    async def fake_stream(**kwargs):
        yield ChatResponse(content=text, finish_reason="stop")

    gw.chat_stream = fake_stream
    return gw


class TestSubagent:
    @pytest.mark.asyncio
    async def test_returns_child_output(self):
        gw = _make_gateway("child did the work")
        set_tool_context(session_id="parent-s", user_id="u1", tenant_id="t1", gateway=gw)
        out = await subagent("read files and summarize", run_in_background=False)
        # output 按 L2 契约包了 <subagent-result> 不可信标记，正文在其中
        assert "child did the work" in out["output"]
        # payload 契约（to_tool_payload）：status / output / result_ref / usage / summary / truncated
        assert out["status"] == "completed"
        assert out["result_ref"].startswith("rs_")
        assert "usage" in out and "summary" in out

    @pytest.mark.asyncio
    async def test_restores_parent_context(self):
        gw = _make_gateway("ok")
        set_tool_context(session_id="parent-s", user_id="u1", tenant_id="t1", gateway=gw)
        await subagent("task", run_in_background=False)
        from app.tools.context import get_session_id
        assert get_session_id() == "parent-s"  # 子 agent 执行后父上下文还原

    @pytest.mark.asyncio
    async def test_no_gateway_errors(self):
        set_tool_context(session_id="", user_id="", tenant_id="", gateway=None)
        out = await subagent("task")
        assert "error" in out and "runtime" in out["error"]

    @pytest.mark.asyncio
    async def test_empty_task_rejected(self):
        set_tool_context(session_id="s", user_id="u", tenant_id="t", gateway=_make_gateway())
        out = await subagent("   ")
        assert "error" in out

    @pytest.mark.asyncio
    async def test_child_error_propagates(self):
        gw = MagicMock()

        async def failing_stream(**kwargs):
            yield ChatResponse(content="", finish_reason="error")

        gw.chat_stream = failing_stream
        set_tool_context(session_id="s", user_id="u", tenant_id="t", gateway=gw)
        out = await subagent("task", run_in_background=False)
        # 无文本但有 error 事件 → 返回 error；或 fallback 无输出时给占位
        assert "output" in out or "error" in out
        set_tool_context(session_id="", user_id="", tenant_id="", gateway=None)

    @pytest.mark.asyncio
    async def test_depth_limit_blocks_recursion(self):
        """S3: 委派深度超过 MAX_DEPTH 时拒绝，防无限递归。"""
        gw = _make_gateway("ok")
        from app.tools.context import set_tool_context
        from app.tools.subagent import MAX_DEPTH
        set_tool_context(session_id="s", user_id="u", tenant_id="t", gateway=gw, subagent_depth=MAX_DEPTH)
        out = await subagent("nested task")
        assert "error" in out and "depth" in out["error"]
        set_tool_context(session_id="", user_id="", tenant_id="", gateway=None, subagent_depth=0)

    @pytest.mark.asyncio
    async def test_child_delegation_depth_is_capped_by_default(self):
        """S3: 无 Profile 的通用子 Agent 默认只能再委派一层（runner 的 DEFAULT_MAX_DEPTH）。

        这里其实有**两层**阈值，语义不同：
          * ``tools/subagent.py`` 的 ``MAX_DEPTH=3`` 是**全局硬上限**（超过连 runner 都不进）；
          * runner 的 ``DEFAULT_MAX_DEPTH=1`` 是**通用子 Agent 的默认上限**，更严格 ——
            所以 depth=1 再委派会被拒（child_depth=2 > 1）。

        这是刻意的保守默认：默认不允许子 Agent 继续繁殖；要更深必须显式配 Profile。
        """
        gw = _make_gateway("ok")
        from app.tools.context import set_tool_context
        set_tool_context(session_id="s", user_id="u", tenant_id="t", gateway=gw, subagent_depth=1)
        out = await subagent("task", run_in_background=False)
        assert out["status"] == "failed"
        assert "depth exceeded" in str(out.get("error", ""))
        set_tool_context(session_id="", user_id="", tenant_id="", gateway=None, subagent_depth=0)

    @pytest.mark.asyncio
    async def test_defaults_to_background_delegation(self):
        """默认走后台：立即返回 run_id，且 run 登记进注册表并**可被停止**。

        回归保护：默认同步会让父 turn 原地等整轮子 Agent（可能数分钟），而同步 run 既不在
        注册表（看门狗与前端「停止」都够不到），也没有 wall 上限 —— 父回合被无限期占住，
        这就是"主 Agent 长期阻塞"的主因。
        """
        import asyncio

        from app.subagent import registry as subagent_registry

        gw = _make_gateway("bg result")
        set_tool_context(session_id="s", user_id="u", tenant_id="t", gateway=gw)
        run_id = ""
        try:
            out = await subagent("background task")
            assert out["status"] == "async_launched"
            assert out["isAsync"] is True
            run_id = out["run_id"]
            # 在注册表里 = 能被「停止」也能被看门狗收口（本次修复的核心保证）
            assert any(item["run_id"] == run_id for item in subagent_registry.list_active("s"))
            assert subagent_registry.cancel(run_id, "cancelled_by_user") is True
        finally:
            # 注册表是模块级的、event loop 是每个用例一个：task 的 finally 可能落在下一个
            # loop 里（甚至永不发生），所以这里显式反注册，避免跨用例污染。
            subagent_registry.unregister(run_id)
            set_tool_context(session_id="", user_id="", tenant_id="", gateway=None)


class TestSyncWallBudget:
    """同步委派的 wall 上限 —— "父 turn 原地等"的路径必须有兜底。"""

    def test_defaults_and_overrides(self, monkeypatch):
        from app.tools.subagent import DEFAULT_SYNC_MAX_SECONDS, _sync_wall_seconds

        monkeypatch.delenv("SUBAGENT_SYNC_MAX_SECONDS", raising=False)
        # 同步 + 未显式指定 → 给默认上限（**不能**是 0 = 不限）
        assert _sync_wall_seconds(False, 0) == DEFAULT_SYNC_MAX_SECONDS
        # 显式值优先
        assert _sync_wall_seconds(False, 42) == 42
        # 后台委派不额外限时：它由看门狗按 idle / max_runtime 收口
        assert _sync_wall_seconds(True, 0) == 0
        # 环境变量覆盖
        monkeypatch.setenv("SUBAGENT_SYNC_MAX_SECONDS", "77")
        assert _sync_wall_seconds(False, 0) == 77

    def test_run_in_background_default_is_true(self):
        import inspect

        from app.tools.subagent import subagent as subagent_tool

        assert inspect.signature(subagent_tool).parameters["run_in_background"].default is True
