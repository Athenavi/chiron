"""
Multi-Agent System — Claude Code 风格的子代理调度机制

提供 SubAgent（子代理）和 AgentDispatcher（调度器），支持：
- 注册命名子代理
- 同步/异步调度子任务
- 内置 code / review / research / test 代理
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.gateway.provider import ChatMessage, ToolCall
from app.gateway.router import GatewayRouter
from app.tools.registry import ToolRegistry, registry

logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────────────


@dataclass
class SubAgentResult:
    """子代理执行结果"""

    success: bool
    output: str
    tool_calls: list[dict] = field(default_factory=list)
    token_usage: dict = field(default_factory=dict)
    duration: float = 0.0
    error: str = ""


# ── 子代理 ────────────────────────────────────────────────


class SubAgent:
    """
    可调度的子代理，拥有独立的推理循环上下文。

    运行一个完整的 agent loop：构建 system prompt → 调 LLM → 解析结果，
    最多迭代 max_turns 轮。
    """

    def __init__(
        self,
        name: str,
        description: str,
        system_prompt: str,
        tools: list[dict] | None = None,
        gateway: GatewayRouter | None = None,
        model: str = "claude-sonnet-4-20250514",
        max_turns: int = 5,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ):
        self.name = name
        self.description = description
        self.system_prompt = system_prompt
        self.tools = tools or []
        self._gateway = gateway
        self.model = model
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._openai_tools = None  # P8: 缓存工具转换结果

    async def run(
        self,
        task: str,
        context: dict[str, Any] | None = None,
        tenant_id: str = "",
    ) -> SubAgentResult:
        """
        执行完整的子代理推理循环。

        Args:
            task: 任务描述文本
            context: 可选的上下文信息（会附加到 system prompt）
            tenant_id: 租户 ID（用于 Gateway 预算/路由）

        Returns:
            SubAgentResult: 包含 output、tool_calls、token_usage、duration
        """
        if not self._gateway:
            return SubAgentResult(
                success=False,
                output="",
                error="No gateway configured for this sub-agent",
            )

        start_time = time.monotonic()
        all_tool_calls: list[dict] = []
        total_input_tokens = 0
        total_output_tokens = 0
        final_output = ""

        # 构建 system prompt（可注入上下文）
        system_prompt = self.system_prompt
        if context:
            context_text = "\n".join(f"- {k}: {v}" for k, v in context.items())
            system_prompt = f"{system_prompt}\n\n## Context\n{context_text}"

        # 将 tools 转换为 OpenAI function-calling 格式（缓存结果，P8 修复）
        openai_tools = self._openai_tools
        if openai_tools is None and self.tools:
            openai_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {}),
                    },
                }
                for t in self.tools
            ]
            self._openai_tools = openai_tools

        # Agent loop
        messages: list[ChatMessage] = []
        if system_prompt:
            messages.append(ChatMessage(role="system", content=system_prompt))
        messages.append(ChatMessage(role="user", content=task))

        try:
            for _turn in range(self.max_turns):
                response_content = ""
                tool_calls: list[dict] = []

                async for chunk in self._gateway.chat_stream(
                    messages=messages,
                    model=self.model,
                    tenant_id=tenant_id,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    tools=openai_tools,
                ):
                    # 文本片段
                    if chunk.content:
                        response_content += chunk.content

                    # 工具调用
                    if chunk.tool_calls:
                        for tc in chunk.tool_calls:
                            tool_calls.append(
                                {
                                    "id": tc.id,
                                    "name": tc.name,
                                    "arguments": tc.arguments,
                                }
                            )

                    # Token 用量
                    if chunk.input_tokens:
                        total_input_tokens += chunk.input_tokens
                    if chunk.output_tokens:
                        total_output_tokens += chunk.output_tokens

                    # 完成
                    if chunk.finish_reason in ("stop", "length"):
                        break
                    if chunk.finish_reason == "error":
                        return SubAgentResult(
                            success=False,
                            output=response_content,
                            tool_calls=all_tool_calls,
                            token_usage={
                                "input_tokens": total_input_tokens,
                                "output_tokens": total_output_tokens,
                            },
                            duration=time.monotonic() - start_time,
                            error="LLM returned error",
                        )

                # 收集工具调用
                all_tool_calls.extend(tool_calls)

                # 如果有工具调用，将工具调用结果加入消息继续推理
                if tool_calls:
                    # Assistant 消息（带 tool_calls）
                    assistant_msg = ChatMessage(
                        role="assistant",
                        content=response_content or "",
                        tool_calls=[
                            ToolCall(
                                id=tc["id"], name=tc["name"], arguments=tc["arguments"]
                            )
                            for tc in tool_calls
                        ],
                    )
                    messages.append(assistant_msg)

                    # 工具结果消息（真实执行：走工具注册表沙箱，返回实际结果/错误）
                    from app.tools.context import set_tool_context

                    # S 修复：优先用 context 携带的 user_id（缺省回退 tenant），不再用 tenant 冒充 user
                    _ctx_user = (
                        context.get("user_id") if isinstance(context, dict) else ""
                    )
                    set_tool_context(
                        session_id=(
                            context.get("session_id")
                            if isinstance(context, dict)
                            else ""
                        ),
                        user_id=_ctx_user or tenant_id or "anonymous",
                        tenant_id=tenant_id or "default",
                    )
                    for tc in tool_calls:
                        tool_result = await self._execute_tool(tc)
                        messages.append(
                            ChatMessage(
                                role="tool",
                                content=json.dumps(
                                    tool_result, ensure_ascii=False, default=str
                                ),
                                tool_call_id=tc["id"],
                            )
                        )
                    continue

                # 无工具调用 → 推理完成
                final_output = response_content
                break

            duration = time.monotonic() - start_time
            return SubAgentResult(
                success=True,
                output=final_output,
                tool_calls=all_tool_calls,
                token_usage={
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                },
                duration=duration,
            )

        except Exception as e:
            logger.error("SubAgent '%s' execution error: %s", self.name, e)
            return SubAgentResult(
                success=False,
                output=final_output,
                tool_calls=all_tool_calls,
                token_usage={
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                },
                duration=time.monotonic() - start_time,
                error=str(e),
            )

    async def _execute_tool(self, tc: dict[str, Any]) -> dict[str, Any]:
        """执行一次工具调用（走注册表；含沙箱/SSRF 防护），返回真实结果或错误。"""
        name = tc.get("name", "")
        raw_args = tc.get("arguments", "{}")
        try:
            args = (
                json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            )
        except (json.JSONDecodeError, TypeError):
            args = {"input": str(raw_args)}
        if not isinstance(args, dict):
            args = {"input": args}
        result = await registry.execute(name, args)
        return {"tool": name, **result}


# ── 调度器 ────────────────────────────────────────────────


class AgentDispatcher:
    """
    子代理调度器 — 注册、分发、管理子代理。

    支持同步 dispatch（等待完成）和异步 dispatch_async（返回 task_id）。
    """

    def __init__(
        self,
        gateway: GatewayRouter | None = None,
        tool_registry: ToolRegistry | None = None,
    ):
        self._gateway = gateway
        self._tool_registry = tool_registry
        self._agents: dict[str, SubAgent] = {}
        self._active: dict[str, asyncio.Task] = {}
        self._results: dict[str, SubAgentResult] = {}

    # ── 注册 ──

    def register_agent(
        self,
        name: str,
        description: str,
        system_prompt: str,
        tools: list[dict] | None = None,
        model: str = "claude-sonnet-4-20250514",
        max_turns: int = 5,
    ) -> SubAgent:
        """注册一个命名子代理"""
        agent = SubAgent(
            name=name,
            description=description,
            system_prompt=system_prompt,
            tools=tools,
            gateway=self._gateway,
            model=model,
            max_turns=max_turns,
        )
        self._agents[name] = agent
        logger.info("Registered sub-agent: %s", name)
        return agent

    # ── 同步调度 ──

    async def dispatch(
        self,
        name: str,
        task: str,
        context: dict[str, Any] | None = None,
        tenant_id: str = "",
    ) -> SubAgentResult:
        """同步调度：向命名 agent 派发任务，等待完成"""
        agent = self._agents.get(name)
        if not agent:
            return SubAgentResult(
                success=False,
                output="",
                error=f"Agent '{name}' not found",
            )
        return await agent.run(task, context=context, tenant_id=tenant_id)

    # ── 异步调度 ──

    async def dispatch_async(
        self,
        name: str,
        task: str,
        context: dict[str, Any] | None = None,
        tenant_id: str = "",
    ) -> str:
        """
        异步调度：将任务派发到后台，立即返回 task_id。
        用 get_result(task_id) 获取结果。
        """
        agent = self._agents.get(name)
        if not agent:
            # 存一个失败结果并返回 id
            task_id = str(uuid.uuid4())
            self._results[task_id] = SubAgentResult(
                success=False,
                output="",
                error=f"Agent '{name}' not found",
            )
            self._bound_results()
            return task_id

        task_id = str(uuid.uuid4())

        async def _run_and_store():
            result = await agent.run(task, context=context, tenant_id=tenant_id)
            self._results[task_id] = result
            self._bound_results()
            self._active.pop(task_id, None)

        atask = asyncio.create_task(_run_and_store())
        self._active[task_id] = atask
        return task_id

    # ── 获取异步结果 ──

    async def get_result(self, task_id: str) -> SubAgentResult | None:
        """
        获取异步调度的结果。

        - 如果任务已完成，直接返回
        - 如果任务仍在运行，等待完成后返回
        - 如果 task_id 不存在，返回 None
        """
        # 已完成
        if task_id in self._results:
            return self._results[task_id]

        # 仍在运行 → 等待
        atask = self._active.get(task_id)
        if atask:
            await atask
            return self._results.get(task_id)

        return None

    # ── 查询 ──

    def list_agents(self) -> list[dict]:
        """列出所有已注册的子代理"""
        return [
            {
                "name": agent.name,
                "description": agent.description,
                "tools_count": len(agent.tools),
                "model": agent.model,
            }
            for agent in self._agents.values()
        ]

    def get_agent(self, name: str) -> SubAgent | None:
        """按名称获取子代理"""
        return self._agents.get(name)

    @property
    def active_count(self) -> int:
        """当前正在执行的异步任务数"""
        return len(self._active)

    async def cancel_all(self) -> None:
        """取消所有正在执行的异步任务，等待它们完成"""
        tasks = list(self._active.values())
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()

    def cleanup_results(self, max_age: float = 3600.0) -> int:
        """清理过期异步结果，返回清理数量。

        S 修复：原实现用 result.duration(耗时秒)当时间戳与单调钟比较，恒为假 →
        结果永不被清理、内存无限增长。现改为有界容量淘汰（保留最近 MAX_RESULTS 条）。
        """
        return self._bound_results()

    def _bound_results(self, max_results: int = 500) -> int:
        """将 _results 有界到最近 max_results 条（dict 保持插入序）。"""
        overflow = len(self._results) - max_results
        if overflow <= 0:
            return 0
        for task_id in list(self._results.keys())[:overflow]:
            del self._results[task_id]
        return overflow


# ── 内置代理定义 ──────────────────────────────────────────

BUILTIN_AGENTS: dict[str, dict] = {
    "code": {
        "description": "Code writing and editing agent",
        "system_prompt": (
            "You are a code writing and editing assistant. "
            "Write clean, well-documented code following best practices. "
            "When editing existing code, preserve style and conventions. "
            "Focus on correctness, readability, and maintainability."
        ),
        "max_turns": 10,
    },
    "review": {
        "description": "Code review agent",
        "system_prompt": (
            "You are a code review assistant. "
            "Analyze code for bugs, security issues, performance problems, "
            "and style violations. Provide actionable feedback with specific "
            "suggestions for improvement. Rate severity as critical, warning, "
            "or suggestion."
        ),
        "max_turns": 5,
    },
    "research": {
        "description": "Research and analysis agent",
        "system_prompt": (
            "You are a research and analysis assistant. "
            "Investigate questions thoroughly, gather information from "
            "available sources, and provide well-structured analysis. "
            "Cite your reasoning and provide confidence levels for claims."
        ),
        "max_turns": 5,
    },
    "test": {
        "description": "Test writing agent",
        "system_prompt": (
            "You are a test writing assistant. "
            "Write comprehensive test cases covering happy paths, edge cases, "
            "and error conditions. Use appropriate testing frameworks and "
            "follow the project's existing test conventions. "
            "Aim for high coverage with meaningful assertions."
        ),
        "max_turns": 10,
    },
}


def create_dispatcher_with_builtins(
    gateway: GatewayRouter | None = None,
    tool_registry: ToolRegistry | None = None,
    model: str = "claude-sonnet-4-20250514",
) -> AgentDispatcher:
    """创建预注册了所有内置代理的 AgentDispatcher"""
    dispatcher = AgentDispatcher(gateway=gateway, tool_registry=tool_registry)
    for name, config in BUILTIN_AGENTS.items():
        dispatcher.register_agent(
            name=name,
            description=config["description"],
            system_prompt=config["system_prompt"],
            model=model,
            max_turns=config["max_turns"],
        )
    return dispatcher
