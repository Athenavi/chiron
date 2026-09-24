"""Tests for tools.context — 工具执行上下文传播。"""
from __future__ import annotations

import asyncio

from app.tools.context import (
    get_gateway,
    get_session_id,
    get_tenant_id,
    get_tool_context,
    get_user_id,
    set_tool_context,
)


class TestToolContext:
    def test_defaults_empty(self):
        assert get_session_id() == ""
        assert get_user_id() == ""
        assert get_tenant_id() == ""
        assert get_gateway() is None
        assert get_tool_context("nope", "d") == "d"

    def test_set_and_get(self):
        set_tool_context(session_id="s1", user_id="u1", tenant_id="t1", gateway="gw")
        assert get_session_id() == "s1"
        assert get_user_id() == "u1"
        assert get_tenant_id() == "t1"
        assert get_gateway() == "gw"
        # 重置避免污染其它测试
        set_tool_context(session_id="", user_id="", tenant_id="", gateway=None)

    def test_async_propagation(self):
        async def worker():
            return get_session_id()

        async def main():
            set_tool_context(session_id="async-s")
            return await worker()

        assert asyncio.run(main()) == "async-s"
        set_tool_context(session_id="", user_id="", tenant_id="", gateway=None)
