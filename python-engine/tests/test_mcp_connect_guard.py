"""MCP 直连守卫（app/mcp/client.py）。

回归保护：MCP 的连接凭据（``ServerDef.env`` / ``url``）来自插件配置，而引擎进程正在处理
**不可信内容** —— 被注入的 agent 可以直接拿这些凭据触达外部系统，绕过网关的授权、审计与
限流。所以引擎**只允许直连声明为只读的 MCP server**，未声明一律拒绝（fail-closed）。
"""
import json

import pytest

from app.mcp.client import ServerDef, assert_server_connectable


def test_refuses_server_without_read_only_declaration():
    server = ServerDef(name="github", command="npx", args=["-y", "server-github"])
    with pytest.raises(PermissionError) as exc:
        assert_server_connectable(server)
    # 报错要说清"怎么放行"，否则用户只能干瞪眼
    assert "read_only" in str(exc.value)
    assert "github" in str(exc.value)


def test_allows_declared_read_only_server():
    assert_server_connectable(ServerDef(name="docs", command="npx", read_only=True))


def test_read_only_defaults_to_false():
    # fail-closed 的前提：默认就是"不直连"
    assert ServerDef(name="x", command="y").read_only is False


def test_plugin_config_read_only_reaches_server_def(tmp_path):
    """声明链路：插件配置里的 ``read_only`` 必须一路传到 ``ServerDef``。

    漏传的后果很典型 —— "MCP 插件明明标了只读却连不上"（守卫把已声明的也拒了）。
    """
    from app.plugins.pool import _server_to_def
    from app.plugins.store import PluginStore

    store = PluginStore(data_dir=tmp_path / "plugins")
    path = store._path("u1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "mcp_servers": [
                    {"name": "docs", "command": "npx", "read_only": True},
                    {"name": "writer", "command": "npx"},
                ]
            }
        ),
        encoding="utf-8",
    )

    servers = {server.name: server for server in store.load("u1")}
    assert servers["docs"].read_only is True
    assert servers["writer"].read_only is False

    assert _server_to_def(servers["docs"]).read_only is True
    assert _server_to_def(servers["writer"]).read_only is False


def test_round_trip_preserves_read_only(tmp_path):
    """save → load 往返不能丢掉声明（否则重启后已放行的 server 会突然连不上）。"""
    from app.plugins.store import PluginStore, ServerConfig

    store = PluginStore(data_dir=tmp_path / "plugins")
    store.save("u1", [ServerConfig(name="docs", command="npx", read_only=True)])

    loaded = store.load("u1")
    assert loaded and loaded[0].read_only is True
