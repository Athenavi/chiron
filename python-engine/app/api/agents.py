"""Agents API endpoints."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.tools.agent import agent_list

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agents"])


@router.get("/v1/agents")
async def list_agents() -> dict[str, Any]:
    """Agent 列表（页面主链路已由 Go 的 DB agents 表提供；此端点保留给工具链）。"""
    return await agent_list()


class AgentDispatchRequest(BaseModel):
    task: str
    agent_type: str = ""
    # 完整 Agent 配置（Go 从 DB agents 表读取后传入）→ SubAgent 真执行
    name: str = ""
    description: str = ""
    system_prompt: str = ""
    tools: list[dict[str, Any]] = []
    model: str = ""
    max_turns: int = 5
    max_tokens: int = 4096
    temperature: float = 0.7
    tenant_id: str = ""
    user_id: str = ""
    session_id: str = ""
    # 工作台绑定（Go 的 agents 表新增列）：Agent 自带的知识库与技能
    kb_id: str = ""
    skills: list[str] = []


@router.post("/v1/agents/dispatch")
async def dispatch_agent(body: AgentDispatchRequest) -> dict[str, Any]:
    """
    派发 Agent 任务。

    - 携带 system_prompt（Go 传入 DB 配置）：用 SubAgent 执行完整 agent loop
      （LLM 流式 + 工具调用 + 多轮），返回真实结果。
    - 否则（工具链调用）：回退到内存 registry 的假派发。
    """
    if body.system_prompt.strip():
        # 标记用户活跃（驱动 MCP 插件轮询范围）
        from app.main import touch_user

        touch_user(body.tenant_id)

        # 延迟导入：避免 app.main ↔ app.api 循环依赖
        from app.agent.multi_agent import SubAgent
        from app.main import get_gateway

        try:
            gateway = await get_gateway()
        except RuntimeError:
            gateway = None
        if gateway is None:
            return {
                "success": False,
                "error": "LLM gateway not initialized",
                "output": "",
            }

        # 工作台绑定：Agent 自带的知识库与技能。
        # SubAgent 只接受一个 system_prompt（没有 skills/rag 注入口），所以先转成文本附上；
        # 这样"给这个 Agent 配好知识库/技能"在派发链路里才真正生效。
        binding = await _workbench_binding(body, gateway)
        system_prompt = f"{body.system_prompt}\n\n{binding}" if binding else body.system_prompt

        agent = SubAgent(
            name=body.name or body.agent_type or "agent",
            description=body.description or "",
            system_prompt=system_prompt,
            tools=body.tools or None,
            gateway=gateway,
            model=body.model or "deepseek-chat",
            max_turns=body.max_turns or 5,
            max_tokens=body.max_tokens or 4096,
            temperature=body.temperature or 0.7,
        )
        result = await agent.run(
            task=body.task,
            context={"session_id": body.session_id} if body.session_id else None,
            tenant_id=body.tenant_id,
        )
        return {
            "success": result.success,
            "output": result.output,
            "error": result.error,
            "tool_calls": result.tool_calls,
            "token_usage": result.token_usage,
            "duration": result.duration,
            "session_id": body.session_id,
        }

    from app.tools.agent import agent_dispatch

    return await agent_dispatch(task=body.task, agent_type=body.agent_type)


async def _workbench_binding(body: AgentDispatchRequest, gateway: Any) -> str:
    """把 Agent 自带的知识库与技能转成 system_prompt 的补充段落。

    SubAgent 只接受一个 system_prompt —— 没有 skills/rag 注入口。所以这里在派发前
    把它们变成文本：技能列成清单（让模型知道有哪些手段），知识库做一次检索并把
    命中片段附上。任何一步失败都只丢那一段，不影响 Agent 正常执行。
    """
    sections: list[str] = []

    if body.skills:
        try:
            from app.skill.store import SkillStore

            store = SkillStore(tenant_id=body.tenant_id, user_id=body.user_id)
            wanted = {s.strip() for s in body.skills if isinstance(s, str) and s.strip()}
            lines = [
                f"- {skill.name}: {skill.description}" if skill.description else f"- {skill.name}"
                for skill in store.list()
                if skill.name in wanted
            ]
            if lines:
                sections.append("## 可用技能\n\n" + "\n".join(lines))
        except Exception as exc:  # pragma: no cover - 降级路径
            logger.warning("agent binding: skill lookup failed: %s", exc)

    if body.kb_id and gateway is not None:
        try:
            from app.rag.builder import RAGBuilder

            builder = RAGBuilder(llm_gateway=gateway)
            results = await builder.query(
                kb_id=body.kb_id,
                query=body.task,
                top_k=3,
                threshold=0.5,
            )
            snippets: list[str] = []
            for doc in results or []:
                snippet = str(doc.get("content", doc.get("text", ""))).strip()
                if not snippet:
                    continue
                if len(snippet) > 500:
                    snippet = snippet[:500] + "…"
                snippets.append(f"- (score {doc.get('score', 0):.2f}) {snippet}")
            if snippets:
                sections.append("## 知识库参考\n\n" + "\n".join(snippets))
        except Exception as exc:  # pragma: no cover - 降级路径
            logger.warning("agent binding: rag query failed: %s", exc)

    return "\n\n".join(sections)


class AgentApprovalRequest(BaseModel):
    """工具审批决策请求（Go 网关 /v1/agent/approval 转发而来）。"""

    tool_call_id: str
    approved: bool
    reason: str = ""
    session_id: str = ""
    user_id: str = ""


@router.post("/v1/agent/approval")
async def submit_agent_approval(body: AgentApprovalRequest) -> dict[str, Any]:
    """处理工具审批决策（三态栅栏"确认"态的回调）。

    多副本语义：优先唤醒**本实例**正在等待的 runtime（零延迟）；若本实例无人等待
    （决策被路由到其它副本），则写 Redis 决策键，由正在等待的副本取走 ——
    这样审批不再依赖会话亲和路由，副本扩缩容期间也能正确送达。
    """
    from app.agent.runtime import submit_approval_global

    if not body.tool_call_id:
        return {"ok": False, "error": "tool_call_id is required"}

    ok = await submit_approval_global(body.tool_call_id, body.approved, body.reason)
    return {
        "ok": ok,
        "tool_call_id": body.tool_call_id,
        "approved": body.approved,
    }
