"""统一任务执行处理器 (Unified Task Executor)

整合对话、Agent、工作流的所有能力,通过 TaskRouter 自动编排执行

API 路由:
- POST /v1/chat/submit       提交自然语言任务 (自动编排)
- GET  /v1/chat/sessions     获取会话列表
- GET  /v1/chat/sessions/{id}/messages 获取消息历史
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.agent.workbench_context import (
    context_ids,
    merge_by_quota,
    selected_workflow_ids,
)
from app.core.context_bus import publish_result
from app.core.task_router import TaskPriority, TaskRouter
from app.db import get_pool

logger = logging.getLogger(__name__)


@dataclass
class ChatSession:
    """对话会话"""

    session_id: str
    tenant_id: str
    # 会话归属是用户级的：内存兜底路径也要能校验 user，否则 DB 不可用时会
    # 退化成"同租户内任意用户可读别人会话"。
    user_id: str = ""
    title: str = ""
    mode: str = "auto"
    messages: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = 0

    # 共享上下文 (跨工作台状态)
    shared_context: dict[str, Any] = field(default_factory=dict)


def _dt_to_ts(value: Any) -> float:
    """timestamptz → Unix 秒 (datetime / None / 数值兼容)。"""
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value)


class UnifiedChatHandler:
    """统一聊天处理器 (整合所有工作台能力)

    功能:
    1. 接收用户自然语言输入
    2. 调用 TaskRouter 自动编排执行
    3. 发布结果到 ContextBus
    4. 维护会话历史和共享上下文
    """

    def __init__(self):
        self.sessions: dict[str, ChatSession] = (
            {}
        )  # session_id -> Session (L1 缓存,有界)
        self.router = TaskRouter()
        self._db_warned = False  # DB 降级仅告警一次

    # L1 会话缓存上限(S 资源修复: 原 self.sessions 无界,消息无限 append → 内存线性增长)
    _MAX_L1_SESSIONS = 200

    def _evict_l1_sessions(self) -> None:
        """超出上限时淘汰最久未更新的会话,防止内存无限增长。"""
        while len(self.sessions) > self._MAX_L1_SESSIONS:
            oldest_id = min(self.sessions, key=lambda k: self.sessions[k].updated_at)
            self.sessions.pop(oldest_id, None)

    # ── PostgreSQL 持久化层 (统一任务会话) ──────────────────────────
    # 写策略: 先写库,再更新内存 L1 缓存 (写穿透)。
    # 读策略: 读库为准 (多实例一致),内存仅作 DB 不可用时的兜底。
    # 所有 DB 操作独立 try/except: 池未初始化 / 表缺失 / 连接异常均
    # 降级为纯内存模式并 warn 一次,绝不阻断主任务执行。

    def _warn_db(self, exc: Exception) -> None:
        """DB 不可用时仅告警一次,避免日志刷屏。"""
        if not self._db_warned:
            self._db_warned = True
            logger.warning(
                "Unified session DB unavailable, falling back to in-memory mode: %s",
                exc,
            )

    async def _db_get_session(self, session_id: str) -> dict | None:
        """按 id 读取会话行;DB 不可用或会话不存在返回 None。"""
        try:
            pool = get_pool()
            row = await pool.fetchrow(
                "SELECT id, tenant_id, user_id, title, mode, shared_context, "
                "created_at, updated_at FROM unified_sessions WHERE id = $1",
                session_id,
            )
            return dict(row) if row else None
        except Exception as e:  # noqa: BLE001
            self._warn_db(e)
            return None

    async def _db_ensure_session(
        self,
        session_id: str,
        tenant_id: str,
        user_id: str,
        title: str,
        mode: str,
        shared_context: dict,
    ) -> bool:
        """确保会话行存在 (INSERT ... ON CONFLICT DO NOTHING,幂等)。

        多实例并发创建同一会话时,后到者冲突被忽略,行必然存在。
        返回 True 表示 DB 可用;False 表示降级为内存模式。
        """
        try:
            pool = get_pool()
            await pool.execute(
                "INSERT INTO unified_sessions (id, tenant_id, user_id, title, mode, shared_context) "
                "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (id) DO NOTHING",
                session_id,
                tenant_id,
                user_id,
                title,
                mode,
                dict(shared_context),
            )
            return True
        except Exception as e:  # noqa: BLE001
            self._warn_db(e)
            return False

    async def _db_append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict,
        error: str = "",
    ) -> None:
        """追加一条消息到 unified_messages (写库失败不阻断主流程)。"""
        try:
            pool = get_pool()
            await pool.execute(
                "INSERT INTO unified_messages (session_id, role, content, metadata, error) "
                "VALUES ($1, $2, $3, $4, $5)",
                session_id,
                role,
                content,
                dict(metadata),
                error,
            )
        except Exception as e:  # noqa: BLE001
            self._warn_db(e)

    async def _db_touch_session(
        self,
        session_id: str,
        mode: str,
        shared_context: dict,
    ) -> None:
        """更新会话 mode / shared_context (jsonb 顶层键合并) / updated_at。"""
        try:
            pool = get_pool()
            await pool.execute(
                "UPDATE unified_sessions "
                "SET mode = $2, shared_context = shared_context || $3::jsonb, "
                "updated_at = NOW() WHERE id = $1",
                session_id,
                mode,
                dict(shared_context),
            )
        except Exception as e:  # noqa: BLE001
            self._warn_db(e)

    async def submit_task(
        self,
        user_input: str,
        tenant_id: str,
        session_id: Optional[str] = None,
        mode: str = "auto",  # "auto" / "agent" / "workflow"
        trace_id: str = "",
        context: Optional[dict] = None,
        user_id: str = "",
        llm_config: Optional[dict] = None,  # P1-d：会话运行时状态（模型/provider/模式）
    ) -> dict[str, Any]:
        """提交任务 (自动编排)

        流程:
        1. 创建/获取会话
        2. 调用 TaskRouter 编排执行
        3. 将结果写入共享上下文
        4. 发布结果到 ContextBus
        5. 返回给用户

        Args:
            context: 可选会话上下文 (跨工作台注入):
                - kb_id (str): 知识库 ID,执行前做 RAG 检索,片段拼接到 user_input 前
                - agent (dict): mode=agent 时的 SubAgent 配置覆盖
                  (system_prompt / model / max_turns / name,缺省保持现有默认值)
                - skill_names (list[str]): 透传给 TaskRouter.route_task 的 context,
                  供能力匹配阶段作提示 (不改变现有匹配逻辑)
                - workflow_id (str): mode=workflow 时透传记录到结果 metadata
            user_id: 会话归属用户标识 (Go 网关经 ?user_id=<claims.UserID> 注入;
                缺省以 tenant_id 兜底),持久化到 unified_sessions.user_id。

        持久化: 会话与每轮消息写 PostgreSQL (unified_sessions / unified_messages),
        DB 不可用时降级为内存模式 (见 get_session_messages 同款策略)。
        """
        import uuid

        if not trace_id:
            trace_id = uuid.uuid4().hex[:12]

        context = context or {}
        ctx_agent = (
            context.get("agent") if isinstance(context.get("agent"), dict) else None
        )
        # 只带 agent_id 时按 id 补全配置。SSE 链路由 Go 网关的 resolveAgentContext
        # 补全（internal/api/agents.go:722），而统一链路的 /v1/chat/submit 是直通
        # 代理、不经那道补全 —— 不在这里补，前端"带 Agent 进对话"在统一模式下会
        # 静默退化成"没带 Agent"。
        if not ctx_agent:
            agent_ids = context_ids(context, "agent")
            if agent_ids:
                payload = await load_agent_payload(agent_ids[0], tenant_id, user_id)
                if payload:
                    ctx_agent = payload
                    context["agent"] = payload
                    # Agent 自带的 kb/skill/plugin 绑定提升到顶层（用户显式选择优先）。
                    # 必须在下面解析 kb / workflow 之前做，否则绑定的知识库不生效。
                    apply_agent_bindings(context, payload)

        # 多值优先、单值回退（老客户端与手写 URL 只发单值）。读取口径见
        # app/agent/workbench_context.py —— 三条链路共用同一套，避免"在首页多选
        # 生效、在侧栏多选不生效"这类行为分歧。
        ctx_kb_ids = context_ids(context, "kb")
        # 工作流目标走共享解析函数：SSE 链路（/v1/agent/submit）读的是同一个，
        # 两条链路不会再各自解析出不同结果。
        ctx_workflow_ids = selected_workflow_ids(context)

        # P1-d：会话运行时状态（模式/模型/provider）并入本链路 context ——
        # 前端组装的 context 不含这些字段；并入后 session.shared_context（持久化，
        # 刷新/重开会话不丢）与下游 route_task（意图理解、任务拆解）取同一个口径。
        context = {**(context or {}), "llm_config": dict(llm_config or {})}

        # ── 创建/获取会话 (内存 L1 缓存 + PostgreSQL 写穿透持久化) ──
        if not session_id:
            session_id = f"session_{int(time.time())}_{tenant_id[:8]}"

        session = self.sessions.get(session_id)
        if session is None:
            # 先尝试从 DB 恢复 (多实例场景: 其他实例可能已创建该会话)
            db_row = await self._db_get_session(session_id)
            if db_row is not None:
                session = ChatSession(
                    session_id=session_id,
                    tenant_id=db_row["tenant_id"],
                    user_id=db_row["user_id"] or "",
                    title=db_row["title"] or "",
                    mode=db_row["mode"] or mode,
                    created_at=_dt_to_ts(db_row.get("created_at")),
                    updated_at=_dt_to_ts(db_row.get("updated_at")),
                    shared_context=dict(db_row.get("shared_context") or {}),
                )
            else:
                session = ChatSession(
                    session_id=session_id,
                    tenant_id=tenant_id,
                    # 与写库时的兜底一致（见 _db_ensure_session 的 user_id or tenant_id）
                    user_id=user_id or tenant_id,
                    title=user_input[:50],
                    mode=mode,
                    # 初始 shared_context = 请求注入的跨工作台上下文
                    shared_context=dict(context),
                )
            self.sessions[session_id] = session
            self._evict_l1_sessions()

        session.updated_at = time.time()
        session.mode = mode

        # 写穿透: 确保会话行存在 (幂等; DB 不可用时静默降级为内存模式)
        db_ok = await self._db_ensure_session(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id or tenant_id,
            title=session.title,
            mode=mode,
            shared_context=session.shared_context,
        )

        # 添加用户消息到历史 (先写库,再更新内存缓存)
        session.messages.append(
            {
                "role": "user",
                "content": user_input,
                "timestamp": time.time(),
            }
        )
        if db_ok:
            await self._db_append_message(session_id, "user", user_input, {})

        logger.info(
            "User submitted task (session=%s, tenant=%s, mode=%s, trace_id=%s)",
            session_id,
            tenant_id,
            mode,
            trace_id,
        )

        try:
            # 会话上下文注入: context.kb_id / kb_ids → 执行前 RAG 检索,片段拼接到 user_input 前
            kb_hits = 0
            effective_input = user_input
            if ctx_kb_ids:
                kb_block, kb_hits = await self._retrieve_kb_context(
                    kb_ids=ctx_kb_ids,
                    query=user_input,
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                )
                if kb_block:
                    effective_input = f"{kb_block}\n{user_input}"
                logger.info(
                    "KB context injected (kb_ids=%s, hits=%d, session=%s)",
                    ctx_kb_ids,
                    kb_hits,
                    session_id,
                )

            # 调用 TaskRouter (带模式选择)
            if mode == "agent":
                # 强制使用 Agent 工作台 (context.agent 覆盖 SubAgent 配置)
                result = await self._execute_via_agent(
                    effective_input,
                    tenant_id,
                    trace_id,
                    agent_config=ctx_agent,
                )
            elif mode == "workflow":
                # 强制使用工作流工作台：按选择顺序执行用户自己的工作流（多选即流水线）
                result = await self._execute_via_workflow(
                    effective_input,
                    tenant_id,
                    trace_id,
                    workflow_ids=ctx_workflow_ids,
                )
            else:
                # 自动模式 (默认)
                if context:
                    # context (含 skill_names) 透传给 route_task,供意图/能力匹配阶段参考
                    result = await self.router.route_task(
                        user_input=effective_input,
                        tenant_id=tenant_id,
                        priority=TaskPriority.NORMAL,
                        trace_id=trace_id,
                        context=context,
                    )
                else:
                    result = await self.router.route_task(
                        user_input=effective_input,
                        tenant_id=tenant_id,
                        priority=TaskPriority.NORMAL,
                        trace_id=trace_id,
                    )

            # 提取最终输出
            # Fail loud: 执行器返回 status=error (如 gateway 未初始化) 时,
            # 不得包装成 success=True 响应,必须转入异常路径返回明确错误。
            if result.get("status") == "error":
                output_data = result.get("output")
                error_msg = (
                    output_data.get("error", "execution failed")
                    if isinstance(output_data, dict)
                    else "execution failed"
                )
                raise RuntimeError(str(error_msg))

            final_output = self._extract_output(result)

            # 结果元数据: 基础字段 + 上下文注入的可选字段 (kb_hits/kb_id/workflow_id/agent_name)
            meta: dict[str, Any] = {
                "task_id": result.get("task_id"),
                "duration_ms": result.get("total_duration_ms"),
                "subtasks": result.get("subtasks", []),
            }
            # 多值用 kb_ids / workflow_ids 呈现；同时保留单值的首个，避免既有前端
            # （metadata.kb_id 驱动的知识库引用标签）失效。
            if ctx_kb_ids:
                meta["kb_hits"] = kb_hits
                meta["kb_id"] = ctx_kb_ids[0]
                meta["kb_ids"] = ctx_kb_ids
            workflow_ids = result.get("workflow_ids") or ctx_workflow_ids
            if workflow_ids:
                meta["workflow_id"] = workflow_ids[0]
                meta["workflow_ids"] = workflow_ids
            if mode == "agent":
                meta["agent_name"] = (ctx_agent or {}).get(
                    "name"
                ) or "unified_chat_agent"

            # 添加到会话历史 (assistant 消息 metadata 同带引用/来源字段,供前端展示)
            session.messages.append(
                {
                    "role": "assistant",
                    "content": final_output,
                    "timestamp": time.time(),
                    "metadata": dict(meta),
                }
            )

            # 更新共享上下文 (供后续任务使用)
            session.shared_context["last_output"] = final_output
            session.shared_context["last_trace_id"] = trace_id

            # 写穿透: 落库 assistant 消息 + 更新会话共享上下文
            if db_ok:
                await self._db_append_message(
                    session_id, "assistant", final_output, dict(meta)
                )
                await self._db_touch_session(session_id, mode, session.shared_context)

            # 发布结果到 ContextBus (publish_result 默认即 RESULT_PUBLISH)
            await publish_result(
                topic=f"chat.sessions.{session_id}",
                data={
                    "output": final_output,
                    "task_id": result.get("task_id"),
                    "trace_id": trace_id,
                },
                tenant_id=tenant_id,
            )

            logger.info(
                "Task completed (session=%s, task_id=%s, duration=%dms)",
                session_id,
                result.get("task_id"),
                result.get("total_duration_ms", 0),
            )

            return {
                "success": True,
                "session_id": session_id,
                "trace_id": trace_id,
                "output": final_output,
                "metadata": {
                    **meta,
                    "subtasks_completed": result.get("output", {}).get(
                        "subtasks_completed", 0
                    ),
                },
            }

        except Exception as e:
            logger.error(f"Task execution failed: {e}", exc_info=True)

            # 记录错误到会话历史 (失败同样落库,便于跨实例恢复)
            session.messages.append(
                {
                    "role": "assistant",
                    "content": f"抱歉,执行失败: {str(e)}",
                    "timestamp": time.time(),
                    "error": str(e),
                }
            )
            if db_ok:
                await self._db_append_message(
                    session_id,
                    "assistant",
                    f"抱歉,执行失败: {str(e)}",
                    {},
                    error=str(e),
                )
                await self._db_touch_session(session_id, mode, session.shared_context)

            return {
                "success": False,
                "error": str(e),
                "session_id": session_id,
                "trace_id": trace_id,
            }

    async def _execute_via_agent(
        self,
        user_input: str,
        tenant_id: str,
        trace_id: str,
        agent_config: Optional[dict] = None,
    ) -> dict:
        """通过 Agent 工作台执行

        agent_config (来自 context.agent) 可覆盖 SubAgent 的
        name / system_prompt / model / max_turns,缺省时保持现有默认值。
        """
        from app.agent.multi_agent import SubAgent
        from app.main import get_gateway

        try:
            gateway = await get_gateway()
        except RuntimeError:
            return {
                "status": "error",
                "output": {"error": "LLM gateway not initialized"},
            }

        agent_config = agent_config or {}

        # name 覆盖
        agent_name = str(agent_config.get("name") or "unified_chat_agent")

        # system_prompt 覆盖 (缺省保持现有默认)
        default_prompt = """你是一个多功能 AI 助手。请帮助用户完成各种任务,包括:
