"""工作流引擎增强: 链路追踪 + 运行时参数编辑

功能:
1. 为每个 Workflow Node 执行记录 span (trace)
2. 运行时动态修改节点参数 (租户隔离 state)
3. Pause-Mode 编辑: 暂停执行 → 用户修改 → 恢复执行
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.agent.runtime import AgentEvent
from app.gateway.router import GatewayRouter
from app.trace import record_span
from app.workflow.engine import NodeResult, WorkflowInstance, _build_node_fns, _topological_sort

logger = logging.getLogger(__name__)


# ── 运行时编辑命令 ───────────────────────────────────────────────
@dataclass
class EditCommand:
    """运行时编辑命令"""

    node_id: str
    command: str  # "update_config" / "skip_node" / "replay_node"
    config_updates: dict[str, Any] = field(default_factory=dict)  # {key: new_value}
    tenant_id: str = ""  # SaaS 安全: 租户隔离


@dataclass
class WorkflowEditSession:
    """工作流编辑会话 (SaaS 租户隔离)

    存储运行时编辑状态,带 TTL 自动过期
    """

    session_id: str
    workflow_instance_id: str
    tenant_id: str  # SaaS 安全: 租户隔离
    commands: list[EditCommand] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0  # 0 表示永不过期,默认 1 小时

    def is_expired(self) -> bool:
        if self.expires_at == 0:
            return False
        return time.time() > self.expires_at


# 全局编辑会话存储 (生产环境应使用 Redis)
_edit_sessions: dict[str, WorkflowEditSession] = {}
_EDIT_SESSION_TTL = 3600  # 1 小时


def get_edit_session(session_id: str) -> WorkflowEditSession | None:
    """获取编辑会话"""
    session = _edit_sessions.get(session_id)
    if session and not session.is_expired():
        return session
    elif session:
        # 清理过期会话
        del _edit_sessions[session_id]
    return None


def create_edit_session(
    workflow_instance_id: str,
    tenant_id: str,
    ttl: int = _EDIT_SESSION_TTL,
) -> str:
    """创建编辑会话,返回 session_id"""
    import uuid

    session_id = uuid.uuid4().hex[:12]
    _edit_sessions[session_id] = WorkflowEditSession(
        session_id=session_id,
        workflow_instance_id=workflow_instance_id,
        tenant_id=tenant_id,
        expires_at=time.time() + ttl,
    )
    return session_id


# ── 增强版工作流引擎 ─────────────────────────────────────────────
class TracingWorkflowEngine:
    """带链路追踪的工作流引擎

    增强点:
    1. 每个节点执行前后记录 span
    2. 支持运行时编辑 (通过 EditCommand)
    3. 节点级别重试与降级
    """

    def __init__(self, gateway_router: GatewayRouter) -> None:
        self.gateway = gateway_router

    async def run_workflow_with_trace(
        self,
        graph_json: dict[str, Any],
        initial_state: dict[str, Any],
        tenant_id: str,
        instance_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """运行工作流 (带完整 trace)

        流程:
        1. 拓扑排序节点
        2. 按序执行每个节点
        3. 每节点记录 span (开始/完成/错误)
        4. 检查编辑命令 (Pause-Mode)
        5. 聚合输出到最终事件
        """
        import uuid as uuid_mod

        trace_id = instance_id or uuid_mod.uuid4().hex[:12]
        graph_name = graph_json.get("name", "unnamed_workflow")

        start_time = time.time()

        # ── 生成 Workflow Instance ──────────────────────────────────
        workflow_id = instance_id or f"wf_{trace_id}"
        instance = WorkflowInstance(
            instance_id=workflow_id,
            graph_name=graph_name,
            state=initial_state.copy(),
        )

        yield AgentEvent(
            type="workflow_start",
            content=json.dumps(
                {
                    "workflow_id": workflow_id,
                    "graph_name": graph_name,
                    "node_count": len(graph_json.get("nodes", [])),
                    "trace_id": trace_id,
                    "tenant_id": tenant_id,
                }
            ),
            trace_id=trace_id,
        )

        try:
            # ── 拓扑排序 ──────────────────────────────────────────────
            nodes = graph_json.get("nodes", [])
            edges = graph_json.get("edges", [])
            sorted_nodes = _topological_sort(nodes, edges)

            # ── 构建节点执行函数（闭包，含 node_map / preds / gateway 上下文）──
            node_fns = _build_node_fns(graph_json, self.gateway)

            logger.info(
                "Workflow %s: executing %d nodes in topo order",
                workflow_id,
                len(sorted_nodes),
            )

            # ── 按序执行节点 ──────────────────────────────────────────
            for node_idx, node in enumerate(sorted_nodes):
                node_id = node["id"]
                node_type = node.get("node_type", node.get("type", "default"))
                node_label = node.get("label", node_id)

                # ── 记录节点开始 span ───────────────────────────────────
                node_start = time.time()

                yield AgentEvent(
                    type="workflow_node_start",
                    content=json.dumps(
                        {
                            "node_id": node_id,
                            "node_type": node_type,
                            "node_label": node_label,
                            "index": node_idx,
                            "total": len(sorted_nodes),
                            "trace_id": trace_id,
                        }
                    ),
                    trace_id=trace_id,
                    span_name=f"workflow:{node_label}",
                )

                # ── 检查是否有编辑命令 (Pause-Mode) ────────────────────
                edit_session = self._check_edit_commands(workflow_id, tenant_id)
                if edit_session:
                    for cmd in edit_session.commands:
                        if cmd.node_id == node_id and cmd.command == "update_config":
                            # 应用配置更新
                            current_config = node.get("config", {})
                            node["config"] = {**current_config, **cmd.config_updates}
                            logger.info(
                                "Workflow %s: applied edit for node %s: %s",
                                workflow_id,
                                node_id,
                                cmd.config_updates,
                            )

                    # 清理编辑会话
                    del _edit_sessions[edit_session.session_id]

                # ── 执行节点 ────────────────────────────────────────────
                try:
                    node_result = await self._execute_node_with_trace(
                        node=node,
                        state=instance.state,
                        trace_id=trace_id,
                        node_index=node_idx,
                        node_fns=node_fns,
                        tenant_id=tenant_id,
                    )
                    instance.results[node_id] = node_result
                    # node_result.output 是 JSON 字符串，解析回 dict 后合并进 state
                    if node_result.output:
                        try:
                            out_dict = json.loads(node_result.output)
                            if isinstance(out_dict, dict):
                                instance.state.update(out_dict)
                        except (json.JSONDecodeError, TypeError):
                            pass

                except Exception as e:
                    logger.error(
                        "Workflow %s: node %s failed: %s",
                        workflow_id,
                        node_id,
                        e,
                    )
                    node_result = NodeResult(
                        node_id=node_id,
                        status="error",
                        error=str(e),
                        duration_ms=int((time.time() - node_start) * 1000),
                    )
                    instance.results[node_id] = node_result

                    # 记录错误 span
                    await record_span(
                        trace_id=trace_id,
                        span_name=f"error:{node_label}",
                        duration_ms=node_result.duration_ms,
                        metadata={
                            "node_id": node_id,
                            "node_type": node_type,
                            "error": str(e),
                            "tenant_id": tenant_id,
                        },
                        tenant_id=tenant_id,
                    )

                    yield AgentEvent(
                        type="workflow_node_error",
                        content=json.dumps(
                            {
                                "node_id": node_id,
                                "error": str(e),
                                "trace_id": trace_id,
                            }
                        ),
                        trace_id=trace_id,
                    )

                finally:
                    # ── 记录节点完成 span ─────────────────────────────────
                    node_duration = int((time.time() - node_start) * 1000)

                    await record_span(
                        trace_id=trace_id,
                        span_name=f"node:{node_label}",
                        duration_ms=node_duration,
                        metadata={
                            "node_id": node_id,
                            "node_type": node_type,
                            "status": node_result.status,
                            "index": node_idx,
                            "tenant_id": tenant_id,
                        },
                        tenant_id=tenant_id,
                    )

            # ── 工作流完成 ──────────────────────────────────────────────
            total_duration = int((time.time() - start_time) * 1000)
            instance.status = "completed"
            instance.finished_at = time.time()

            # 构建摘要
            summary = {
                "workflow_id": workflow_id,
                "graph_name": graph_name,
                "total_nodes": len(nodes),
                "completed_nodes": sum(
                    1 for r in instance.results.values() if r.status == "completed"
                ),
                "error_nodes": sum(
                    1 for r in instance.results.values() if r.status == "error"
                ),
                "total_duration_ms": total_duration,
                "trace_id": trace_id,
            }

            yield AgentEvent(
                type="workflow_done",
                content=json.dumps(summary, ensure_ascii=False),
                trace_id=trace_id,
            )

            logger.info(
                "Workflow %s completed (trace_id=%s, duration=%dms, nodes=%d/%d)",
                workflow_id,
                trace_id,
                total_duration,
                summary["completed_nodes"],
                summary["total_nodes"],
            )

        except Exception as e:
            logger.error("Workflow %s failed: %s", workflow_id, e)

            yield AgentEvent(
                type="workflow_error",
                content=f"工作流执行失败: {str(e)}",
                trace_id=trace_id,
            )

    async def _execute_node_with_trace(
        self,
        node: dict[str, Any],
        state: dict[str, Any],
        trace_id: str,
        node_index: int,
        node_fns: dict[str, Any],
        tenant_id: str = "",
    ) -> NodeResult:
        """执行单个节点 (带 trace)

        使用 _build_node_fns 构建的闭包函数（含 node_map / preds / gateway 上下文），
        而非直接导入 engine 模块的闭包局部函数。

        注: 成功 span 由外层 run_workflow_with_trace 的 finally 统一记录
        （带 tenant_id）；此处只记录节点级错误 span（内部捕获不外抛，
        外层 except 不会触发）。
        """
        node_id = node["id"]
        node_type = node.get("node_type", node.get("type", "default"))
        node_label = node.get("label", node_id)

        node_start = time.time()

        # 从 _build_node_fns 结果中获取该节点的执行函数
        executor = node_fns.get(node_id)
        if executor is None:
            raise ValueError(f"Unknown node type: {node_type} (id={node_id})")

        try:
            # 节点函数签名: (state, node_id) -> 增量 dict
            output_dict = await executor(state, node_id)

            return NodeResult(
                node_id=node_id,
                status="completed",
                output=json.dumps(output_dict, ensure_ascii=False),
                duration_ms=int((time.time() - node_start) * 1000),
            )

        except Exception as e:
            node_result = NodeResult(
                node_id=node_id,
                status="error",
                error=str(e),
                duration_ms=int((time.time() - node_start) * 1000),
            )

            # 记录错误 span（tenant 隔离：SaaS 安全，按租户分流）
            await record_span(
                trace_id=trace_id,
                span_name=f"error:{node_label}",
                duration_ms=node_result.duration_ms,
                metadata={
                    "node_type": node_type,
                    "error": str(e),
                },
                tenant_id=tenant_id,
            )

            return node_result

    def _check_edit_commands(
        self,
        workflow_id: str,
        tenant_id: str,
    ) -> WorkflowEditSession | None:
        """检查是否有该工作流的编辑会话"""
        for _session_id, session in list(_edit_sessions.items()):
            if (
                session.workflow_instance_id == workflow_id
                and session.tenant_id == tenant_id
                and not session.is_expired()
            ):
                return session
        return None


# ── 便捷函数 ──────────────────────────────────────────────────────
async def run_workflow_traced(
    graph_json: dict[str, Any],
    initial_state: dict[str, Any],
    tenant_id: str,
    gateway: GatewayRouter,
) -> AsyncIterator[AgentEvent]:
    """便捷函数: 运行带 trace 的工作流"""
    engine = TracingWorkflowEngine(gateway)
    async for event in engine.run_workflow_with_trace(
        graph_json=graph_json,
        initial_state=initial_state,
        tenant_id=tenant_id,
    ):
        yield event
