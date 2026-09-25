"""Agent 协同工作台: 多 Agent 并行执行 + 共享上下文

架构设计:
- AgentHub 作为中枢,管理多个 Agent 实例
- 每个 Agent 独立运行 ReAct 循环
- 通过共享 context store (Redis/PG) 交换状态
- Go 网关统一鉴权 + 限流 + trace 追踪

SaaS 安全:
- 租户隔离: 所有 context 携带 tenant_id
- 资源配额: 每租户最多启动 N 个并发 Agent
- 链路追踪: 跨 Agent 请求传递 trace_id
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.agent.runtime import AgentEvent, AgentRuntime, AgentTask, CompactionConfig
from app.gateway.router import GatewayRouter
from app.redis_keys import rkey
from app.trace import record_span

logger = logging.getLogger(__name__)


class AgentRole(StrEnum):
    """Agent 角色定义"""

    RESEARCHER = "researcher"  # 信息收集与研究
    CODER = "coder"  # 代码生成与实现
    REVIEWER = "reviewer"  # 代码审查与测试
    PLANNER = "planner"  # 任务规划与拆解
    ORCHESTRATOR = "orchestrator"  # 编排协调(主 Agent)


@dataclass
class AgentSpec:
    """Agent 角色规格定义"""

    role: AgentRole
    description: str
    system_prompt: str
    max_turns: int = 10
    model: str = "gpt-4o-mini"
    mode: str = "normal"  # 运行模式（normal/minimal/ptc/creative），见 app.agent.modes
    compaction_config: CompactionConfig | None = None  # 逐 agent 截断策略覆盖


@dataclass
class CollaborativeTask:
    """协同任务定义"""

    task_id: str
    original_query: str
    tenant_id: str  # SaaS 安全: 租户隔离
    trace_id: str  # 链路追踪
    subtasks: list[dict[str, Any]]  # [{agent_role, description, dependencies}]
    shared_context: dict[str, Any] = field(default_factory=dict)  # 共享上下文
    status: str = "pending"  # pending/running/completed/failed


class AgentContextStore:
    """共享上下文存储 (SaaS 租户隔离)

    实现方式:
    - Redis (分布式场景): key = "chiron:context:{tenant_id}:{context_id}"
    - 内存 (单实例降级): dict[tenant_id][context_id] = data
    """

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id
        self._local_store: dict[str, dict[str, Any]] = {}
        self._redis_client = None  # Lazy load
        self._redis_available = False  # P7: 标记 Redis 可用性

    async def set(self, context_id: str, data: dict[str, Any], ttl: int = 3600) -> None:
        """设置上下文 (带 TTL 自动过期)"""
        data["_ttl"] = time.time() + ttl
        data["_tenant_id"] = self.tenant_id  # SaaS 安全: 元数据标记

        if self._redis_client:
            await self._redis_client.setex(
                rkey(f"chiron:context:{self.tenant_id}:{context_id}"),
                ttl,
                json.dumps(data, ensure_ascii=False),
            )
        else:
            if not self._redis_available:
                self._redis_available = True
                logger.warning(
                    "AgentContextStore Redis unavailable for tenant %s — "
                    "falling back to in-memory store (data lost on restart, "
                    "multi-instance inconsistent)",
                    self.tenant_id,
                )
            self._local_store[context_id] = data

    async def get(self, context_id: str) -> dict[str, Any] | None:
        """获取上下文"""
        if self._redis_client:
            data = await self._redis_client.get(
                rkey(f"chiron:context:{self.tenant_id}:{context_id}")
            )
            return json.loads(data) if data else None
        else:
            return self._local_store.get(context_id)

    async def delete(self, context_id: str) -> None:
        """删除上下文"""
        if self._redis_client:
            await self._redis_client.delete(
                rkey(f"chiron:context:{self.tenant_id}:{context_id}")
            )
        else:
            self._local_store.pop(context_id, None)

    async def list_keys(self) -> list[str]:
        """列出该租户下的所有 context_id"""
        if self._redis_client:
            # 使用 SCAN 替代 KEYS 避免 O(N) 阻塞 Redis 主线程
            keys = []
            cursor = 0
            pattern = rkey(f"chiron:context:{self.tenant_id}:*")
            while True:
                cursor, batch = await self._redis_client.scan(
                    cursor=cursor, match=pattern, count=100
                )
                keys.extend(batch)
                if cursor == 0:
                    break
            return [k.split(":")[-1] for k in keys]
        else:
            return list(self._local_store.keys())


class AgentHub:
    """Agent 协同中枢

    功能:
    1. 接收复杂任务,拆解为子任务分配给专业 Agent
    2. 管理 Agent 生命周期与并发度
    3. 聚合各 Agent 输出到共享上下文
    4. 统一 trace 记录

    SaaS 安全:
    - 每租户并发 Agent 数限制 (默认 3)
    - 所有 Agent 共享同一 trace_id
    - Context Store 按租户隔离
    """

    # 预定义 Agent 角色配置
    AGENT_ROLES: dict[AgentRole, AgentSpec] = {
        AgentRole.RESEARCHER: AgentSpec(
            role=AgentRole.RESEARCHER,
            description="负责信息搜索、文档分析、知识库检索",
            system_prompt="""你是一个专业的研究助手。请：
