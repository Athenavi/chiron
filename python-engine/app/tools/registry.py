"""本地工具注册表（Python 端）。

职责：
- 提供统一的工具注册/查找接口
- 支持将工具导出为 OpenAI function-calling schema
- 支持直接执行本地工具（替代 Go /v1/tools/execute）

用户隔离（S 安全修复）：MCP 等用户级工具注册时携带 ``owner``（user_id 集合，
空集合 = 全局工具）。``to_openai_tools`` 与 ``execute`` 按当前上下文用户过滤/校验，
避免用户 A 的插件工具被用户 B 列出或调用。注册表为进程内可重建状态：
实例重启后由插件配置重新注册（幂等覆盖）。

来源标注：``source`` 说明工具从哪来（``builtin`` 内置 / ``mcp`` MCP 或插件注入），
由注入点显式声明而不是从 ``owner`` 推断 —— ``app/mcp/registry.py`` 注册的是全局
MCP 工具（无 owner），只按 owner 判断会漏标。前端据此把"看不见的 MCP"展示出来。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# 工具来源；新增来源时同步前端的 ToolInfo.source 与展示文案
SOURCE_BUILTIN = "builtin"
SOURCE_MCP = "mcp"

# 隐藏来源：这些工具**不进入模型可见清单**（也不被 tool_search 搜到/激活），
# 只能经 `use_capability` 代理工具调用（P7，对齐 Reasonix usecap）。
#
# 理由：MCP / 插件工具的数量与 schema 由外部服务器决定，直接暴露会让请求体随
# "这个用户连了几个 MCP、每个 MCP 有几个工具"而漂移 —— 既贵（每个 schema 都占 token）
# 又不稳定（同名工具被不同服务器抢注册）。代理工具的 schema 是**固定的**，不随 MCP 增删改变。
HIDDEN_SOURCES: frozenset[str] = frozenset({SOURCE_MCP})


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    owners: set[str] = field(default_factory=set)  # 空 = 全局工具
    source: str = SOURCE_BUILTIN


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., Awaitable[Any]],
        owner: str = "",
        source: str = SOURCE_BUILTIN,
    ) -> None:
        """注册工具。owner 非空时该工具仅对 owner（user_id）可见/可调用；
        同名重复注册幂等覆盖；已存在且带 owner 时合并归属用户集合
        （共享连接去重场景：多用户引用同一 MCP 服务器）。

        source 由调用方声明（内置工具用默认值，MCP/插件注入点传 SOURCE_MCP）。
        """
        existing = self._tools.get(name)
        if existing is not None:
            owners: set[str] = set(existing.owners)
            if owner:
                owners.add(owner)
            existing.owners = owners
            existing.description = description
            existing.parameters = parameters
            existing.handler = handler
            existing.source = source
            return
        owners = set()
        if owner:
            owners.add(owner)
        self._tools[name] = ToolDef(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
            owners=owners,
            source=source,
        )

    def set_owners(self, name: str, owners: set[str]) -> None:
        """更新工具的归属用户集合（共享连接去重场景：多用户引用同一配置）。"""
        tool = self._tools.get(name)
        if tool is not None:
            tool.owners = set(owners or ())

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def unregister_prefix(self, prefix: str) -> int:
        """移除所有以 prefix 开头的工具（MCP 重载前清理），返回移除数量。"""
        removed = [n for n in self._tools if n.startswith(prefix)]
        for n in removed:
            del self._tools[n]
        return len(removed)

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        return list(self._tools.keys())

    def list_capabilities(self, user_id: str = "") -> list[dict[str, str]]:
        """列出**隐藏来源**的能力（供 use_capability 的按需发现使用）。

        与 ``to_openai_tools`` 互补：那些**不进模型可见清单**的工具（MCP / 插件），
        从这里能被列出来，模型据此知道代理背后有哪些能力。
        """
        user_id = self._resolve_user(user_id)
        out: list[dict[str, str]] = []
        for tool in self._tools.values():
            if tool.source not in HIDDEN_SOURCES or not self._visible(tool, user_id):
                continue
            out.append({"name": tool.name, "description": tool.description})
        return sorted(out, key=lambda t: t["name"])

    def _visible(self, tool: ToolDef, user_id: str) -> bool:
        return not tool.owners or user_id in tool.owners

    @staticmethod
    def _resolve_user(user_id: str) -> str:
        """显式传入优先；否则从工具上下文（contextvars）取当前用户。"""
        if user_id:
            return user_id
        try:
            from app.tools.context import get_user_id

            return get_user_id() or ""
        except ImportError:  # pragma: no cover — 上下文模块不可用时视为未认证
            return ""

    def to_openai_tools(self, user_id: str = "") -> list[dict[str, Any]]:
        """导出工具列表；按当前用户过滤掉其他用户的工具，并**跳过隐藏来源**。

        source 放在 function 外层：它不属于 OpenAI 的 function schema，
        混进去会被上游 provider 拒绝。

        P7：``HIDDEN_SOURCES``（MCP / 插件）**不进入可见清单** —— 它们只能经
        ``use_capability`` 代理调用（见 app/tools/use_capability.py）。这样请求体里的
        工具 schema **不随"用户连了几个 MCP"而漂移**，且 tool_search 也搜不到它们。
        """
        user_id = self._resolve_user(user_id)
        converted: list[dict[str, Any]] = []
        for tool in self._tools.values():
            if not self._visible(tool, user_id) or tool.source in HIDDEN_SOURCES:
                continue
            converted.append(
                {
                    "type": "function",
                    "source": tool.source,
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
            )
        return converted

    async def execute(
        self, name: str, params: dict[str, Any], user_id: str = ""
    ) -> dict[str, Any]:
        """执行工具。owner 工具要求当前用户匹配（无上下文/未认证时拒绝 owner 工具，
        防止未认证直连越权调用他人插件工具）。"""
        user_id = self._resolve_user(user_id)
        tool = self._tools.get(name)
        if not tool:
            return {"error": f"Tool '{name}' not found"}
        if not self._visible(tool, user_id):
            return {"error": f"Tool '{name}' is not available for this user"}
        try:
            result = await tool.handler(**params)
            if isinstance(result, dict):
                return result
            return {"output": result}
        except Exception as e:
            return {"error": str(e)}


# 全局注册表（进程内单例）
registry = ToolRegistry()
