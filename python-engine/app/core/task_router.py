"""跨工作台调度器 (TaskRouter)

设计目标:
- 接收自然语言任务,自动分解为子任务
- 根据 Capabilities Registry 动态匹配最优执行路径
- 编排多工作台协同执行 (对话 → Agent → 工作流 → 技能 → 知识库)
- 统一异常处理与错误恢复

核心思想:
类比操作系统的进程调度器 + 微服务网关
- 能力发现 → 任务拆解 → DAG 构建 → 执行编排 → 结果聚合
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.core.capabilities import WorkstationType, get_registry

logger = logging.getLogger(__name__)


class TaskPriority(StrEnum):
    """任务优先级"""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class TaskStatus(StrEnum):
    """已执行任务的终态。"""

    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class SubTask:
    """子任务"""

    subtask_id: str
    description: str
    capability_id: str  # 匹配到的能力 ID
    parameters: dict[str, Any] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)  # 依赖的其他 subtask_id
    tags: list[str] = field(default_factory=list)  # 意图关键词 (能力语义匹配用)
    workstation_type: WorkstationType | None = None
    status: str = "pending"  # pending/running/completed/failed


@dataclass
class ExecutedTask:
    """执行的任务 (含结果)"""

    task_id: str
    subtask_id: str
    capability_id: str
    input_params: dict[str, Any]
    output: Any = None
    error: str = ""
    duration_ms: int = 0
    status: TaskStatus = TaskStatus.COMPLETED


class TaskRouter:
    """跨工作台任务路由器

    核心流程:
    1. Intent Understanding → 理解用户意图
    2. Task Decomposition → 拆解为子任务
    3. Capability Matching → 为每个子任务匹配能力
    4. DAG Construction → 构建执行依赖图
    5. Execution Planning → 生成执行计划 (并行优化)
    6. Result Aggregation → 聚合输出
    """

    def __init__(self) -> None:
        self.registry = get_registry()
        self._execution_history: list[ExecutedTask] = []  # 最近 100 条

    async def route_task(
        self,
        user_input: str,
        tenant_id: str,
        context: dict[str, Any] | None = None,
        priority: TaskPriority = TaskPriority.NORMAL,
        trace_id: str = "",
    ) -> dict[str, Any]:
        """路由并执行任务 (完整流程)

        Args:
            user_input: 用户自然语言输入
            tenant_id: 租户 ID
            context: 上下文 (包含历史对话、共享状态)
            priority: 优先级
            trace_id: 链路追踪 ID

        Returns:
            {
                "task_id": "xxx",
                "status": "completed",
                "output": {...},
                "subtasks": [...],
                "trace_id": "xxx",
                "duration_ms": 1234,
            }
        """
        start_time = time.time()
        task_id = f"task_{int(time.time())}_{tenant_id[:8]}"

        logger.info(
            "Routing task (task_id=%s, tenant=%s, priority=%s, trace_id=%s)",
            task_id,
            tenant_id,
            priority.value,
            trace_id,
        )

        try:
            # ── 阶段 1: 意图理解 ─────────────────────────────────────
            # 会话运行时状态（P1-d）：统一链路的模型/provider 选择在此进入意图理解。
            # 此前这里没有传，于是意图理解恒用写死的 gpt-4o-mini。
            llm_config = (context or {}).get("llm_config") or {}
            intent = await self._understand_intent(
                user_input,
                context or {},
                model=str(llm_config.get("model") or ""),
                provider_hint=str(llm_config.get("provider") or ""),
                tenant_id=tenant_id,
            )

            # ── 阶段 2: 任务拆解 ─────────────────────────────────────
            subtasks = await self._decompose_task(intent, tenant_id, user_input)

            if not subtasks:
                return {
                    "task_id": task_id,
                    "status": "no_match",
                    "output": {"error": "未找到匹配的能力"},
                    "trace_id": trace_id,
                    "duration_ms": int((time.time() - start_time) * 1000),
                }

            # ── 阶段 3: 能力匹配 & DAG 构建 ─────────────────────────
            matched_tasks = await self._match_capabilities(subtasks, tenant_id)

            # ── 阶段 4: 执行计划生成 (拓扑排序) ─────────────────────
            execution_order = self._topological_sort(matched_tasks)

            # ── 阶段 5: 执行 (按序,支持并行) ────────────────────────
            results = await self._execute_tasks(
                execution_order,
                tenant_id=tenant_id,
                trace_id=trace_id,
            )

            # ── 阶段 6: 结果聚合 ─────────────────────────────────────
            final_output = await self._aggregate_results(results, intent, user_input)

            total_duration = int((time.time() - start_time) * 1000)

            result = {
                "task_id": task_id,
                "status": "completed",
                "output": final_output,
                "subtasks": [
                    {
                        "subtask_id": t.subtask_id,
                        "capability_id": t.capability_id,
                        "status": t.status,
                        "duration_ms": t.duration_ms,
                    }
                    for t in results
                ],
                "trace_id": trace_id,
                "total_duration_ms": total_duration,
                "priority": priority.value,
            }

            logger.info(
                "Task completed (task_id=%s, subtasks=%d, duration=%dms)",
                task_id,
                len(results),
                total_duration,
            )

            return result

        except Exception as e:
            logger.error(f"Task routing failed: {e}", exc_info=True)
            return {
                "task_id": task_id,
                "status": "error",
                "output": {"error": str(e)},
                "trace_id": trace_id,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

    async def _understand_intent(
        self,
        user_input: str,
        context: dict[str, Any],
        model: str = "",
        provider_hint: str = "",
        tenant_id: str = "",
    ) -> dict[str, Any]:
        """理解用户意图

        策略:
        1. 尝试调用 LLM (如果 gateway 可用)
        2. 降级到关键词提取 (LLM 不可用时)

        model / provider_hint / tenant_id 来自**会话运行时状态**（P1-d，docs/session-runtime-spec.md）。
        此前这条链路把模型写死为 gpt-4o-mini，导致用户在界面上切换模型/提供商
        对统一链路（/v1/chat/submit）完全无效。
        """
        # 尝试使用 LLM 进行意图识别
        try:
            return await self._llm_understand_intent(
                user_input,
                context,
                model=model,
                provider_hint=provider_hint,
                tenant_id=tenant_id,
            )
        except Exception as e:
            logger.warning(
                f"LLM intent recognition failed, falling back to keywords: {e}"
            )
            # 降级到关键词提取
            return {
                "keywords": self._extract_keywords(user_input),
                "entities": self._extract_entities(user_input),
                "action": self._infer_action(user_input),
                "fallback": True,
            }

    async def _llm_understand_intent(
        self,
        user_input: str,
        context: dict[str, Any],
        model: str = "",
        provider_hint: str = "",
        tenant_id: str = "",
    ) -> dict[str, Any]:
        """使用 LLM 理解用户意图 (生产级实现)

        model/provider_hint/tenant_id 由 route_task 从会话运行时状态注入（P1-d）；
        为空时回落默认轻量模型，保持既有行为不变。
        """
        from app.main import get_gateway

        gateway = await get_gateway()

        prompt = f"""Analyze the user's intent and return a structured JSON response.

