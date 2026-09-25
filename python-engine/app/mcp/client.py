"""MCP Client — connects to MCP servers over stdio, discovers and calls tools.

Mirrors Go internal/mcp/client.go with multi-server support.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

# MCP protocol version
MCP_PROTOCOL_VERSION = "2025-03-26"

logger = logging.getLogger(__name__)


def _is_private_ip(host: str) -> bool:
    """Check if a host resolves to a private/reserved IP address (SSRF protection)."""
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        pass
    try:
        addrinfos = socket.getaddrinfo(host, None, socket.AI_ADDRCONFIG)
        for family, _, _, _, sockaddr in addrinfos:
            ip_str = sockaddr[0] if family in (socket.AF_INET, socket.AF_INET6) else None
            if ip_str:
                try:
                    addr = ipaddress.ip_address(ip_str)
                    if addr.is_private or addr.is_loopback or addr.is_link_local:
                        return True
                except ValueError:
                    continue
    except (socket.gaierror, OSError):
        pass
    return False


@dataclass
class ServerDef:
    """MCP server configuration."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    transport: str = "stdio"  # "stdio" | "http_sse"
    url: str = ""  # HTTP SSE endpoint URL (used when transport="http_sse")
    #: 该 server 是否**只提供只读操作**。引擎只直连 read_only 的 server（见 _connect_server 的
    #: 连接守卫）：连接凭据（env / url）会留在引擎进程里，而那正是 agent 能读到的地方。
    #: 未声明默认 False —— fail-closed：宁可工具不可用，也不给引擎高权限凭据。
    read_only: bool = False


def assert_server_connectable(server: ServerDef, *, role: str = "engine") -> None:
    """直连守卫：**引擎**只允许直连 **read_only** 的 MCP server（见 ``_connect_server``）。

    ``role="broker"``（独立 MCP broker 进程）是唯一被允许连危险 server 的角色 —— 它的凭据与
    执行面都与引擎隔离，且执行前仍要向服务端（Go 的 Tool Broker）要授权：判定权威在服务端，
    broker 只负责"用凭据执行"，两者不在同一个信任域。

    抽成模块级函数是为了能被直接单测 —— 这条边界值得有独立断言。
    """
    if role == "broker" or server.read_only:
        return
    raise PermissionError(
        f"MCP server '{server.name}' is not declared read-only — refusing to connect from "
        "the engine. Credentials for write/external tools must not live in agent reach; "
        'declare "read_only": true in the plugin config if this server only reads.'
    )


@dataclass
class MCPTool:
    """A tool provided by an MCP server."""

    name: str  # Namespaced: {server_name}_{tool_name}
    description: str
    input_schema: dict[str, Any]
    server_name: str
    local_name: str  # Original tool name on the server