1. 仔细分析用户问题,提取关键实体和概念
2. 使用搜索工具获取相关信息
3. 整理并总结发现,注明来源
4. 保留未解决的不确定性""",
            max_turns=15,
        ),
        AgentRole.CODER: AgentSpec(
            role=AgentRole.CODER,
            description="负责代码生成、脚本编写、沙箱执行",
            system_prompt="""你是一个资深程序员。请：
1. 根据需求设计清晰的代码结构
2. 编写可执行、有注释的代码
3. 在沙箱中测试验证
4. 报告执行结果和潜在问题""",
            max_turns=20,
        ),
        AgentRole.REVIEWER: AgentSpec(
            role=AgentRole.REVIEWER,
            description="负责代码审查、测试用例、质量保障",
            system_prompt="""你是一个严格的代码审查员。请：
1. 检查代码的逻辑正确性
2. 识别边界情况和异常处理
3. 建议性能优化
4. 生成测试用例并执行""",
            max_turns=10,
        ),
        AgentRole.PLANNER: AgentSpec(
            role=AgentRole.PLANNER,
            description="负责任务拆解、依赖分析、进度跟踪",
            system_prompt="""你是一个项目规划专家。请：
1. 将复杂任务拆解为可并行的子任务
2. 识别子任务间的依赖关系
3. 制定执行顺序和资源分配
4. 监控进度并动态调整计划""",
            max_turns=5,
        ),
        AgentRole.ORCHESTRATOR: AgentSpec(
            role=AgentRole.ORCHESTRATOR,
            description="负责整体协调、冲突解决、最终汇总",
            system_prompt="""你是协同系统的总编排者。请：
1. 理解用户原始需求,制定高层策略
2. 协调各专业 Agent 的工作
3. 解决子任务间的冲突
4. 聚合输出,生成最终回答
5. 确保质量与一致性""",
            max_turns=10,
        ),
    }

    def __init__(self, gateway: GatewayRouter):
        self.gateway = gateway
        self._runtime_pool: dict[AgentRole, AgentRuntime] = {}
        self._max_concurrent_per_tenant = 3  # SaaS 配额
        self._tenant_running_agents: dict[str, int] = {}  # tenant_id -> count

    async def run_collaborative_task(
        self,
        task: CollaborativeTask,
    ) -> AsyncIterator[AgentEvent]:
        """执行协同任务

        流程:
        1. Planner 拆解任务 (如果尚未拆解)
        2. 并行执行无依赖的子任务
        3. 等待依赖完成
        4. Orchestrator 聚合输出
        """
        import uuid as uuid_mod

        # ── 生成 trace_id (如果不存在) ───────────────────────────────
        trace_id = task.trace_id or uuid_mod.uuid4().hex[:12]

        # ── SaaS 安全: 检查租户并发配额 ─────────────────────────────
        tenant_id = task.tenant_id
        current_count = self._tenant_running_agents.get(tenant_id, 0)
        if current_count >= self._max_concurrent_per_tenant:
            yield AgentEvent(
                type="error",
                content=f"租户并发 Agent 数已达上限 ({self._max_concurrent_per_tenant})",
                trace_id=trace_id,
            )
            return

        self._tenant_running_agents[tenant_id] = current_count + 1

        try:
            # ── 阶段 1: 任务规划 (如果需要) ─────────────────────────────
            if not task.subtasks:
                planner_spec = self.AGENT_ROLES[AgentRole.PLANNER]
                planner_runtime = self._get_or_create_runtime(planner_spec)

                planning_query = f"""请为以下需求拆解任务:
{task.original_query}

