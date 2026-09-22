"""Agent tools 注册到本地工具注册表。

实现对标 Go `internal/agent/router.go` 注册的 5 个工具：
- agent_dispatch / agent_list / code_agent / agent_session_create / agent_session_list

为保持轻量，当前版本使用内存 registry 与 session 管理；后续可接入 AgentRuntime + DB。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.tools.context import get_session_id
from app.tools.registry import registry


# ── Agent registry ─────────────────────────────────────────────
@dataclass
class AgentInfo:
    name: str
    description: str
    tools: list[str] = field(default_factory=list)


_AGENTS: dict[str, AgentInfo] = {
    "knowledge": AgentInfo(
        name="knowledge",
        description="知识检索与问答代理",
        tools=["search_files", "grep_files", "web_fetch", "read_file"],
    ),
    "tool": AgentInfo(name="tool", description="通用工具代理", tools=[]),
    "browser": AgentInfo(
        name="browser",
        description="浏览器自动化代理",
        tools=[
            "browser_navigate",
            "browser_click",
            "browser_type",
            "browser_read",
            "browser_screenshot",
            "browser_scroll",
            "browser_get_state",
            "browser_tab_list",
            "browser_tab_create",
            "browser_tab_switch",
            "browser_tab_close",
        ],
    ),
}


def _route_agent(task: str) -> str:
    t = task.lower()
    if any(k in t for k in ["浏览器", "browser", "网页", "web自动化"]):
        return "browser"
    if any(k in t for k in ["工具", "tool", "命令", "脚本"]):
        return "tool"
    return "knowledge"


# ── Session store ──────────────────────────────────────────────
@dataclass
class Session:
    id: str
    name: str
    task: str
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    #: 创建它的**对话会话**（空 = 无归属）。
    #: 用于让 agent_session_list 只回本会话的条目 —— 否则主 Agent 会看到别的会话
    #: 建的 agent session，把它们误当成自己的任务上下文（这正是"跨会话影响判断"）。
    root_session_id: str = ""


_sessions: dict[str, Session] = {}
_counter = 0


def _new_session_id() -> str:
    global _counter
    _counter += 1
    return f"session_{int(time.time())}_{_counter}"


# ── Tools ──────────────────────────────────────────────────────
async def agent_dispatch(task: str, agent_type: str = "") -> dict[str, Any]:
    if not task:
        return {"error": "task is required"}
    agent_type = agent_type or _route_agent(task)
    agent = _AGENTS.get(agent_type)
    if agent is None:
        return {"error": f"unknown agent type: {agent_type}"}
    return {
        "output": f"Dispatched task to agent '{agent.name}': {task}",
        "agent_type": agent.name,
        "status": "dispatched",
    }


async def agent_list() -> dict[str, Any]:
    lines = [f"  - {a.name}: {a.description}" for a in _AGENTS.values()]
    return {
        "output": "\n".join(lines),
        "agents": [a.__dict__ for a in _AGENTS.values()],
    }


async def code_agent(
    task: str, code: str = "", file_path: str = "", language: str = "python"
) -> dict[str, Any]:
    if not task:
        return {"error": "task is required"}

    result: dict[str, Any] = {"task": task, "language": language}

    if code:
        from app.tools.registry import registry as tool_reg

        path = file_path or f"/tmp/code_{int(time.time())}.{language}"
        await tool_reg.execute("write_file", {"path": path, "content": code})
        command = f"python {path}" if language == "python" else f"bash {path}"
        exec_res = await tool_reg.execute("shell_exec", {"command": command})
        result.update({"file_path": path, "execution": exec_res})
        return {"output": f"Code agent executed task: {task}", **result}

    return {"output": f"Code agent received task (no code provided): {task}", **result}


async def agent_session_create(name: str, task: str) -> dict[str, Any]:
    if not name or not task:
        return {"error": "name and task are required"}
    sid = _new_session_id()
    _sessions[sid] = Session(
        id=sid, name=name, task=task, root_session_id=get_session_id()
    )
    return {
        "output": f"Session '{name}' created",
        "session_id": sid,
        "status": "pending",
    }


async def agent_session_list() -> dict[str, Any]:
    """只列**本对话会话**创建的 agent session。

    按会话隔离是必须的：此前直接返回进程内全局表的全部条目，于是主 Agent 会看到
    别的会话建的 agent session，把它们当成自己的任务上下文 —— 从而误判该做什么。
    这也决定了前端「最近活动」等入口不会把别人的子 Agent 混进来。
    """
    me = get_session_id()
    mine = [s for s in _sessions.values() if (s.root_session_id or "") == me]
    if not mine:
        return {"output": "No agent sessions.", "sessions": []}
    lines = [f"  - {s.id} [{s.status}] {s.name}: {s.task[:60]}" for s in mine]
    return {
        "output": "\n".join(lines),
        "sessions": [s.__dict__ for s in mine],
    }


# ── 注册 ───────────────────────────────────────────────────────
registry.register(
    name="agent_dispatch",
    description="Dispatch a task to a specialized agent (knowledge/tool/browser).",
    parameters={
        "type": "object",
        "properties": {
            "task": {"type": "string"},
            "agent_type": {"type": "string", "default": ""},
        },
        "required": ["task"],
    },
    handler=agent_dispatch,
)

registry.register(
    name="agent_list",
    description="List available agents.",
    parameters={"type": "object", "properties": {}},
    handler=agent_list,
)

registry.register(
    name="code_agent",
    description="Run a code-oriented agent task (optionally execute provided code).",
    parameters={
        "type": "object",
        "properties": {
            "task": {"type": "string"},
            "code": {"type": "string", "default": ""},
            "file_path": {"type": "string", "default": ""},
            "language": {"type": "string", "default": "python"},
        },
        "required": ["task"],
    },
    handler=code_agent,
)

registry.register(
    name="agent_session_create",
    description="Create an agent session.",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "task": {"type": "string"},
        },
        "required": ["name", "task"],
    },
    handler=agent_session_create,
)

registry.register(
    name="agent_session_list",
    description="List agent sessions.",
    parameters={"type": "object", "properties": {}},
    handler=agent_session_list,
)
