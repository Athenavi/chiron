"""工作流节点动态绑定 Skill/MCP (WorkflowNode Skill Binding)

功能:
- 工作流节点支持动态绑定任意已注册能力
- 运行时从 Capabilities Registry 查询可用 Skill
- 支持节点级参数热更新
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.capabilities import Capability, WorkstationType, get_registry

logger = logging.getLogger(__name__)


@dataclass
class BoundSkill:
    """绑定的技能"""

    capability_id: str
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    input_mapping: dict[str, str] = field(
        default_factory=dict
    )  # {"node_output_key": "skill_input_param"}
    output_mapping: dict[str, str] = field(
        default_factory=dict
    )  # {"skill_output_key": "next_node_input_key"}


class WorkflowNodeWithSkill:
    """带技能绑定的工作流节点

    增强原版 Node,支持动态绑定任意 Skill
    """

    def __init__(
        self,
        node_id: str,
        node_type: str,
        label: str,
        bound_skill_id: str | None = None,
        skill_config: dict[str, Any] | None = None,
    ) -> None:
        self.node_id = node_id
        self.node_type = (
            node_type  # "input" / "llm" / "tool" / "condition" / "output" / "skill"
        )
        self.label = label

        # 技能绑定
        self.bound_skill: BoundSkill | None = None
        if bound_skill_id:
            self.bound_skill = BoundSkill(
                capability_id=bound_skill_id,
                name=bound_skill_id.split(":")[-1],
                config=skill_config or {},
            )

        self.execution_count = 0
        self.last_execution_time = 0.0
        self.last_status: str = "pending"  # pending/running/completed/error

    async def execute_with_skill(
        self,
        state: dict[str, Any],
        tenant_id: str,
        trace_id: str = "",
    ) -> dict[str, Any]:
        """使用绑定的技能执行节点

        流程:
        1. 从 state 提取输入 (应用 input_mapping)
        2. 调用能力注册中心查找 executor
        3. 执行并获取输出
        4. 应用 output_mapping 注入到 state
        """
        from app.core.capabilities import get_registry

        if not self.bound_skill:
            raise ValueError(f"No skill bound to node {self.node_id}")

        registry = get_registry()
        cap = await registry.get_by_id(self.bound_skill.capability_id, tenant_id)

        if not cap or not cap._executor:
            raise ValueError(
                f"Capability not found or no executor: {self.bound_skill.capability_id}"
            )

        # 提取输入
        input_params = {}
        for out_key, param_name in self.bound_skill.input_mapping.items():
            if out_key in state:
                input_params[param_name] = state[out_key]

        # 合并显式配置
        input_params.update(self.bound_skill.config)

        # 执行
        start_time = time.time()
        try:
            if callable(cap._executor):
                import asyncio

                if asyncio.iscoroutinefunction(cap._executor):
                    output = await cap._executor(**input_params)
                else:
                    output = cap._executor(**input_params)
            else:
                raise ValueError("Executor is not callable")

            duration_ms = int((time.time() - start_time) * 1000)

            self.execution_count += 1
            self.last_execution_time = time.time()
            self.last_status = "completed"

            # 应用输出映射
            result = {}
            for skill_key, state_key in self.bound_skill.output_mapping.items():
                if skill_key in output:
                    result[state_key] = output[skill_key]

            # 如果没有映射,直接返回完整输出
            if not self.bound_skill.output_mapping:
                result[f"__out_{self.node_id}__"] = output

            logger.info(
                "Node %s executed with skill %s (duration=%dms)",
                self.node_id,
                self.bound_skill.capability_id,
                duration_ms,
            )

            return result

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)

            self.execution_count += 1
            self.last_execution_time = time.time()
            self.last_status = "error"

            logger.error(f"Node {self.node_id} execution failed: {e}")

            return {
                f"__out_{self.node_id}__": {
                    "error": str(e),
                    "duration_ms": duration_ms,
                }
            }


# ── 增强的工作流引擎 (支持动态节点) ────────────────────────────────
class DynamicWorkflowEngine:
    """动态工作流引擎 (支持节点绑定任意 Skill)

    用法:
    1. 创建 WorkflowNodeWithSkill 实例
    2. 添加到工作流图中
    3. 执行时自动调用绑定的 Skill
    """

    def __init__(self) -> None:
        self.nodes: dict[str, WorkflowNodeWithSkill] = {}
        self.registry = get_registry()

    def add_node(
        self,
        node_id: str,
        node_type: str,
        label: str,
        bound_skill_id: str | None = None,
        skill_config: dict[str, Any] | None = None,
    ) -> WorkflowNodeWithSkill:
        """添加节点 (可选绑定 Skill)"""
        node = WorkflowNodeWithSkill(
            node_id=node_id,
            node_type=node_type,
            label=label,
            bound_skill_id=bound_skill_id,
            skill_config=skill_config,
        )
        self.nodes[node_id] = node
        return node

    async def discover_and_bind_skills(
        self,
        node_type: str = "skill",
        tenant_id: str = "",
    ) -> list[Capability]:
        """自动发现并推荐可用 Skills"""
        caps = await self.registry.list_by_workstation(
            workstation_type=WorkstationType.SKILL,
            tenant_id=tenant_id,
        )

        return caps