请以 JSON 格式返回子任务列表:
[
  {{
    "role": "<role>",
    "description": "<描述>",
    "dependencies": ["<依赖的子任务id>"]
  }}
]"""

                async for event in planner_runtime.run(
                    task=self._make_agent_task(
                        spec=planner_spec,
                        task_id=f"planning_{trace_id}",
                        content=planning_query,
                        tenant_id=tenant_id,
                    ),
                ):
                    event.trace_id = trace_id
                    yield event

                    # 解析 Planner 输出,提取 subtasks
                    if event.type == "text":
                        task.subtasks = self._parse_planner_output(event.content)

            # ── 阶段 2: DAG 调度执行子任务 ───────────────────────────
            task.status = "running"

            async for event in self._execute_subtask_dag(task, trace_id, tenant_id):
                event.trace_id = trace_id
                yield event

            # ── 阶段 3: Orchestrator 聚合 ──────────────────────────────
            orchestrator_spec = self.AGENT_ROLES[AgentRole.ORCHESTRATOR]
            orchestrator_runtime = self._get_or_create_runtime(orchestrator_spec)

            aggregation_query = f"""请根据以下各专业 Agent 的输出,生成最终回答:

【原始需求】
{task.original_query}

【各 Agent 输出】
{json.dumps(task.shared_context, ensure_ascii=False, indent=2)}

请综合整理,提供清晰、准确、完整的回答。"""

            final_event = AgentEvent(
                type="done",
                content="协同任务完成",
                trace_id=trace_id,
            )

            async for event in orchestrator_runtime.run(
                task=self._make_agent_task(
                    spec=orchestrator_spec,
                    task_id=f"aggregation_{trace_id}",
                    content=aggregation_query,
                    tenant_id=tenant_id,
                ),
            ):
                event.trace_id = trace_id
                yield event

            task.status = "completed"
            final_event.trace_id = trace_id
            yield final_event

            logger.info(
                "Collaborative task done (task=%s, trace_id=%s, subtasks=%d)",
                task.task_id,
                trace_id,
                len(task.subtasks),
            )

        finally:
            # 释放配额
            self._tenant_running_agents[tenant_id] = max(
                0, self._tenant_running_agents.get(tenant_id, 1) - 1
            )

    # ── DAG 调度器 ──────────────────────────────────────────────────

    def _topological_waves(self, subtasks: list[dict[str, Any]]) -> list[list[int]]:
        """将子任务按依赖关系拓扑排序为执行波次。

        每一波次内的子任务无互相依赖，可并发执行。
        依赖项引用格式: "subtask_N" (N 为索引)。

        返回 [[idx, ...], [idx, ...], ...]
        """
        n = len(subtasks)
        # 构建 dependency map: idx → set of dependency idx
        deps: dict[int, set[int]] = {}
        for i, st in enumerate(subtasks):
            raw_deps = st.get("dependencies", [])
            dep_indices: set[int] = set()
            for d in raw_deps:
                # 解析 "subtask_N" 格式
                if isinstance(d, str) and d.startswith("subtask_"):
                    try:
                        dep_indices.add(int(d.split("_", 1)[1]))
                    except (ValueError, IndexError):
                        pass
                elif isinstance(d, int):
                    dep_indices.add(d)
            deps[i] = dep_indices

        # Kahn 算法分层
        completed: set[int] = set()
        waves: list[list[int]] = []

        while len(completed) < n:
            # 找出所有依赖已满足的未执行节点
            ready = [
                i
                for i in range(n)
                if i not in completed and deps[i].issubset(completed)
            ]
            if not ready:
                # 依赖环：打破环，按原始顺序执行剩余任务
                remaining = [i for i in range(n) if i not in completed]
                logger.warning(
                    "DAG cycle detected, executing remaining subtasks linearly: %s",
                    remaining,
                )
                waves.append(remaining)
                break
            waves.append(ready)
            completed.update(ready)

        return waves

    async def _execute_subtask_dag(
        self,
        task: CollaborativeTask,
        trace_id: str,
        tenant_id: str,
    ) -> AsyncIterator[AgentEvent]:
        """基于 DAG 依赖关系调度子任务执行。

        拓扑排序为波次，每波内的子任务并发执行（受 Semaphore 限制）。
        每个子任务的实际耗时被记录到 span 中。
        """
        waves = self._topological_waves(task.subtasks)
        sem = asyncio.Semaphore(self._max_concurrent_per_tenant)

        for wave_idx, wave in enumerate(waves):
            # 并发执行当前波次
            event_queue: asyncio.Queue[Any] = asyncio.Queue[Any]()
            running_tasks: list[asyncio.Task[Any]] = []

            for subtask_idx in wave:
                t = asyncio.create_task(
                    self._run_single_subtask(
                        subtask_idx, task, trace_id, tenant_id, sem, event_queue
                    ),
                    name=f"subtask_{subtask_idx}",
                )
                running_tasks.append(t)

            # 边执行边 yield 事件
            done_count = 0
            total = len(running_tasks)
            while done_count < total:
                try:
                    event = await event_queue.get()
                    if event is None:
                        # 某个子任务结束信号
                        done_count += 1
                        continue
                    yield event
                except asyncio.CancelledError:
                    for t in running_tasks:
                        t.cancel()
                    raise

            # 等待所有任务完成（应该已完成）
            await asyncio.gather(*running_tasks, return_exceptions=True)

            logger.debug(
                "DAG wave %d/%d completed (%d subtasks)",
                wave_idx + 1,
                len(waves),
                len(wave),
            )

    async def _run_single_subtask(
        self,
        subtask_idx: int,
        task: CollaborativeTask,
        trace_id: str,
        tenant_id: str,
        sem: asyncio.Semaphore,
        event_queue: asyncio.Queue[Any],
    ) -> None:
        """执行单个子任务，将事件推入队列供调度器 yield。"""
        subtask = task.subtasks[subtask_idx]
        role_str = subtask.get("role", "researcher")
        try:
            role = AgentRole(role_str)
        except ValueError:
            role = AgentRole.RESEARCHER

        spec = self.AGENT_ROLES[role]
        runtime = self._get_or_create_runtime(spec)

        # 注入共享上下文
        context_injection = f"""