class HTTPSSEConnection:
    """Connection to an MCP server over HTTP SSE transport."""

    def __init__(self, url: str, name: str):
        self.url = url.rstrip("/")
        self.name = name
        self._req_id = 0
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=30.0)
        self._session_id: str | None = None

        # P0 安全修复：SSRF 防护 — 检查 URL 主机构是否为私有地址
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname
        if host and _is_private_ip(host):
            raise ValueError(
                f"SSRF protection: connecting to private/restricted IP is forbidden "
                f"(host={host}, server={name})"
            )

    async def send_jsonrpc(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a JSON-RPC request via HTTP POST and read the response."""
        self._req_id += 1
        req = {
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": method,
            "params": params or {},
        }
        headers = {"Content-Type": "application/json"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        async with self._lock:
            resp = await self._client.post(
                f"{self.url}/messages",
                json=req,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

            # Extract session ID from SSE response headers
            session_id = resp.headers.get("Mcp-Session-Id")
            if session_id:
                self._session_id = session_id

        if "error" in data and data["error"]:
            raise RuntimeError(f"MCP error: {data['error'].get('message', 'unknown')}")
        result: dict[str, Any] = data.get("result", {})
        return result

    async def close(self) -> None:
        await self._client.aclose()


class ServerConnection:
    """Connection to a single MCP server process."""

    def __init__(self, proc: asyncio.subprocess.Process, name: str):
        self.proc = proc
        self.name = name
        self._req_id = 0
        self._lock = asyncio.Lock()
        # create_subprocess_exec 传了 PIPE，三个流运行时恒非 None；这里 cast 只为把
        # `StreamReader | None` 收窄成具体类型（存成属性，避免每个使用点各自断言）。
        self._stdin = cast("asyncio.StreamWriter", proc.stdin)
        self._stdout = cast("asyncio.StreamReader", proc.stdout)
        self._stderr = cast("asyncio.StreamReader", proc.stderr)
        # 异步读取 stderr 并记录到日志，避免 DEVNULL 丢弃错误信息
        self._stderr_task = asyncio.ensure_future(self._read_stderr())

    async def _read_stderr(self) -> None:
        """异步读取 MCP 服务器 stderr 并记录到日志。"""
        try:
            async for line in self._stderr:
                if line.strip():
                    logger.warning(
                        "MCP stderr [%s]: %s", self.name, line.decode().rstrip()
                    )
        except Exception:
            pass

    async def send_jsonrpc(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and read the response."""
        self._req_id += 1
        req = {
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": method,
            "params": params,
        }
        req_line = json.dumps(req) + "\n"

        async with self._lock:
            self._stdin.write(req_line.encode())
            await self._stdin.drain()

            response_line = await asyncio.wait_for(
                self._stdout.readline(), timeout=30.0
            )
            if not response_line:
                raise ConnectionError(f"No response from MCP server {self.name}")

        resp = json.loads(response_line)
        if "error" in resp and resp["error"]:
            raise RuntimeError(f"MCP error: {resp['error'].get('message', 'unknown')}")
        result: dict[str, Any] = resp.get("result", {})
        return result

    async def close(self) -> None:
        """Kill the server process."""
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=5.0)
            except (TimeoutError, ProcessLookupError):
                self.proc.kill()