1. 数据分析和问题解答
2. 代码生成和调试
3. 文档编写和翻译
4. 知识库检索和信息查询

如果用户需要执行具体操作(如运行 Python 代码),请使用相应的工具。"""
        system_prompt = str(agent_config.get("system_prompt") or default_prompt)

        # model 覆盖 (缺省保持现有默认 gpt-4o-mini)
        model = str(agent_config.get("model") or "gpt-4o-mini")

        # max_turns 覆盖 (缺省保持现有默认 10)
        try:
            max_turns = int(agent_config.get("max_turns", 10) or 10)
        except (TypeError, ValueError):
            max_turns = 10
        if max_turns <= 0:
            max_turns = 10

        # Agent 自带的工具集：与 main.py 的 submit 路径同一语义 —— 非空就只放这些工具
        agent_tools = agent_config.get("tools")
        tools = [t for t in agent_tools if isinstance(t, dict)] if isinstance(agent_tools, list) else None

        agent = SubAgent(
            name=agent_name,
            description="通用助手 Agent",
            system_prompt=system_prompt,
            tools=tools,
            gateway=gateway,
            model=model,
            max_turns=max_turns,
        )

        # SubAgent.run 已实现: 真实调用并透传结果 (绝不返回伪造输出)
        agent_result = await agent.run(task=user_input, tenant_id=tenant_id)
        if not agent_result.success:
            # Fail loud: Agent 执行失败必须向上抛出,由 submit_task 返回 success=False
            raise RuntimeError(
                f"Agent execution failed: {agent_result.error or 'unknown error'}"
            )

        return {
            "status": "completed",
            "output": {
                "result": agent_result.output,
                "tool_calls": agent_result.tool_calls,
            },
            "total_duration_ms": int(agent_result.duration * 1000),
        }

    async def _execute_via_workflow(
        self,
        user_input: str,
        tenant_id: str,
        trace_id: str,
        workflow_ids: list[str] | None = None,
    ) -> dict:
        """通过工作流工作台执行

        用户在对话里选中了工作流时（workbench_context 的 workflow_id / workflow_ids），
        按选择顺序依次执行**用户自己的工作流**，前一个的输出经 initial_state 传给
        后一个 —— 多选即流水线。

        改动前这里把 workflow_id 只当记录用，实际跑的是一个与所选工作流无关的固定
        单节点 LLM 图：用户"带着工作流进对话"，拿到的却是通用问答。没选工作流时仍走
        那条固定图（即原有的默认行为）。
        """
        from app.main import get_gateway

        try:
            gateway = await get_gateway()
        except RuntimeError:
            return {
                "status": "error",
                "output": {"error": "LLM gateway not initialized"},
            }

        selected = [wid.strip() for wid in (workflow_ids or []) if wid and wid.strip()]
        if selected:
            graphs = await self._load_workflows(selected)
            if graphs:
                return await self._run_workflow_chain(
                    graphs, user_input, trace_id, gateway
                )
            # 全部载不到（已删 / 越权 / DB 不可用）：降级为默认图，不阻断对话
            logger.warning(
                "workflow context: none of %s loaded; falling back to default graph",
                selected,
            )

        return await self._run_default_llm_graph(
            user_input,
            trace_id,
            gateway,
            workflow_id=(selected[0] if selected else ""),
        )

    @staticmethod
    def _final_output_of(instance: Any) -> str:
        """取工作流的最终输出（实现见模块级 final_output_of，两条链路共用）。"""
        return final_output_of(instance)

    @staticmethod
    async def _load_workflows(workflow_ids: list[str]) -> list[tuple[str, dict]]:
        """按 id 载入工作流（实现见模块级 load_selected_workflows）。"""
        return await load_selected_workflows(workflow_ids)

    @staticmethod
    async def _run_workflow_chain(
        graphs: list[tuple[str, dict]],
        user_input: str,
        trace_id: str,
        gateway: Any,
    ) -> dict:
        """顺序执行工作流链（实现见模块级 run_workflow_graphs）。"""
        return await run_workflow_graphs(graphs, user_input, trace_id, gateway)

    async def _run_default_llm_graph(
        self,
        user_input: str,
        trace_id: str,
        gateway: Any,
        workflow_id: str = "",
    ) -> dict:
        """未选工作流时的默认路径：固定单节点 LLM 图（改动前的行为）。"""
        from app.workflow.engine import run_workflow

        # 构建简单工作流图: input → llm → output
        graph_json = {
            "name": f"unified_chat_{trace_id}",
            "nodes": [
                {"id": "input_1", "node_type": "input", "label": "用户输入"},
                {
                    "id": "llm_1",
                    "node_type": "llm",
                    "label": "LLM 处理",
                    "config": {
                        "system_prompt": "你是一个多功能 AI 助手,请根据用户输入给出清晰、准确的回答。",
                        "user_message": user_input,
                    },
                },
                {"id": "output_1", "node_type": "output", "label": "输出"},
            ],
            "edges": [
                {"source_id": "input_1", "target_id": "llm_1"},
                {"source_id": "llm_1", "target_id": "output_1"},
            ],
        }

        instance = await run_workflow(
            graph_json=graph_json,
            gateway=gateway,
            initial_state={"input": user_input},
            instance_id=f"wf_{trace_id}",
        )

        if instance.status == "error":
            raise RuntimeError(f"Workflow execution failed: {instance.error}")

        return {
            "status": "completed",
            "output": {
                "result": self._final_output_of(instance),
                "workflow_instance_id": instance.instance_id,
            },
            "workflow_id": workflow_id,
            "total_duration_ms": (
                int((instance.finished_at - instance.started_at) * 1000)
                if instance.finished_at
                else 0
            ),
        }

    async def _retrieve_kb_context(
        self,
        kb_ids: list[str],
        query: str,
        tenant_id: str,
        trace_id: str,
        top_k: int = 5,
        limit: int = 12,
    ) -> tuple[str, int]:
        """基于 context.kb_id / kb_ids 做 RAG 检索 (复用 knowledge:kb_search 的 kb_search 工具)

        多选知识库时逐库检索，再按**轮询配额**合并（workbench_context.merge_by_quota）：
        保证每个库都有代表，而不是让高分库占满名额 —— 不同知识库的 embedding 与索引
        不同，分数本就不可比。limit 是合并后的硬上限，保护多选时的首字节延迟。

        返回 (引用块, 命中数);任何失败 (DB 不可用 / kb 不存在 / 无命中) 都降级为
        空块 + 0,不阻断主任务执行 (与"不传 context 行为一致"的向后兼容原则一致)。
        """
        if not kb_ids:
            return "", 0
        try:
            from app.tools.context import set_tool_context
            from app.tools.kb import kb_search

            # kb_search 依赖工具上下文做归属校验 (与 TaskRouter._execute_single_task 同口径)
            set_tool_context(
                session_id=f"task_{trace_id}" if trace_id else "chat_context_rag",
                user_id=tenant_id,
                tenant_id=tenant_id,
            )

            groups: list[list[str]] = []
            total_hits = 0
            for kb_id in kb_ids:
                result = await kb_search(kb_id=kb_id, query=query, top_k=top_k)
                if not isinstance(result, dict) or "error" in result:
                    # 单个库失败不拖垮其余库：多选场景下不该因为一个坏 id 全盘降级
                    error = result.get("error") if isinstance(result, dict) else result
                    logger.warning(
                        "KB context retrieval skipped (kb_id=%s): %s", kb_id, error
                    )
                    continue
                hits = result.get("results", []) or []
                total_hits += len(hits)
                snippets = [
                    (hit.get("content") or hit.get("name") or "").strip()[:300]
                    for hit in hits[:top_k]
                ]
                snippets = [s for s in snippets if s]
                if snippets:
                    groups.append(snippets)

            merged = merge_by_quota(groups, limit)
            if not merged:
                return "", 0

            block = "【知识库引用】\n" + "\n".join(f"- {s}" for s in merged)
            return block, total_hits

        except Exception as e:  # noqa: BLE001 — 检索失败不阻断主任务
            logger.warning("KB context retrieval failed (kb_ids=%s): %s", kb_ids, e)
            return "", 0

    def _extract_output(self, result: dict) -> str:
        """从执行结果中提取最终输出"""
        output_data = result.get("output", {})

        if isinstance(output_data, dict):
            # 查找最后一个成功的子任务输出
            outputs = output_data.get("outputs", [])
            for output_item in reversed(outputs):
                if output_item.get("output"):
                    return json.dumps(
                        output_item["output"], ensure_ascii=False, indent=2
                    )[:4000]

            # 降级: 返回摘要
            return json.dumps(output_data, ensure_ascii=False, indent=2)[:2000]

        elif isinstance(output_data, str):
            return output_data[:4000]

        else:
            return str(output_data)

    async def get_session_messages(
        self,
        session_id: str,
        tenant_id: str | None = None,
        user_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """获取会话消息历史 (DB 优先,内存兜底)

        S 安全修复:必须提供 tenant_id 并校验会话归属,杜绝跨租户读取消息。

        返回结构保持兼容:
        {success, session_id, messages: [{role, content, timestamp, metadata?, error?}], shared_context}
        timestamp 为 Unix 秒 (由 DB created_at 转换)。
        """
        if not tenant_id:
            return {"success": False, "error": "tenant_id is required"}
        # 只校验 tenant 不够：同租户的其他用户拿到 session_id 就能读别人的消息。
        # 会话归属是用户级的，租户只是它的外边界。
        if not user_id:
            return {"success": False, "error": "user_id is required"}

        # 1) DB 优先: 读库为准,保证多实例一致
        try:
            pool = get_pool()
        except RuntimeError:
            pool = None

        if pool is not None:
            try:
                row = await pool.fetchrow(
                    "SELECT tenant_id, title, mode, shared_context "
                    "FROM unified_sessions "
                    "WHERE id = $1 AND tenant_id = $2 AND user_id = $3",
                    session_id,
                    tenant_id,
                    user_id,
                )
                if row is None:
                    return {"success": False, "error": "Session not found"}
                rows = await pool.fetch(
                    "SELECT role, content, metadata, error, created_at "
                    "FROM unified_messages WHERE session_id = $1 "
                    "ORDER BY created_at, id LIMIT $2",
                    session_id,
                    limit,
                )
                messages: list[dict] = []
                for m in rows:
                    msg: dict[str, Any] = {
                        "role": m["role"],
                        "content": m["content"],
                        "timestamp": _dt_to_ts(m["created_at"]),
                    }
                    if m.get("metadata"):
                        msg["metadata"] = m["metadata"]
                    if m.get("error"):
                        msg["error"] = m["error"]
                    messages.append(msg)
                shared_context = dict(row["shared_context"] or {})
                # 回填 L1 缓存 (DB 不可用时内存兜底仍可读到最新)
                cache = self.sessions.get(session_id)
                if cache is not None:
                    cache.messages = messages
                    cache.shared_context = shared_context
                return {
                    "success": True,
                    "session_id": session_id,
                    "messages": messages,
                    "shared_context": shared_context,
                }
            except Exception as e:  # noqa: BLE001 — 表缺失/连接异常 → 降级内存
                self._warn_db(e)

        # 2) 内存兜底 (DB 不可用,维持旧版行为)
        session = self.sessions.get(session_id)
        # 与 DB 路径保持同一套归属规则：tenant 与 user 都要匹配。
        if (
            not session
            or getattr(session, "tenant_id", None) != tenant_id
            or getattr(session, "user_id", None) != user_id
        ):
            return {"success": False, "error": "Session not found"}

        return {
            "success": True,
            "session_id": session_id,
            "messages": session.messages[-limit:],
            "shared_context": session.shared_context,
        }


# ── 全局单例 ───────────────────────────────────────────────────────
_global_chat_handler: Optional[UnifiedChatHandler] = None


def get_chat_handler() -> UnifiedChatHandler:
    """获取全局聊天处理器 (单例模式)"""
    global _global_chat_handler
    if _global_chat_handler is None:
        _global_chat_handler = UnifiedChatHandler()
    return _global_chat_handler


# ── FastAPI 路由（六大工作台统一对话入口） ──────────────────────────
from fastapi import APIRouter, Request  # noqa: E402

router = APIRouter(tags=["chat"])


# ── 工作流关联目标的载入与执行（模块级：SSE 链路与统一链路共用）─────────────
#
# 这几个函数原为 UnifiedChatHandler 的方法，只有统一链路调用；SSE 链路
# （/v1/agent/submit → AgentRuntime）因此完全没有工作流消费点，"在工作流页点
# 『在对话中使用』"于是变成无报错、无日志的空操作。抽到模块级是为了让两条链路走
# 同一份实现，而不是各写一遍 —— 那正是 workflow_id 在两条链路上行为不一致的成因。


def final_output_of(instance: Any) -> str:
    """取工作流的最终输出。

    引擎把每个节点的增量写进 state 的 ``__out_<node_id>__``，而 dict 保持插入序
    = 节点完成顺序，所以倒序找第一个非空 ``__out_*`` 即拓扑末节点的输出
    （对默认的单节点图同样是 output 节点的结果）。
    """
    state = getattr(instance, "state", {}) or {}
    for key in reversed(list(state.keys())):
        if key.startswith("__out_") and state[key]:
            return str(state[key])
    return ""


async def load_selected_workflows(workflow_ids: list[str]) -> list[tuple[str, dict]]:
    """按 id 载入用户选中的工作流（保持选择顺序；载不到的跳过并记日志）。"""
    loaded: list[tuple[str, dict]] = []
    try:
        pool = get_pool()
    except Exception as exc:  # noqa: BLE001 — DB 不可用降级为默认图
        logger.warning("workflow context: db unavailable: %s", exc)
        return loaded

    for workflow_id in workflow_ids:
        try:
            row = await pool.fetchrow(
                "SELECT id, graph_json FROM workflow_graphs WHERE id = $1",
                workflow_id,
            )
        except Exception as exc:  # noqa: BLE001 — 单个工作流失败不拖垮其余
            logger.warning(
                "workflow context: load failed (id=%s): %s", workflow_id, exc
            )
            continue
        if row is None:
            logger.warning("workflow context: not found (id=%s)", workflow_id)
            continue
        graph = row["graph_json"]
        if isinstance(graph, str):
            try:
                graph = json.loads(graph)
            except json.JSONDecodeError:
                logger.warning(
                    "workflow context: bad graph_json (id=%s)", workflow_id
                )
                continue
        if isinstance(graph, dict) and graph.get("nodes"):
            loaded.append((str(row["id"]), graph))
        else:
            logger.warning("workflow context: empty graph (id=%s)", workflow_id)
    return loaded


async def run_workflow_graphs(
    graphs: list[tuple[str, dict]],
    user_input: str,
    trace_id: str,
    gateway: Any,
) -> dict:
    """顺序执行多个工作流：前一个的输出作为后一个的输入。

    传递用 ``input`` 键 —— 引擎的 input 节点读 ``state["input"]``
    （见 workflow/engine.py 的 _input_node），所以后者能接上前者的产出。
    """
    from app.workflow.engine import run_workflow

    graph_ids = [graph_id for graph_id, _ in graphs]
    combined = user_input
    instance_ids: list[str] = []
    started = time.time()

    for index, (graph_id, graph_json) in enumerate(graphs):
        instance = await run_workflow(
            graph_json=graph_json,
            gateway=gateway,
            initial_state={"input": combined},
            instance_id=f"wf_{trace_id}_{index}",
        )
        instance_ids.append(instance.instance_id)
        if instance.status == "error":
            # 中断即止：后续工作流的输入依赖前者产出，硬跑只会级联出错
            return {
                "status": "error",
                "output": {
                    "error": f"workflow {graph_id} failed: {instance.error}",
                    "workflow_instance_ids": instance_ids,
                },
                "workflow_ids": graph_ids,
            }
        combined = final_output_of(instance) or combined

    return {
        "status": "completed",
        "output": {"result": combined, "workflow_instance_ids": instance_ids},
        "workflow_ids": graph_ids,
        "total_duration_ms": int((time.time() - started) * 1000),
    }


# ── Agent 关联目标的补全（模块级：统一链路专用）─────────────────────────────
#
# SSE 链路（/v1/agent/submit）经 Go 网关的 resolveAgentContext 补全
# （internal/api/agents.go:722）；统一链路的 /v1/chat/submit 是直通代理，不经那道
# 补全，所以必须在这里做 —— 否则前端"带 Agent 进对话"在统一模式下静默退化成
# "没带 Agent"：无报错、无日志，只是人格与工具都没了。


def _decode_json_column(value: Any) -> Any:
    """解析 json / jsonb 列（pgx 对 json 列可能给 str，对 jsonb 可能给 list/dict）。"""
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
    return None


def _string_list(value: Any) -> list[str]:
    """把 jsonb 字符串数组收敛成去空列表（非字符串项丢弃）。"""
    decoded = _decode_json_column(value)
    if not isinstance(decoded, list):
        return []
    return [item.strip() for item in decoded if isinstance(item, str) and item.strip()]


def agent_payload_from_row(row: Any) -> dict[str, Any]:
    """把 agents 表的一行转成 context.agent 的形态。

    字段口径与 Go 网关的 agentContextPayload（internal/api/agents.go:652）一致：
    name / system_prompt / max_turns / model / tools / kb_id / skills / plugins。
    两者必须同步 —— 否则同一条前端请求走不同链路，Agent 的能力会不一样。
    """
    data = dict(row)
    payload: dict[str, Any] = {}

    name = str(data.get("name") or "").strip()
    if name:
        payload["name"] = name
    prompt = str(data.get("system_prompt") or "").strip()
    if prompt:
        payload["system_prompt"] = prompt

    max_turns = data.get("max_turns")
    if isinstance(max_turns, int) and max_turns > 0:
        payload["max_turns"] = max_turns

    # model 存在 llm_config 里（与 Go 侧 agentModel 同口径）
    llm_config = _decode_json_column(data.get("llm_config"))
    if isinstance(llm_config, dict) and llm_config.get("model"):
        payload["model"] = str(llm_config["model"])

    tools = _decode_json_column(data.get("tools"))
    if isinstance(tools, list):
        dict_tools = [t for t in tools if isinstance(t, dict)]
        if dict_tools:
            payload["tools"] = dict_tools

    kb_id = str(data.get("kb_id") or "").strip()
    if kb_id:
        payload["kb_id"] = kb_id
    skills = _string_list(data.get("skills"))
    if skills:
        payload["skills"] = skills
    plugins = _string_list(data.get("plugins"))
    if plugins:
        payload["plugins"] = plugins
    workflows = _string_list(data.get("workflows"))
    if workflows:
        payload["workflows"] = workflows

    return payload


def apply_agent_bindings(context: dict[str, Any], payload: dict[str, Any]) -> None:
    """把 Agent 自带的绑定提升到 context 顶层；已有显式选择时不覆盖。

    与 Go 网关的 applyAgentBindings（internal/api/agents.go）同口径：用户显式带了
    知识库/技能/插件/工作流就以用户的选择为准，Agent 的绑定只补空缺。
    """
    if not context_ids(context, "kb") and payload.get("kb_id"):
        context["kb_id"] = payload["kb_id"]
    if not context.get("skill_names") and payload.get("skills"):
        context["skill_names"] = payload["skills"]
    if not context.get("plugin_names") and payload.get("plugins"):
        context["plugin_names"] = payload["plugins"]
    # workflows 走与 kb 相同的读取口径（多值优先、单值回退）：引擎侧由
    # selected_workflow_ids 消费，_execute_via_workflow 按顺序执行（多选即流水线）
    if not context_ids(context, "workflow") and payload.get("workflows"):
        context["workflow_ids"] = payload["workflows"]


async def load_agent_payload(
    agent_id: str, tenant_id: str, user_id: str
) -> Optional[dict[str, Any]]:
    """按 id 取 Agent 配置；不存在/非本人/DB 不可用时返回 None（不阻断对话）。"""
    if not agent_id.strip():
        return None
    try:
        pool = get_pool()
        row = await pool.fetchrow(
            "SELECT name, system_prompt, max_turns, llm_config, tools, kb_id, skills, plugins, workflows "
            "FROM agents WHERE id::text = $1 AND tenant_id = $2 AND user_id = $3",
            agent_id,
            tenant_id,
            user_id,
        )
    except Exception as exc:  # noqa: BLE001 — 降级为"这次没带 Agent"
        logger.warning("workbench: agent context fallback unavailable: %s", exc)
        return None
    if row is None:
        logger.warning(
            "workbench: agent not found for context fallback (id=%s)", agent_id
        )
        return None
    return agent_payload_from_row(row)


@router.post("/v1/chat/submit")
async def submit_chat(request: Request):
    """统一任务提交入口：TaskRouter 自动编排六大工作台能力

    Body: {message/user_input, tenant_id?, session_id?, mode?, context?}
    context (可选 dict): 跨工作台会话上下文注入 —
        kb_id (str): 执行前 RAG 检索,片段以【知识库引用】拼接到输入前
        agent (dict): mode=agent 时覆盖 SubAgent 配置 (system_prompt/model/max_turns/name)
        skill_names (list[str]): 透传 TaskRouter 作能力匹配提示
        workflow_id (str): mode=workflow 时记录到结果 metadata
    Go 网关代理时追加 ?user_id=<claims.UserID>，作为 tenant_id 兜底。
    """
    body = await request.json()
    user_input = str(body.get("message") or body.get("user_input") or "")
    if not user_input.strip():
        return {"success": False, "error": "message is required"}

    # 多租户隔离:query 的 tenant_id 由 Go 网关从已验证 JWT claims 可信注入,
    # 必须优先于 body 中客户端可伪造的 tenant_id(S 安全修复,防止跨租户)。
    # 两者都存在且不一致时直接拒绝,绝不允许以客户端 body 覆盖可信身份。
    query_tid = str(request.query_params.get("tenant_id") or "")
    body_tid = str(body.get("tenant_id") or "")
    if query_tid and body_tid and query_tid != body_tid:
        return {"success": False, "error": "tenant_id mismatch"}
    tenant_id = query_tid or body_tid
    if not tenant_id:
        return {"success": False, "error": "tenant_id is required"}

    handler = get_chat_handler()
    context = body.get("context")
    if context is not None and not isinstance(context, dict):
        return {"success": False, "error": "context must be an object"}
    # user_id 持久化标识 (Go 网关代理时追加 ?user_id=<claims.UserID>;缺省以 tenant_id 兜底)。
    # 同 tenant_id 逻辑:query 可信优先,防止客户端伪造 user_id 冒充他人。
    query_uid = str(request.query_params.get("user_id") or "")
    body_uid = str(body.get("user_id") or "")
    user_id = query_uid or body_uid
    # P1-d：此前完全忽略 body.llm_config → 界面上切换模型/模式对这条链路
    # （ChatView 的 sendUnified 走的正是它）完全无效。这里读出来交给 ChatHandler
    # 落进会话 shared_context 并透传给 TaskRouter（意图理解等步骤的模型/provider 由此决定）。
    llm_config = body.get("llm_config")
    if not isinstance(llm_config, dict):
        llm_config = {}
    return await handler.submit_task(
        user_input=user_input,
        tenant_id=tenant_id,
        session_id=body.get("session_id"),
        mode=str(body.get("mode", "auto")),
        context=context,
        user_id=user_id,
        llm_config=llm_config,
    )


@router.get("/v1/chat/sessions/{session_id}/messages")
async def get_messages(request: Request, session_id: str, limit: int = 50):
    """获取会话消息历史（含跨工作台共享上下文）

    S 安全修复:强制租户校验。tenant_id 优先取 Go 网关可信注入的
    ?tenant_id= 查询参数,缺省尝试 request.state.tenant_id(中间件设置时)。
    """
    tenant_id = str(request.query_params.get("tenant_id") or "") or str(
        getattr(request.state, "tenant_id", "") or ""
    )
    # 会话归属是用户级的：网关注入的 ?user_id= 与 tenant 一样必须透传下来，
    # 否则同租户内可跨用户读取他人会话消息。
    user_id = str(request.query_params.get("user_id") or "") or str(
        getattr(request.state, "user_id", "") or ""
    )
    handler = get_chat_handler()
    return await handler.get_session_messages(
        session_id, tenant_id=tenant_id, user_id=user_id, limit=limit
    )