User input: "{user_input}"

Context: {json.dumps(context, ensure_ascii=False) if context else "None"}

Please provide:
1. action: One of ["analyze", "generate_code", "search", "create_workflow", "manage_knowledge", "general"]
2. keywords: List of important keywords/entities
3. entities: Extracted entities (files, functions, data)
4. complexity: ["simple", "moderate", "complex"] - How many steps needed
5. recommended_workstations: List of workstation types that should handle this task
6. description: Brief description of what the user wants to achieve

Return ONLY valid JSON without markdown formatting."""

        # 注意：gateway 缓存层要求 ChatMessage 对象（m.to_dict()），裸 dict 会在
        # _exact_key 处抛 AttributeError（冒烟测试 2026-08-21 实测踩坑）
        from app.gateway.provider import ChatMessage

        messages = [
            ChatMessage(
                role="system",
                content="You are a task intent analyzer. Your job is to understand user requests and structure them for automated processing. Return only JSON, no explanations.",
            ),
            ChatMessage(role="user", content=prompt),
        ]

        # 调用 LLM：模型/provider 来自会话运行时状态（P1-d）；tenant_id 传入使租户路由生效
        response_text = ""
        async for chunk in gateway.chat_stream(
            messages=messages,
            model=model or "gpt-4o-mini",
            provider_hint=provider_hint,
            tenant_id=tenant_id,
        ):
            if chunk.content:
                response_text += chunk.content

        # 解析 LLM 响应
        import re

        # 清理可能的 markdown 格式
        response_text = re.sub(r"```json\s*|\s*```", "", response_text.strip())

        try:
            intent: dict[str, Any] = json.loads(response_text)
            intent["fallback"] = False
            logger.info(
                f"LLM intent recognized: action={intent.get('action')}, complexity={intent.get('complexity')}"
            )
            return intent
        except json.JSONDecodeError:
            logger.warning("Failed to parse LLM response as JSON, using fallback")
            raise ValueError("Invalid LLM response format") from None

    async def _decompose_task(
        self,
        intent: dict[str, Any],
        tenant_id: str,
        user_input: str,
    ) -> list[SubTask]:
        """将意图拆解为子任务

        策略:
        1. LLM 意图识别成功 (fallback=False) 且给出推荐工作台 → 按推荐站台编排跨工作台流水线
        2. 否则降级到基于动作的规则分解
        """
        action = intent.get("action", "")
        keywords = intent.get("keywords", [])
        complexity = intent.get("complexity", "simple")
        fallback = intent.get("fallback", True)

        # 降级意图必须先走规则分解：降级 dict 无 complexity 字段，若先判
        # complexity 会拿到默认值 "simple" 而短路成单任务 —— 搜索/分析等
        # 具体动作的 kb 链编排将永远不触发（冒烟测试 2026-08-21 实测踩坑）
        if fallback:
            return await self._rule_based_decomposition(action, keywords, user_input)

        # LLM 意图识别成功且判定为简单任务 → 直接单任务处理
        if complexity == "simple":
            return [
                SubTask(
                    subtask_id="sub_0_default",
                    description=user_input or intent.get("description", ""),
                    capability_id="",
                    tags=keywords,
                )
            ]

        # LLM 提供了推荐的工作台 → 跨工作台流水线编排
        recommended_workstations = intent.get("recommended_workstations", [])
        subtasks = await self._decompose_by_recommendation(
            recommended_workstations,
            tenant_id,
            user_input,
            keywords,
        )
        if subtasks:
            return subtasks

        # 推荐站台无可用能力,回退规则分解
        return await self._rule_based_decomposition(action, keywords, user_input)

    async def _decompose_by_recommendation(
        self,
        recommended_workstations: list[Any],
        tenant_id: str,
        user_input: str,
        keywords: list[str],
    ) -> list[SubTask]:
        """按 LLM 推荐的工作台编排跨工作台流水线

        每个推荐站台匹配一个能力，子任务线性串联（后序依赖前序输出），
        形成真正的跨工作台协同（如 agent 分析 → knowledge 检索 → skill 执行）。
        """
        subtasks: list[SubTask] = []
        for ws in recommended_workstations:
            try:
                wst = WorkstationType(ws)
            except ValueError:
                logger.warning("Unknown recommended workstation: %r", ws)
                continue

            caps = await self.registry.list_by_workstation(wst, tenant_id)
            if not caps:
                logger.warning(
                    "No capability available on recommended workstation: %s", ws
                )
                continue

            cap = caps[0]
            param_names = {p.name for p in cap.input_schema}
            parameters: dict[str, Any] = {}
            if "task" in param_names:
                parameters["task"] = user_input
            elif "query" in param_names:
                parameters["query"] = user_input

            subtask_id = f"sub_{len(subtasks)}_{wst.value}"
            subtasks.append(
                SubTask(
                    subtask_id=subtask_id,
                    description=user_input,
                    capability_id=cap.capability_id,
                    parameters=parameters,
                    dependencies=[subtasks[-1].subtask_id] if subtasks else [],
                    tags=keywords,
                    workstation_type=wst,
                )
            )

        if len(subtasks) >= 2:
            logger.info(
                "Cross-workstation pipeline planned: %s",
                " -> ".join(t.capability_id for t in subtasks),
            )
        return subtasks

    async def _rule_based_decomposition(
        self,
        action: str,
        keywords: list[str],
        user_input: str = "",
    ) -> list[SubTask]:
        """基于动作的任务分解 (降级策略)

        search 动作编排两步知识库链路：kb_list（发现知识库）→ kb_search（检索），
        kb_id 通过依赖输出模板 ${sub_0_list.knowledge_bases.0.id} 注入。
        """
        # 基于动作拆解
        if action == "analyze":
            return [
                SubTask(
                    subtask_id="sub_0_read",
                    description="读取文件内容",
                    capability_id="skill:read_file",
                    parameters={"path": " ".join(keywords) if keywords else ""},
                    tags=keywords,
                ),
                SubTask(
                    subtask_id="sub_1_analyze",
                    description="分析内容并提取关键信息",
                    capability_id="agent:analyzer",
                    parameters={"task": user_input},
                    dependencies=["sub_0_read"],
                    tags=keywords,
                ),
            ]

        elif action == "generate_code":
            return [
                SubTask(
                    subtask_id="sub_0_parse",
                    description="解析需求",
                    capability_id="agent:requirement_parser",
                    parameters={"task": user_input},
                    tags=keywords,
                ),
                SubTask(
                    subtask_id="sub_1_write",
                    description="编写代码",
                    capability_id="skill:execute_python",
                    parameters={"code": "${sub_0_parse.result}"},
                    dependencies=["sub_0_parse"],
                    tags=keywords,
                ),
            ]

        elif action == "search":
            return [
                SubTask(
                    subtask_id="sub_0_list",
                    description="查找可用知识库",
                    capability_id="knowledge:kb_list",
                    parameters={"query": ""},
                    tags=keywords,
                ),
                SubTask(
                    subtask_id="sub_1_search",
                    description="在知识库中检索相关信息",
                    capability_id="knowledge:kb_search",
                    parameters={
                        "kb_id": "${sub_0_list.knowledge_bases.0.id}",
                        "query": user_input,
                    },
                    dependencies=["sub_0_list"],
                    tags=keywords,
                ),
            ]

        else:
            # 默认: 单任务
            return [
                SubTask(
                    subtask_id="sub_0_default",
                    description=user_input or "Execute task",
                    capability_id="",  # 待匹配
                    tags=keywords,
                )
            ]

    async def _match_capabilities(
        self,
        subtasks: list[SubTask],
        tenant_id: str,
    ) -> list[SubTask]:
        """为每个子任务匹配能力"""
        matched = []

        for subtask in subtasks:
            if subtask.capability_id:
                # 精确匹配
                cap = await self.registry.get_by_id(subtask.capability_id, tenant_id)
                if cap:
                    subtask.capability_id = cap.capability_id
                    subtask.workstation_type = cap.workstation_type
                    matched.append(subtask)
                    continue

            # 语义搜索匹配
            query = subtask.description + " " + " ".join(subtask.tags)
            caps = await self.registry.search(
                query=query,
                tenant_id=tenant_id,
                limit=3,
            )

            if caps:
                best_cap = caps[0]
                subtask.capability_id = best_cap.capability_id
                subtask.workstation_type = best_cap.workstation_type
                matched.append(subtask)
                logger.info(
                    "Matched capability for subtask %s: %s",
                    subtask.subtask_id,
                    best_cap.name,
                )
            else:
                logger.warning(
                    f"No capability matched for subtask: {subtask.description}"
                )
                # 兜底：未匹配到能力时路由到通用 Agent 对话能力，
                # 保证任务链路不失联（能力缺失会导致整个任务 no_match）。
                fallback_cap = await self.registry.get_by_id(
                    "agent:general_chat", tenant_id
                )
                if fallback_cap:
                    subtask.capability_id = fallback_cap.capability_id
                    subtask.workstation_type = fallback_cap.workstation_type
                    subtask.parameters = {"task": subtask.description}
                    matched.append(subtask)
                    logger.info(
                        "Falling back to %s for subtask %s",
                        fallback_cap.capability_id,
                        subtask.subtask_id,
                    )

        return matched

    def _topological_sort(self, tasks: list[SubTask]) -> list[SubTask]:
        """拓扑排序 (基于依赖关系)"""
        task_map = {t.subtask_id: t for t in tasks}
        in_degree: dict[str, int] = {t.subtask_id: 0 for t in tasks}
        adj: dict[str, list[str]] = {}

        for t in tasks:
            adj[t.subtask_id] = []
            for dep in t.dependencies:
                if dep in task_map:
                    adj[dep].append(t.subtask_id)
                    in_degree[t.subtask_id] += 1

        # BFS
        queue = [tid for tid, deg in in_degree.items() if deg == 0]
        sorted_tasks = []

        while queue:
            current = queue.pop(0)
            sorted_tasks.append(task_map[current])

            for neighbor in adj[current]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        return sorted_tasks

    async def _execute_tasks(
        self,
        tasks: list[SubTask],
        tenant_id: str,
        trace_id: str = "",
    ) -> list[ExecutedTask]:
        """执行所有子任务 (有依赖的按序执行,无依赖可并行)"""
        import asyncio

        results = []
        completed_tasks: dict[str, ExecutedTask] = {}

        # 分组: 按依赖层级
        layers = self._group_by_dependencies(tasks)

        for layer_idx, layer in enumerate(layers):
            logger.info(
                f"Executing layer {layer_idx + 1}/{len(layers)}: {[t.subtask_id for t in layer]}"
            )

            # 并行执行同一层级的任务
            coroutines = [
                self._execute_single_task(task, tenant_id, trace_id, completed_tasks)
                for task in layer
            ]

            layer_results = await asyncio.gather(*coroutines, return_exceptions=True)

            for result in layer_results:
                if isinstance(result, Exception):
                    logger.error(f"Task execution failed: {result}")
                elif isinstance(result, ExecutedTask):
                    completed_tasks[result.subtask_id] = result
                    results.append(result)

        return results

    def _group_by_dependencies(self, tasks: list[SubTask]) -> list[list[SubTask]]:
        """按依赖关系分组 (同一组内可并行)"""
        {t.subtask_id: t for t in tasks}
        grouped = []
        resolved: set[str] = set()

        while len(resolved) < len(tasks):
            # 找出所有依赖都已 resolved 的任务
            layer = []
            for t in tasks:
                if t.subtask_id not in resolved:
                    if all(dep in resolved for dep in t.dependencies):
                        layer.append(t)

            if not layer:
                # 循环依赖,全部加入（比较 subtask_id —— resolved 存的是 id，不是对象）
                layer = [t for t in tasks if t.subtask_id not in resolved]

            grouped.append(layer)
            for t in layer:
                resolved.add(t.subtask_id)

        return grouped

    def _resolve_params(
        self,
        params: dict[str, Any],
        completed_tasks: dict[str, ExecutedTask],
    ) -> dict[str, Any]:
        """解析依赖输出模板 "${dep_id.field.subfield...}"

        允许后序子任务引用前序子任务的输出（跨工作台数据流），
        路径不存在时解析为空串（由能力的参数校验显式报错）。
        """
        resolved: dict[str, Any] = {}
        for key, value in params.items():
            if (
                isinstance(value, str)
                and value.startswith("${")
                and value.endswith("}")
            ):
                path = value[2:-1].split(".")
                dep_id, fields = path[0], path[1:]
                node = (
                    completed_tasks[dep_id].output
                    if dep_id in completed_tasks
                    else None
                )
                for f in fields:
                    if isinstance(node, dict) and f in node:
                        node = node[f]
                    elif isinstance(node, list) and f.isdigit() and int(f) < len(node):
                        node = node[int(f)]
                    else:
                        node = None
                        break
                resolved[key] = node if node is not None else ""
            else:
                resolved[key] = value
        return resolved

    async def _execute_single_task(
        self,
        task: SubTask,
        tenant_id: str,
        trace_id: str,
        completed_tasks: dict[str, ExecutedTask],
    ) -> ExecutedTask:
        """执行单个子任务"""
        cap = await self.registry.get_by_id(task.capability_id, tenant_id)
        if not cap:
            return ExecutedTask(
                task_id="",
                subtask_id=task.subtask_id,
                capability_id=task.capability_id,
                input_params=task.parameters,
                error=f"Capability not found: {task.capability_id}",
                status=TaskStatus.FAILED,
            )

        executor = cap._executor
        if not executor:
            return ExecutedTask(
                task_id="",
                subtask_id=task.subtask_id,
                capability_id=task.capability_id,
                input_params=task.parameters,
                error=f"No executor registered for: {task.capability_id}",
                status=TaskStatus.FAILED,
            )

        # 注入依赖任务的输出（依赖输出模板 ${dep.field} 解析）
        input_params = self._resolve_params(task.parameters, completed_tasks)

        # 设置工具上下文：kb 等工具依赖 user_id 做归属校验（跨工作台调用必须携带）
        from app.tools.context import set_tool_context

        set_tool_context(
            session_id=f"task_{trace_id}" if trace_id else task.subtask_id,
            user_id=tenant_id,
            tenant_id=tenant_id,
        )

        # 执行
        start_time = time.time()
        try:
            import asyncio

            if asyncio.iscoroutinefunction(executor):
                output = await executor(**input_params)
            else:
                output = executor(**input_params)

            duration_ms = int((time.time() - start_time) * 1000)

            # 工具型输出约定 {"error": ...} 即失败（fail loud，不当作成功结果聚合）
            failed = isinstance(output, dict) and "error" in output
            error_msg = str(output.get("error")) if failed else ""

            # 记录 span
            from app.trace import record_span

            await record_span(
                trace_id=trace_id,
                span_name=f"task:{cap.name}",
                duration_ms=duration_ms,
                metadata={
                    "subtask_id": task.subtask_id,
                    "workstation": cap.workstation_type.value,
                    "tenant_id": tenant_id,
                    "status": "failed" if failed else "completed",
                },
                tenant_id=tenant_id,
            )

            # 发布子任务状态到 ContextBus（跨工作台事件流，供其他工作台订阅）
            from app.core.context_bus import publish_state_change

            await publish_state_change(
                topic=(
                    f"task.{trace_id}.{task.subtask_id}"
                    if trace_id
                    else f"task.{task.subtask_id}"
                ),
                data={
                    "subtask_id": task.subtask_id,
                    "capability_id": cap.capability_id,
                    "workstation": cap.workstation_type.value,
                    "status": "failed" if failed else "completed",
                    "duration_ms": duration_ms,
                },
                tenant_id=tenant_id,
            )

            # 更新能力统计
            cap.record_call(duration_ms, success=not failed)

            return ExecutedTask(
                task_id="",
                subtask_id=task.subtask_id,
                capability_id=cap.capability_id,
                input_params=input_params,
                output=None if failed else output,
                error=error_msg,
                duration_ms=duration_ms,
                status=TaskStatus.FAILED if failed else TaskStatus.COMPLETED,
            )

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)

            cap.record_call(duration_ms, success=False)

            return ExecutedTask(
                task_id="",
                subtask_id=task.subtask_id,
                capability_id=cap.capability_id,
                input_params=input_params,
                error=str(e),
                duration_ms=duration_ms,
                status=TaskStatus.FAILED,
            )

    async def _aggregate_results(
        self,
        results: list[ExecutedTask],
        intent: dict[str, Any],
        original_input: str,
    ) -> dict[str, Any]:
        """聚合子任务输出 — 按意图类型定制聚合策略

        各意图的聚合逻辑:
        - search:  收集所有来源，去重，生成摘要 + 来源列表
        - analyze: 串联分析结论，按依赖顺序输出关键发现
        - generate_code: 合并代码产出，按子任务顺序拼接
        - create_workflow / manage_knowledge: 结构化输出
        - general / default: 通用列表式聚合
        """
        completed = [r for r in results if r.status == TaskStatus.COMPLETED]
        failed = [r for r in results if r.status == TaskStatus.FAILED]
        action = intent.get("action", "general")

        base: dict[str, Any] = {
            "subtasks_completed": len(completed),
            "subtasks_failed": len(failed),
            "intent": action,
        }

        if action == "search":
            return self._aggregate_search(completed, failed, base, original_input)

        if action == "analyze":
            return self._aggregate_analyze(completed, failed, base, original_input)

        if action == "generate_code":
            return self._aggregate_code(completed, failed, base)

        # 默认通用聚合
        base["outputs"] = [
            {
                "subtask_id": r.subtask_id,
                "capability_id": r.capability_id,
                "output": r.output if r.status == TaskStatus.COMPLETED else None,
                "error": r.error if r.status == TaskStatus.FAILED else None,
            }
            for r in results
        ]
        return base

    def _aggregate_search(
        self,
        completed: list[ExecutedTask],
        failed: list[ExecutedTask],
        base: dict[str, Any],
        original_input: str,
    ) -> dict[str, Any]:
        """搜索意图聚合: 收集来源 + 去重 + 摘要"""
        sources: list[dict[str, Any]] = []
        snippets: list[str] = []

        for r in completed:
            out = r.output
            if isinstance(out, dict):
                # kb_search 返回 {results: [...]} 或 {knowledge_bases: [...]}
                items = out.get("results") or out.get("knowledge_bases") or []
                for item in items:
                    if isinstance(item, dict):
                        title = item.get("title", item.get("name", ""))
                        url = item.get("url", item.get("id", ""))
                        snippet = item.get("snippet", item.get("description", ""))
                        source = {"title": title, "url": url, "snippet": snippet}
                        # 去重 (按 url)
                        if url and not any(s.get("url") == url for s in sources):
                            sources.append(source)
                        if snippet:
                            snippets.append(snippet)
            elif isinstance(out, str) and out:
                snippets.append(out[:200])

        base["sources"] = sources
        base["summary"] = f"找到 {len(sources)} 个相关来源" + (
            f"，查询: '{original_input[:60]}'" if original_input else ""
        )
        if snippets:
            base["key_snippets"] = snippets[:5]
        if failed:
            base["errors"] = [r.error for r in failed]
        return base

    def _aggregate_analyze(
        self,
        completed: list[ExecutedTask],
        failed: list[ExecutedTask],
        base: dict[str, Any],
        original_input: str,
    ) -> dict[str, Any]:
        """分析意图聚合: 按依赖顺序串联分析结论"""
        findings: list[dict[str, Any]] = []

        for r in completed:
            out = r.output
            if isinstance(out, dict):
                conclusion = out.get(
                    "result", out.get("analysis", out.get("summary", ""))
                )
            elif isinstance(out, str):
                conclusion = out
            else:
                conclusion = str(out) if out else ""
            findings.append(
                {
                    "subtask_id": r.subtask_id,
                    "capability_id": r.capability_id,
                    "conclusion": conclusion[:500] if conclusion else "",
                }
            )

        base["findings"] = findings
        base["synthesis"] = (
            " | ".join(f["conclusion"] for f in findings if f["conclusion"])
            if findings
            else "No analysis output"
        )
        if failed:
            base["errors"] = [r.error for r in failed]
        return base

    def _aggregate_code(
        self,
        completed: list[ExecutedTask],
        failed: list[ExecutedTask],
        base: dict[str, Any],
    ) -> dict[str, Any]:
        """代码生成意图聚合: 按子任务顺序拼接代码产出"""
        code_blocks: list[dict[str, Any]] = []

        for r in completed:
            out = r.output
            if isinstance(out, dict):
                code = out.get("code", out.get("result", ""))
            elif isinstance(out, str):
                code = out
            else:
                code = str(out) if out else ""
            code_blocks.append(
                {
                    "subtask_id": r.subtask_id,
                    "code": code,
                }
            )

        base["code_blocks"] = code_blocks
        base["combined_code"] = "\n\n".join(
            cb["code"] for cb in code_blocks if cb["code"]
        )
        if failed:
            base["errors"] = [r.error for r in failed]
        return base

    def _extract_keywords(self, text: str) -> list[str]:
        """提取关键词 (简化版)"""
        import re

        words = re.findall(r"[a-zA-Z0-9]+", text.lower())
        return [w for w in words if len(w) > 2]

    def _extract_entities(self, text: str) -> dict[str, Any]:
        """提取实体 — 规则式 NER（无需外部模型依赖）

        识别实体类型:
        - urls: URL 链接
        - emails: 邮箱地址
        - phones: 手机号（中国大陆 1xx 格式）
        - file_paths: 文件路径（Unix / Win）
        - dates: 日期（YYYY-MM-DD / YYYY/MM/DD / 中文日期）
        - amounts: 金额（¥ / $ / 元）
        - ip_addresses: IPv4 地址
        - code_refs: 代码引用（function() / Class.method）
        """
        import re

        entities: dict[str, list[str]] = {}

        # URL
        urls = re.findall(r'https?://[^\s<>"\')\]]+', text)
        if urls:
            entities["urls"] = urls

        # Email
        emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)
        if emails:
            entities["emails"] = emails

        # Phone (中国大陆手机号)
        phones = re.findall(r"\b1[3-9]\d{9}\b", text)
        if phones:
            entities["phones"] = phones

        # File paths (Unix / Windows)
        unix_paths = re.findall(
            r'(?:~/|/(?:usr|home|opt|var|etc|tmp)/)[^\s<>"\')\]]+',
            text,
        )
        win_paths = re.findall(
            r'[A-Za-z]:\\[^\s<>"\')\]]+',
            text,
        )
        file_paths = unix_paths + win_paths
        if file_paths:
            entities["file_paths"] = file_paths

        # Dates
        dates = re.findall(
            r"\d{4}[-/]\d{1,2}[-/]\d{1,2}" r"|\d{4}年\d{1,2}月\d{1,2}日",
            text,
        )
        if dates:
            entities["dates"] = dates

        # Amounts (¥ / $ / 元)
        amounts = re.findall(
            r"[¥$]\s*[\d,]+(?:\.\d+)?" r"|\d+(?:\.\d+)?\s*(?:元|万元|USD|CNY)",
            text,
        )
        if amounts:
            entities["amounts"] = [a.strip() for a in amounts]

        # IPv4
        ips = re.findall(
            r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|1?\d?\d)\b",
            text,
        )
        if ips:
            entities["ip_addresses"] = ips

        # Code references (function() / Class.method)
        code_refs = re.findall(
            r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\([^)]*\)",
            text,
        )
        if code_refs:
            entities["code_refs"] = code_refs

        return entities

    def _infer_action(self, text: str) -> str:
        """推断动作"""
        text_lower = text.lower()
        if "分析" in text_lower or "analyze" in text_lower:
            return "analyze"
        elif "生成" in text_lower or "generate" in text_lower or "写代码" in text_lower:
            return "generate_code"
        elif "搜索" in text_lower or "search" in text_lower:
            return "search"
        else:
            return "general"


# ── 便捷函数 ────────────────────────────────────────────────────────
async def quick_execute(
    user_input: str,
    tenant_id: str,
    trace_id: str = "",
) -> dict[str, Any]:
    """快捷执行 API (Go 侧代理)"""
    router = TaskRouter()
    return await router.route_task(
        user_input=user_input,
        tenant_id=tenant_id,
        trace_id=trace_id,
    )