class MCPClient:
    """Manages connections to multiple MCP servers and their tools."""

    def __init__(self, servers: list[ServerDef], *, role: str = "engine"):
        #: "engine"（默认）= 只能连 read_only 的 server；"broker" = 独立 MCP broker 进程，
        #: 唯一被允许持凭据连危险 server 的角色（见 assert_server_connectable）。
        self._role = role
        self._servers = servers
        self._conns: dict[str, ServerConnection | HTTPSSEConnection] = {}
        self._tools: list[MCPTool] = []

    async def start(self) -> None:
        """Connect to all configured MCP servers and discover their tools."""
        for server in self._servers:
            try:
                await self._connect_server(server)
            except Exception as e:
                logger.error("MCP connect %s failed: %s", server.name, e)
                raise

    async def _connect_server(self, server: ServerDef) -> None:
        """Connect to a single MCP server and discover its tools.

        **直连守卫（安全边界）**：引擎只允许直连**声明为只读**的 MCP server。

        理由：连接凭据来自 ``ServerDef.env`` / ``url``（插件配置），而引擎进程正在处理
        不可信内容 —— 被 prompt injection 影响的 agent 可以直接拿这些凭据触达外部系统，
        绕过网关的全部授权、审计与限流（docs/agent-safety-and-reliability.md §1.4）。

        未声明 ``read_only`` 的 server 一律**不连**（fail-closed）。要放行请在插件配置里显式
        写 ``"read_only": true``（表示它只提供读操作）；含写 / 删 / 外部操作的 server 应由服务端
        分发，不能把凭据留在 agent 可触达的地方。
        """
        assert_server_connectable(server, role=self._role)
        if server.transport == "http_sse":
            await self._connect_http_sse(server)
        elif server.transport == "stdio":
            await self._connect_stdio(server)
        else:
            raise ValueError(f"Unsupported MCP transport: {server.transport}")

    async def _connect_http_sse(self, server: ServerDef) -> None:
        """Connect to an MCP server via HTTP SSE."""
        if not server.url:
            raise ValueError("HTTP SSE transport requires 'url' in ServerDef")
        conn = HTTPSSEConnection(server.url, server.name)
        self._conns[server.name] = conn

        # Initialize
        await conn.send_jsonrpc(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "clientInfo": {"name": "chiron-python", "version": "3.0.0"},
                "capabilities": {},
            },
        )

        # List tools
        tools_result = await conn.send_jsonrpc("tools/list", None)
        raw_tools = tools_result.get("tools", [])
        self._register_tools(server, raw_tools)
        logger.info("MCP server %s connected (HTTP SSE): %d tools", server.name, len(raw_tools))

    async def _connect_stdio(self, server: ServerDef) -> None:
        """Connect to an MCP server via stdio (subprocess)."""
        # 安全修复（P0-S7）：仅允许 PLUGIN_COMMAND_ALLOWLIST 白名单内的命令被拉起
        from app.tools.ssrf import command_allowed

        if not command_allowed(server.command):
            raise PermissionError(
                f"MCP server command {server.command!r} not allowed: "
                "set PLUGIN_COMMAND_ALLOWLIST (comma-separated basenames) to enable"
            )
        env = None
        if server.env:
            # 安全修复：使用 sandboxed_env 清理宿主环境变量，
            # 仅传递基础 PATH/HOME 和 server.env 白名单覆盖，
            # 防止 MCP 子进程读取 JWT_SECRET/INTERNAL_TOKEN 等敏感密钥
            from app.tools.sandbox import sandboxed_env
            env = {**sandboxed_env(), **server.env}

        proc = await asyncio.create_subprocess_exec(
            server.command,
            *server.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        conn = ServerConnection(proc, server.name)
        self._conns[server.name] = conn

        # Initialize
        await conn.send_jsonrpc(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "clientInfo": {"name": "chiron-python", "version": "3.0.0"},
            },
        )

        # List tools
        result = await conn.send_jsonrpc("tools/list", None)
        raw_tools = result.get("tools", [])
        self._register_tools(server, raw_tools)
        logger.info("MCP server %s connected (stdio): %d tools", server.name, len(raw_tools))

    def _register_tools(self, server: ServerDef, raw_tools: list[dict[str, Any]]) -> None:
        """Register tools from a server response."""
        for i, t in enumerate(raw_tools):
            tool = MCPTool(
                name=f"{server.name}_{t.get('name', f'unnamed_{i}')}",
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
                server_name=server.name,
                local_name=t.get("name", f"unnamed_{i}"),
            )
            self._tools.append(tool)
            logger.info("MCP tool discovered: %s (%s)", tool.name, server.name)

    @property
    def tools(self) -> list[MCPTool]:
        return self._tools

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Call a tool on the appropriate MCP server."""
        for server in self._servers:
            prefix = f"{server.name}_"
            if tool_name.startswith(prefix):
                local_name = tool_name[len(prefix) :]
                conn = self._conns.get(server.name)
                if not conn:
                    return {"error": f"MCP server {server.name} not connected"}
                result = await conn.send_jsonrpc(
                    "tools/call",
                    {
                        "name": local_name,
                        "arguments": arguments,
                    },
                )
                return result
        return {"error": f"Tool {tool_name} not found on any MCP server"}

    async def close(self) -> None:
        """Shut down all MCP server connections."""
        for name, conn in self._conns.items():
            try:
                await conn.close()
            except Exception as e:
                logger.warning("Error closing MCP server %s: %s", name, e)
        self._conns.clear()


async def load_mcp_config(config_path: str) -> list[ServerDef]:
    """Load MCP server definitions from a JSON config file."""
    from pathlib import Path

    p = Path(config_path)
    if not p.exists():
        return []

    data = json.loads(p.read_text(encoding="utf-8"))
    servers = []
    for s in data.get("mcp_servers", []):
        servers.append(
            ServerDef(
                name=s["name"],
                command=s["command"],
                args=s.get("args", []),
                env=s.get("env", {}),
                transport=s.get("transport", "stdio"),
                url=s.get("url", ""),
            )
        )
    return servers