【共享上下文】
{json.dumps(task.shared_context.get(role_str, {}), ensure_ascii=False)}

请直接回答问题,不要重复已确认的事实。"""

        sub_query = f"""【子任务 {subtask_idx + 1}/{len(task.subtasks)}】
{subtask['description']}

{context_injection}

原始用户需求: {task.original_query}"""

        start_time = time.time()

        async with sem:
            async for event in runtime.run(
                task=self._make_agent_task(
                    spec=spec,
                    task_id=f"subtask_{subtask_idx}_{trace_id}",
                    content=sub_query,
                    tenant_id=tenant_id,
                ),
            ):
                # 将输出写入共享上下文
                if event.type == "text" and event.content:
                    task.shared_context.setdefault(role_str, {})[
                        f"subtask_{subtask_idx}"
                    ] = event.content

                # 推入事件队列
                await event_queue.put(event)

        # 记录 span（含实际耗时）
        duration_ms = int((time.time() - start_time) * 1000)
        await record_span(
            trace_id=trace_id,
            span_name=f"agent:{role_str}",
            duration_ms=duration_ms,
            metadata={
                "subtask_index": subtask_idx,
                "dependencies": subtask.get("dependencies", []),
                "tenant_id": tenant_id,
            },
            tenant_id=tenant_id,
        )

        # 发送结束信号
        await event_queue.put(None)

    def _get_or_create_runtime(self, spec: AgentSpec) -> AgentRuntime:
        """获取或创建指定角色的 Agent Runtime

        mode/compaction 不在构造期注入——AgentRuntime.run() 按任务
        从 task.llm_config 解析（见 runtime.py: get_mode_config），
        由 _make_agent_task 负责把 spec 的 mode/model/compaction 装进任务。
        """
        if spec.role not in self._runtime_pool:
            from app.agent.runtime import AgentRuntime

            self._runtime_pool[spec.role] = AgentRuntime(gateway=self.gateway)
        return self._runtime_pool[spec.role]

    def _make_agent_task(
        self,
        spec: AgentSpec,
        task_id: str,
        content: str,
        tenant_id: str,
    ) -> AgentTask:
        """按 spec 构造完整的 AgentTask（含 mode/model/compaction 配置注入）。

        历史缺陷修复：此前用 type('obj', ...) 伪造任务对象，缺
        llm_config/user_id/session_id 等属性，runtime.run() 一进入就
        AttributeError；且调用过不存在的 run_single_turn。
        """
        from dataclasses import asdict

        from app.agent.runtime import AgentTask

        llm_config: dict[str, Any] = {"mode": spec.mode, "model": spec.model}
        if spec.compaction_config is not None:
            # 逐 agent 截断策略覆盖（runtime 侧按 llm_config["compaction"] 读取）
            llm_config["compaction"] = asdict(spec.compaction_config)

        return AgentTask(
            id=task_id,
            tenant_id=tenant_id,
            user_id=f"collab:{tenant_id}",
            session_id=f"collab:{task_id}",
            content=content,
            system_prompt=spec.system_prompt,
            llm_config=llm_config,
            max_turns=spec.max_turns,
        )

    def _parse_planner_output(self, output: str) -> list[dict[str, Any]]:
        """解析 Planner 的 JSON 输出"""
        import re

        # 尝试提取 JSON 数组
        match = re.search(r"\[.*\]", output, re.DOTALL)
        if match:
            try:
                parsed: list[dict[str, Any]] = json.loads(match.group())
                return parsed
            except json.JSONDecodeError:
                logger.warning("Failed to parse planner output as JSON")

        # 降级: 返回单任务
        return [{"role": "researcher", "description": output, "dependencies": []}]
