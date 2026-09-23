"""插件模块测试：PluginStore per-user 隔离、ActiveTracker、registry owner 过滤、连接池去重共享。"""
from __future__ import annotations

import pytest

from app.plugins.store import ActiveTracker, PluginStore, ServerConfig
from app.plugins.pool import MCPClientPool, _fingerprint
from app.tools.registry import registry


# ── PluginStore per-user 隔离 ─────────────────────────────────────────────

def test_plugin_store_per_user_isolation(tmp_path):
    store = PluginStore(tmp_path)
    store.save("u1", [ServerConfig(name="git", command="npx", args=["-y", "@modelcontextprotocol/server-git"], status="active")])
    store.save("u2", [ServerConfig(name="web", command="npx", args=["-y", "web-server"], status="inactive")])

    u1 = store.load("u1")
    assert len(u1) == 1 and u1[0].name == "git"
    # u2 的配置不影响 u1
    assert store.load("u1") == u1
    # inactive 服务器不返回
    assert store.active_servers("u2") == []
    # 未知用户返回空
    assert store.load("nobody") == []


def test_plugin_store_signature_changes_on_update(tmp_path):
    store = PluginStore(tmp_path)
    store.save("u1", [ServerConfig(name="git", command="npx", args=["-y", "git-server"])])
    sig1 = store.signature("u1")
    store.save("u1", [ServerConfig(name="git", command="npx", args=["-y", "git-server-v2"])])
    sig2 = store.signature("u1")
    assert sig1 != sig2


# ── ActiveTracker ─────────────────────────────────────────────────────────

def test_active_tracker_window():
    tracker = ActiveTracker()
    tracker.touch("u1")
    assert tracker.is_active("u1", window=3600)
    assert tracker.is_active("u2", window=3600) is False
    assert "u1" in tracker.active_users(window=3600)
    tracker.prune(window=0)  # 立即过期
    assert tracker.is_active("u1", window=0) is False


# ── registry owner 过滤 ───────────────────────────────────────────────────

async def _fake_owner_tool(**_kw):
    return {"ok": True}


@pytest.mark.asyncio
async def test_registry_owner_isolation():
    registry.register("global_tool_x", "global", {"type": "object"}, _fake_owner_tool)
    registry.register("mcp_u1_tool", "owned by u1", {"type": "object"}, _fake_owner_tool, owner="u1")

    try:
        # u1 可见 owner 工具 + 全局工具
        names_u1 = {t["function"]["name"] for t in registry.to_openai_tools(user_id="u1")}
        assert "mcp_u1_tool" in names_u1 and "global_tool_x" in names_u1

        # u2 不可见 owner 工具，但可见全局工具
        names_u2 = {t["function"]["name"] for t in registry.to_openai_tools(user_id="u2")}
        assert "mcp_u1_tool" not in names_u2 and "global_tool_x" in names_u2

        # 执行：u2 调用 u1 的 owner 工具被拒绝；u1 可调用
        denied = await registry.execute("mcp_u1_tool", {}, user_id="u2")
        assert "not available" in denied["error"]
        allowed = await registry.execute("mcp_u1_tool", {}, user_id="u1")
        assert allowed.get("ok") is True
    finally:
        registry.unregister("global_tool_x")
        registry.unregister("mcp_u1_tool")


# ── 连接池：配置去重共享 + 用户释放 ──────────────────────────────────────

class _FakeMCPClient:
    """替代 MCPClient：start 后提供固定工具。"""

    def __init__(self, servers):
        self._servers = servers
        self.tools = []
        self.closed = False

    async def start(self):
        self.tools = [
            type("T", (), {"name": f"{self._servers[0].name}_demo_tool", "description": "demo",
                            "input_schema": {"type": "object"}})()
        ]

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_pool_shares_connection_by_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr("app.plugins.pool.MCPClient", _FakeMCPClient)

    store = PluginStore(tmp_path)
    # 声明 read_only：这个用例测的是"连接共享按指纹复用"，属于**引擎直连**路径的逻辑。
    # 未声明的 server 会改走 MCP broker（凭据不出引擎进程），不建本地连接 —— 见
    # app/plugins/pool.py 的分支与 app/mcp/client.py 的连接守卫。
    cfg = [
        ServerConfig(
            name="git", command="npx", args=["-y", "git-server"], status="active", read_only=True
        )
    ]
    store.save("u1", cfg)
    store.save("u2", cfg)  # 相同配置

    tracker = ActiveTracker()
    tracker.touch("u1")
    tracker.touch("u2")
    pool = MCPClientPool(store=store, tracker=tracker)

    try:
        await pool.reconcile()
        # 相同配置 → 只建一个共享连接
        assert len(pool._conns) == 1  # noqa: SLF001
        shared = next(iter(pool._conns.values()))
        assert shared.users == {"u1", "u2"}

        # 工具注册且归属两个用户
        tool = registry.get("git_demo_tool")
        assert tool is not None and tool.owners == {"u1", "u2"}

        # 释放 u1 后：工具仍属于 u2，连接仍被 u2 引用
        tracker._last["u1"] = 0  # noqa: SLF001 — 模拟 u1 过期
        await pool.reconcile()
        tool = registry.get("git_demo_tool")
        assert tool is not None and tool.owners == {"u2"}

        # 全部释放后：工具注销、连接关闭
        tracker._last["u2"] = 0  # noqa: SLF001
        await pool.reconcile()
        assert registry.get("git_demo_tool") is None
        assert len(pool._conns) == 0
    finally:
        await pool.stop()
