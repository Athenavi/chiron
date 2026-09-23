"""经 MCP broker 调用工具 —— 危险 server **不直连**。

引擎侧不持有危险 MCP server 的连接与凭据（``app/mcp/client.py`` 的连接守卫会拒绝），
这类 server 的工具改由独立进程 MCP broker 执行：凭据只存在于那一个进程里，引擎即使被
完全控制也读不到。

授权链路不变：broker 在执行前仍向服务端（Go 的 Tool Broker）要授权，判定权威仍在服务端。
本模块只负责"把调用递过去"。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 单次调用超时（秒）。MCP 侧本就有 30s 工具超时，这里留出授权 + 网络余量。
CALL_TIMEOUT_SECONDS = 60.0
#: 工具发现超时（秒）—— broker 首次连接时会拉起 MCP 子进程，给宽一点。
DISCOVER_TIMEOUT_SECONDS = 30.0


def _endpoint() -> tuple[str, str]:
    from app.config import settings

    base = (getattr(settings, "mcp_broker_url", "") or "http://127.0.0.1:8001").rstrip("/")
    return base, settings.internal_token or ""


async def _post(path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    import aiohttp

    base, token = _endpoint()
    if not token:
        raise RuntimeError("mcp broker not configured (INTERNAL_TOKEN missing)")
    async with aiohttp.ClientSession() as client:
        async with client.post(
            f"{base}{path}",
            json=payload,
            headers={"X-Internal-Token": token},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                detail = data.get("detail") if isinstance(data, dict) else data
                raise RuntimeError(f"mcp broker HTTP {resp.status}: {detail}")
            return data


async def list_tools(user_id: str, server_name: str = "") -> list[dict[str, Any]]:
    """拉取工具清单（引擎据此注册"经 broker 调用"的工具）。"""
    data = await _post(
        "/v1/mcp/tools",
        {"user_id": user_id, "server_name": server_name},
        DISCOVER_TIMEOUT_SECONDS,
    )
    return list(data.get("tools") or [])


async def call_tool(
    user_id: str,
    tool_name: str,
    args: dict[str, Any] | None,
    *,
    server_name: str = "",
    tenant_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    tools_mode: str = "auto",
) -> dict[str, Any]:
    """执行一次工具调用；broker 会先向服务端要授权，被拒时返回 ``{success: false, ...}``。"""
    return await _post(
        "/v1/mcp/call",
        {
            "user_id": user_id,
            "tool_name": tool_name,
            "arguments": args or {},
            "server_name": server_name,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "tool_call_id": tool_call_id,
            "tools_mode": tools_mode,
        },
        CALL_TIMEOUT_SECONDS,
    )
