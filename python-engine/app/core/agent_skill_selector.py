"""Agent Role-based Skill Selection (基于角色的技能自动匹配)

设计目标:
- Agent 根据角色自动选择最优技能
- 支持多角色协同 (Researcher ↔ Coder ↔ Reviewer)
- 动态能力适配 (根据任务上下文调整)

架构思想:
类比操作系统的能力调度器 (Capability Scheduler)
- 角色定义 = 权限矩阵
- 技能匹配 = 资源分配
- 协同协议 = IPC (进程间通信)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.core.capabilities import Capability, WorkstationType, get_registry

logger = logging.getLogger(__name__)


class AgentRole(StrEnum):
    """Agent 角色定义"""

    RESEARCHER = "researcher"  # 信息收集与研究
    CODER = "coder"  # 代码生成与实现
    REVIEWER = "reviewer"  # 代码审查与测试
    PLANNER = "planner"  # 任务规划与拆解
    ORCHESTRATOR = "orchestrator"  # 编排协调 (主 Agent)
    ANALYST = "analyst"  # 数据分析与可视化
    TECHNICIAN = "technician"  # 技术执行 (文件操作/Shell)
    DOCUMENTER = "documenter"  # 文档编写


@dataclass
class RoleProfile:
    """角色档案

    定义 Agent 的能力范围、偏好技能、协作规则
    """

    role_id: str  # "{role}" (预定义角色)
    name: str  # 显示名称
    description: str  # 角色描述

    # 权限矩阵
    allowed_workstations: list[WorkstationType] = field(default_factory=list)
    preferred_capabilities: list[str] = field(default_factory=list)  # 优先使用的能力 ID
    forbidden_capabilities: list[str] = field(default_factory=list)  # 禁止使用的能力

    # 协作规则
    can_collaborate_with: list[AgentRole] = field(default_factory=list)
    leader_roles: list[AgentRole] = field(default_factory=list)  # 可领导的角色
    follower_roles: list[AgentRole] = field(default_factory=list)  # 可领导的下属角色

    # 元数据
    version: str = "1.0.0"
    tags: list[str] = field(default_factory=list)


@dataclass
class AgentContext:
    """Agent 执行上下文

    包含任务执行所需的状态、共享数据、历史决策
    """

    agent_id: str
    role: AgentRole
    tenant_id: str
    task_description: str

    # 状态存储
    memory: dict[str, Any] = field(default_factory=dict)
    shared_data: dict[str, Any] = field(default_factory=dict)  # 与其他 Agent 共享的数据

    # 执行历史
    execution_log: list[dict] = field(default_factory=list)

    # 当前任务
    current_task: str | None = None
    status: str = "pending"  # pending/running/completed/error

    # 时间戳
    created_at: float = field(default_factory=time.time)
    updated_at: float = 0


class RoleSkillMatcher:
    """角色-技能匹配器

    功能:
    1. 根据角色快速筛选可用技能
    2. 基于任务上下文智能推荐技能
    3. 动态评分与排序 (基于历史成功率、耗时等)
    """

    def __init__(self):
        self.registry = get_registry()
        self._roles: dict[str, RoleProfile] = {}
        self._index_by_tags: dict[str, list[str]] = {}

    async def register_role(self, profile: RoleProfile) -> None:
        """注册角色档案"""
        self._roles[profile.role_id] = profile

        # 更新索引
        for tag in profile.tags:
            self._index_by_tags.setdefault(tag, []).append(profile.role_id)

        logger.info(f"Registered role profile: {profile.role_id} ({profile.name})")

    async def get_role_profile(self, role: AgentRole) -> RoleProfile | None:
        """获取角色档案"""
        return self._roles.get(role.value)

    async def match_skills(
        self,
        role: AgentRole,
        task_description: str,
        tenant_id: str,
        top_k: int = 5,
    ) -> list[Capability]:
        """根据角色和任务描述匹配最优技能

        匹配策略:
        1. 从角色档案中获取偏好技能列表
        2. 精确匹配偏好技能
        3. 语义搜索补充 (基于任务描述)
        4. 按成功率、耗时排序
        """
        profile = await self.get_role_profile(role)
        if not profile:
            logger.warning(f"Role profile not found: {role}")
            return []

        matched_caps = []

        # ── 策略 1: 精确匹配偏好技能 ─────────────────────────
        for cap_id in profile.preferred_capabilities:
            cap = await self.registry.get_by_id(cap_id, tenant_id)
            if cap:
                matched_caps.append(cap)

        # ── 策略 2: 语义搜索补充 ─────────────────────────────
        query = task_description

        caps = await self.registry.search(
            query=query,
            workstation_type=(
                profile.allowed_workstations[0]
                if profile.allowed_workstations
                else None
            ),
            limit=top_k * 2,
        )

        # 过滤掉已匹配的
        existing_ids = {c.capability_id for c in matched_caps}
        for cap in caps:
            if cap.capability_id not in existing_ids:
                # 检查是否在禁止列表中
                if cap.capability_id not in profile.forbidden_capabilities:
                    matched_caps.append(cap)

        # ── 排序: 按使用次数 (热度) ──────────────────────────
        matched_caps.sort(key=lambda c: c.usage_count, reverse=True)

        return matched_caps[:top_k]

    async def recommend_skills_for_task(
        self,
        task_description: str,
        tenant_id: str,
        available_roles: list[AgentRole] = None,
        top_k: int = 10,
    ) -> dict[str, Any]:
        """为复杂任务推荐多角色协同方案

        Returns:
            {
                "recommended_roles": [AgentRole, ...],
                "role_skill_mapping": {
                    "researcher": [Capability, ...],
                    "coder": [Capability, ...],
                },
                "coordination_plan": {...}
            }
        """
        # 如果没有指定角色列表,自动推断
        if not available_roles:
            available_roles = await self._infer_roles(task_description)

        result = {
            "recommended_roles": available_roles,
            "role_skill_mapping": {},
            "coordination_plan": {},
        }

        # 为每个角色匹配技能
        for role in available_roles:
            skills = await self.match_skills(role, task_description, tenant_id, top_k=3)
            result["role_skill_mapping"][role.value] = [
                {
                    "capability_id": cap.capability_id,
                    "name": cap.name,
                    "usage_count": cap.usage_count,
                }
                for cap in skills
            ]

        # 生成协同计划
        result["coordination_plan"] = await self._generate_coordination_plan(
            available_roles, result["role_skill_mapping"]
        )

        return result

    async def _infer_roles(self, task_description: str) -> list[AgentRole]:
        """根据任务描述推断需要的角色

        简化版: 关键词匹配 (生产环境应使用 LLM)
        """
        text = task_description.lower()
        roles = []

        if any(keyword in text for keyword in ["分析", "analyze", "报告", "report"]):
            roles.append(AgentRole.ANALYST)

        if any(keyword in text for keyword in ["代码", "code", "写", "implement"]):
            roles.append(AgentRole.CODER)
            roles.append(AgentRole.REVIEWER)

        if any(keyword in text for keyword in ["研究", "research", "搜索", "search"]):
            roles.append(AgentRole.RESEARCHER)

        if any(keyword in text for keyword in ["计划", "plan", "规划", "拆解"]):
            roles.append(AgentRole.PLANNER)

        if any(keyword in text for keyword in ["生成", "generate", "创建", "create"]):
            roles.append(AgentRole.DOCUMENTER)

        # 默认使用 Orchestrator
        if not roles:
            roles.append(AgentRole.ORCHESTRATOR)

        return roles

    async def _generate_coordination_plan(
        self,
        roles: list[AgentRole],
        skill_mapping: dict[str, list],
    ) -> dict:
        """生成协同计划 (DAG)"""
        plan = {
            "execution_order": [],
            "dependencies": {},
        }

        # 简单线性计划 (后续可扩展为 DAG)
        for idx, role in enumerate(roles):
            role_key = role.value
            plan["execution_order"].append(role_key)

            # 依赖前一个角色完成
            if idx > 0:
                plan["dependencies"][role_key] = [roles[idx - 1].value]
            else:
                plan["dependencies"][role_key] = []

        return plan

    async def preload_default_roles(self) -> None:
        """预加载默认角色档案"""

        # ── Researcher 角色 ──────────────────────────────────
        await self.register_role(
            RoleProfile(
                role_id=AgentRole.RESEARCHER.value,
                name="研究员",
                description="负责信息收集、研究、资料整理",
                allowed_workstations=[WorkstationType.KNOWLEDGE, WorkstationType.SKILL],
                preferred_capabilities=[
                    "knowledge:kb_search",
                    "skill:read_file",
                    "skill:grep_files",
                ],
                tags=["研究", "信息收集", "搜索"],
                can_collaborate_with=[
                    AgentRole.PLANNER,
                    AgentRole.ANALYST,
                    AgentRole.CODER,
                ],
                leader_roles=[AgentRole.PLANNER],
            )
        )

        # ── Coder 角色 ────────────────────────────────────────
        await self.register_role(
            RoleProfile(
                role_id=AgentRole.CODER.value,
                name="编码者",
                description="负责代码生成、脚本编写、功能实现",
                allowed_workstations=[WorkstationType.SKILL, WorkstationType.WORKFLOW],
                preferred_capabilities=["skill:execute_python", "skill:write_file"],
                forbidden_capabilities=["knowledge:kb_search"],  # 不需要知识库检索
                tags=["编码", "开发", "Python"],
                can_collaborate_with=[
                    AgentRole.REVIEWER,
                    AgentRole.TECHNICIAN,
                ],
                leader_roles=[AgentRole.REVIEWER],
                follower_roles=[AgentRole.TECHNICIAN],
            )
        )

        # ── Reviewer 角色 ────────────────────────────────────
        await self.register_role(
            RoleProfile(
                role_id=AgentRole.REVIEWER.value,
                name="审查者",
                description="负责代码审查、测试验证、质量保证",
                allowed_workstations=[WorkstationType.SKILL],
                preferred_capabilities=["skill:execute_python", "skill:test_runner"],
                tags=["审查", "测试", "质量"],
                can_collaborate_with=[
                    AgentRole.CODER,
                    AgentRole.ANALYST,
                ],
                leader_roles=[AgentRole.CODER],
            )
        )

        # ── Planner 角色 ─────────────────────────────────────
        await self.register_role(
            RoleProfile(
                role_id=AgentRole.PLANNER.value,
                name="规划师",
                description="负责任务规划、拆解、编排",
                allowed_workstations=[WorkstationType.WORKFLOW, WorkstationType.AGENT],
                preferred_capabilities=["workflow:build_dag", "agent:orchestrator"],
                tags=["规划", "编排", "拆解"],
                can_collaborate_with=[
                    AgentRole.RESEARCHER,
                    AgentRole.ANALYST,
                    AgentRole.ORCHESTRATOR,
                ],
                leader_roles=[
                    AgentRole.RESEARCHER,
                    AgentRole.CODER,
                    AgentRole.REVIEWER,
                ],
                follower_roles=[
                    AgentRole.RESEARCHER,
                    AgentRole.CODER,
                    AgentRole.REVIEWER,
                ],
            )
        )

        # ── Orchestrator 角色 ───────────────────────────────
        await self.register_role(
            RoleProfile(
                role_id=AgentRole.ORCHESTRATOR.value,
                name="编排者",
                description="负责整体协调、决策、冲突解决",
                allowed_workstations=[
                    WorkstationType.DIALOGUE,
                    WorkstationType.AGENT,
                    WorkstationType.WORKFLOW,
                ],
                preferred_capabilities=[],  # 不偏好具体技能,统一调度其他角色
                tags=["协调", "编排", "管理"],
                can_collaborate_with=[
                    AgentRole.PLANNER,
                    AgentRole.ANALYST,
                ],
                leader_roles=[
                    AgentRole.PLANNER,
                    AgentRole.RESEARCHER,
                    AgentRole.CODER,
                    AgentRole.REVIEWER,
                ],
            )
        )

        # ── Analyst 角色 ─────────────────────────────────────
        await self.register_role(
            RoleProfile(
                role_id=AgentRole.ANALYST.value,
                name="分析师",
                description="负责数据分析、可视化、报告生成",
                allowed_workstations=[WorkstationType.SKILL, WorkstationType.KNOWLEDGE],
                preferred_capabilities=["skill:execute_python", "knowledge:kb_search"],
                tags=["分析", "数据", "可视化"],
                can_collaborate_with=[
                    AgentRole.RESEARCHER,
                    AgentRole.DOCUMENTER,
                ],
                leader_roles=[AgentRole.RESEARCHER],
            )
        )

        # ── Technician 角色 ──────────────────────────────────
        await self.register_role(
            RoleProfile(
                role_id=AgentRole.TECHNICIAN.value,
                name="技术员",
                description="负责文件操作、Shell 命令、环境配置",
                allowed_workstations=[WorkstationType.SKILL],
                preferred_capabilities=[
                    "skill:read_file",
                    "skill:write_file",
                    "skill:shell_exec",
                ],
                tags=["技术", "执行", "文件操作"],
                can_collaborate_with=[
                    AgentRole.CODER,
                    AgentRole.REVIEWER,
                ],
                follower_roles=[AgentRole.CODER],
            )
        )

        # ── Documenter 角色 ─────────────────────────────────
        await self.register_role(
            RoleProfile(
                role_id=AgentRole.DOCUMENTER.value,
                name="文档编写者",
                description="负责技术文档、用户手册、API 文档编写",
                allowed_workstations=[WorkstationType.SKILL, WorkstationType.KNOWLEDGE],
                preferred_capabilities=["skill:write_file", "knowledge:index_document"],
                tags=["文档", "写作", "技术写作"],
                can_collaborate_with=[
                    AgentRole.ANALYST,
                    AgentRole.RESEARCHER,
                ],
            )
        )

        logger.info(f"Preloaded {len(self._roles)} default role profiles")


# ── 全局单例 ────────────────────────────────────────────────────────
_global_matcher: RoleSkillMatcher | None = None


def get_matcher() -> RoleSkillMatcher:
    """获取角色-技能匹配器单例"""
    global _global_matcher

    if _global_matcher is None:
        _global_matcher = RoleSkillMatcher()

    return _global_matcher


async def init_matcher() -> RoleSkillMatcher:
    """初始化角色-技能匹配器 (预加载角色档案)"""
    matcher = get_matcher()
    await matcher.preload_default_roles()
    return matcher


async def auto_select_skills(
    task_description: str,
    tenant_id: str,
) -> dict[str, Any]:
    """自动选择技能 (便捷函数)

    Args:
        task_description: 任务描述
        tenant_id: 租户 ID

    Returns:
        角色-技能映射 + 协同计划
    """
    matcher = get_matcher()
    return await matcher.recommend_skills_for_task(
        task_description=task_description,
        tenant_id=tenant_id,
    )
