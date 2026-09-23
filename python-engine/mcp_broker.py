"""MCP broker —— 独立进程，**唯一**持有危险 MCP server 连接与凭据的地方。

为什么独立
----------
引擎（python-engine）正在处理不可信内容（LLM 输出 / 用户输入 / 工具结果），被注入的 agent
能读到该进程里的一切 —— 包括 MCP 连接凭据（``ServerDef.env`` / ``url``）。所以危险 server 的
连接必须移出引擎进程：引擎侧由 ``assert_server_connectable`` 拒绝直连，改为调用本服务。

独立进程还顺带给了两件事：**执行面收窄**（本服务只做 MCP，不做别的）、**出站可单独限制**
（部署时只放行白名单域名，引擎侧不需要这些出站能力）。

授权仍在服务端
--------------
broker 在执行前向 Go 的 Tool Broker 要一次授权（``POST /v1/internal/tool-authorize``）：
判定权威在服务端，broker 只负责"用凭据执行"，两者不在同一个信任域。这条链路**不能**省 ——
否则 broker 就变成一个"拿到就能用"的高权限代理。

启动
----
``python mcp_broker.py``（默认 ``127.0.0.1:8001``，``MCP_BROKER_PORT`` 可覆盖；由 run.py 管理）。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("mcp-broker")

app = FastAPI(title="chiron-mcp-broker")

#: user_id → MCPClient（懒加载 + 常驻缓存：MCP 连接本身就是有状态的）
_clients: dict[str, Any] = {}


class ToolListRequest(BaseModel):
    user_id: str
    server_name: str = ""


class ToolCallRequest(BaseModel):
    user_id: str
    tool_name: str
    arguments: dict[str, Any] = {}
    server_name: str = ""
    tenant_id: str = ""
    session_id: str = ""
    tool_call_id: str = ""
    tools_mode: str = "auto"


def _internal_token() -> str:
    from app.config import settings

    return settings.internal_token or ""


def _check_token(token: str | None) -> None:
    expected = _internal_token()
    if not expected:
        raise HTTPException(status_code=503, detail="broker misconfigured: INTERNAL_TOKEN not set")
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid internal token")


async def _client_for(user_id: str):
    """按用户懒加载 MCPClient。

    ``role="broker"`` 是关键：它是**唯一**被允许持凭据连接危险 server 的角色
    （引擎侧调同一个函数会拿到 PermissionError）。
    """
    client = _clients.get(user_id)
    if client is not None:
        return client

    from app.mcp.client import MCPClient
    from app.plugins.pool import _server_to_def
    from app.plugins.store import PluginStore

    servers = [_server_to_def(server) for server in PluginStore().load(user_id)]
    client = MCPClient(servers, role="broker")
    await client.start()
    _clients[user_id] = client
    logger.info("mcp broker connected %d server(s) for user %s", len(servers), user_id)
    return client


def _pick(client, server_name: str):
    """取指定 server 的工具子集；server_name 为空表示全部。"""
    tools = list(getattr(client, "tools", []) or [])
    if server_name:
        tools = [tool for tool in tools if getattr(tool, "server_name", "") == server_name]
    return tools


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "mcp-broker", "users": len(_clients)}


@app.post("/v1/mcp/tools")
async def list_tools(
    body: ToolListRequest, x_internal_token: str | None = Header(default=None)
) -> dict[str, Any]:
    """列出某用户的 MCP 工具清单（引擎用它注册"经 broker 调用"的工具）。"""
    _check_token(x_internal_token)
    try:
        client = await _client_for(body.user_id)
    except Exception as exc:  # noqa: BLE001 - 连接失败要让引擎看得见原因
        logger.warning("mcp broker connect failed for %s: %s", body.user_id, exc)
        raise HTTPException(status_code=502, detail=f"mcp connect failed: {exc}") from exc

    return {
        "tools": [
            {
                "name": tool.name,
                "description": getattr(tool, "description", "") or "",
                "input_schema": getattr(tool, "input_schema", {}) or {},
                "server_name": getattr(tool, "server_name", "") or "",
                "local_name": getattr(tool, "local_name", "") or "",
            }
            for tool in _pick(client, body.server_name)
        ]
    }


@app.post("/v1/mcp/call")
async def call_tool(
    body: ToolCallRequest, x_internal_token: str | None = Header(default=None)
) -> dict[str, Any]:
    """执行一次 MCP 工具调用：**先向服务端要授权，再执行**。"""
    _check_token(x_internal_token)

    # ① 服务端授权（判定权威在 Go 的 Tool Broker；broker 不自己拍板）
    try:
        from app.tools.broker import authorize

        decision = await authorize(
            body.tool_name,
            body.arguments,
            tenant_id=body.tenant_id,
            user_id=body.user_id,
            session_id=body.session_id,
            tool_call_id=body.tool_call_id,
            tools_mode=body.tools_mode,
        )
    except Exception as exc:  # noqa: BLE001 - 拿不到授权就不执行（fail-closed）
        logger.error("tool broker unavailable, refusing %s: %s", body.tool_name, exc)
        raise HTTPException(
            status_code=503, detail=f"authorization service unavailable: {exc}"
        ) from exc

    if not decision.get("allowed", False):
        reason = decision.get("reason") or "not allowed by server policy"
        logger.warning("tool broker denied mcp call %s: %s", body.tool_name, reason)
        return {"success": False, "error": f"denied by server policy: {reason}"}

    # ② 执行（凭据只在本进程里）
    try:
        client = await _client_for(body.user_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"mcp connect failed: {exc}") from exc

    try:
        result = await client.call_tool(body.tool_name, body.arguments or {})
    except Exception as exc:  # noqa: BLE001 - 工具自身失败要如实回传，不是 broker 故障
        logger.warning("mcp tool %s failed: %s", body.tool_name, exc)
        return {"success": False, "error": str(exc)}

    return {
        "success": True,
        "result": result,
        "level": decision.get("level"),
        "rollback": decision.get("rollback"),
    }


def main() -> None:
    import uvicorn

    port = int(os.getenv("MCP_BROKER_PORT", "8001"))
    host = os.getenv("MCP_BROKER_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, log_level=os.getenv("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
