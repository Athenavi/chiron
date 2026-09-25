"""MCP Registry — registers MCP tools into the local Python tool registry."""

from __future__ import annotations

import logging
from typing import Any

from app.mcp.client import MCPClient, MCPTool, load_mcp_config
from app.tools.registry import SOURCE_MCP
from app.tools.registry import registry as local_registry

logger = logging.getLogger(__name__)


async def init_mcp(config_path: str) -> MCPClient | None:
    """Load MCP config, connect servers, register tools. Returns client or None."""
    servers = await load_mcp_config(config_path)
    if not servers:
        return None

    client = MCPClient(servers)
    try:
        await client.start()
    except Exception as e:
        logger.error("MCP initialization failed: %s", e)
        return None

    # Register each MCP tool into the local tool registry
    for tool in client.tools:
        _register_mcp_tool(client, tool)

    logger.info("MCP tools registered: %d", len(client.tools))
    return client


def _register_mcp_tool(client: MCPClient, tool: MCPTool) -> None:
    """Register a single MCP tool as a callable tool in the local registry."""

    # Build JSON Schema for parameters
    properties = {}
    required = []
    if isinstance(tool.input_schema, dict):
        properties = tool.input_schema.get("properties", {})
        required = tool.input_schema.get("required", [])

    schema = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required

    async def handler(**kwargs: Any) -> dict[str, Any]:
        return await client.call_tool(tool.name, kwargs)

    local_registry.register(
        name=tool.name,
        description=tool.description,
        parameters=schema,
        handler=handler,
        source=SOURCE_MCP,
    )
