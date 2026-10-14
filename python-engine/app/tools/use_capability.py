"""use_capability — 单一能力代理工具（P7）。

对照实现：Reasonix `internal/runtime/usecap`（"the one fixed tool through which…"），
其测试**锁死该工具的 schema 字节不随 MCP 增删而变**。

## 为什么需要它

Chiron 已有 `tool_search`（按需激活），但它解决的是"**少暴露**"而非"**稳定暴露**"：
MCP / 插件工具仍是普通注册项，一旦被激活就进入请求体。于是同一份请求的工具清单会随
"这个用户连了几个 MCP、每个 MCP 有几个工具"而**漂移** —— 既贵（每个 schema 都占 token），
又不稳定（同名工具被不同服务器抢注册，上游 provider 看到的工具集每次不同）。

本工具把这条通道收成一个：**MCP / 插件（隐藏来源）不直接可见，只能经它访问**，
而它的参数 schema 是**固定的两个字面字段**，与后端连了什么 MCP 无关。

## 发现与调用

* 无参调用 ⇒ 返回**可用能力清单**（名字 + 描述），模型据此知道代理背后有什么；
* 带参调用 ⇒ 转发给对应能力。

内置工具**不**经它调用（它们本就在清单里，经代理只会多一层并掩盖来源）—— 直接拒绝。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from app.tools.registry import HIDDEN_SOURCES, SOURCE_BUILTIN, registry

logger = logging.getLogger(__name__)

USE_CAPABILITY_NAME = "use_capability"

#: ⚠️ **固定 schema** —— 这正是本工具的全部意义（对齐 Reasonix usecap 的
#: "schema 字节不随 MCP 增删而变"）。**不要**动态改它，尤其不要往 properties 里
#: 加入 MCP 的工具名 —— 那会让请求体重新随后端漂移，把这个问题原样带回来。
_CAPABILITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "capability": {
            "type": "string",
            "description": (
                "Name of the external capability to invoke. Omit to list what is available."
            ),
        },
        "arguments": {
            "type": "object",
            "description": "Arguments object for the capability (matches its parameters).",
        },
    },
    "required": ["capability"],
}


def capability_schema() -> dict[str, Any]:
    """返回代理工具参数的**独立副本**（供测试断言其不随 MCP 增删改变）。"""
    return copy.deepcopy(_CAPABILITY_SCHEMA)


def _available() -> list[str]:
    return [c["name"] for c in registry.list_capabilities()]


async def use_capability(
    capability: str = "", arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """调用一个**隐藏来源**（MCP / 插件）的能力；无参时列出可用能力。"""
    name = (capability or "").strip()
    if not name:
        caps = registry.list_capabilities()
        return {
            "capabilities": caps,
            "count": len(caps),
            "note": (
                "Pass {'capability': '<name>', 'arguments': {...}} to invoke one. "
                "These are external integrations; built-in tools are called directly."
            ),
        }

    tool = registry.get(name)
    if tool is None:
        return {"error": f"unknown capability '{name}'", "available": _available()}
    if tool.source not in HIDDEN_SOURCES:
        # 内置工具本就在工具清单里：经代理调用只会多一层、还掩盖调用来源。
        return {
            "error": (
                f"'{name}' is a built-in tool; call it directly instead of via "
                f"{USE_CAPABILITY_NAME}"
            )
        }
    return await registry.execute(name, dict(arguments or {}))


registry.register(
    name=USE_CAPABILITY_NAME,
    description=(
        "Invoke an external capability (MCP server / plugin tool) by name. Call it with no "
        "arguments to list the available capabilities, then call it again with the capability "
        "name and an arguments object matching that capability's parameters. Built-in tools "
        "are called directly, not through this tool."
    ),
    parameters=_CAPABILITY_SCHEMA,
    handler=use_capability,
    source=SOURCE_BUILTIN,
)
