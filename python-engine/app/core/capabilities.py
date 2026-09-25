"""统一能力注册中心 (Capabilities Registry)

设计目标:
- 所有工作台(对话/Agent/工作流/技能/知识库/插件)统一注册自身能力
- 提供基于语义的能力搜索与发现
- 支持能力组合与链式调用
- SaaS 租户隔离: 每个租户只能使用自己的能力

架构思想:
类比 Docker 的 Registry + Kubernetes 的 Service Discovery
- 工作台启动时注册 capability
- 运行时查询可用能力
- TaskRouter 根据能力动态编排执行路径
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class WorkstationType(StrEnum):
    """工作台类型"""

    DIALOGUE = "dialogue"  # 对话工作台 (入口)
    AGENT = "agent"  # Agent 工作台 (智能体)
    WORKFLOW = "workflow"  # 工作流工作台 (DAG 编排)
    SKILL = "skill"  # 技能工作台 (工具执行)
    KNOWLEDGE = "knowledge"  # 知识库工作台 (RAG)
    PLUGIN = "plugin"  # 插件工作台 (扩展)


class CapabilityType(StrEnum):
    """能力类型"""

    TOOL = "tool"  # 工具型 (如 execute_python)
    SERVICE = "service"  # 服务型 (如 kb_search)
    TEMPLATE = "template"  # 模板型 (如 prompt_template)
    COMPOSITE = "composite"  # 组合型 (多个能力组合)


@dataclass
class CapabilityParam:
    """能力参数定义 (JSON Schema 简化版)"""

    name: str
    type: str  # "string" / "number" / "boolean" / "object" / "array"
    description: str
    required: bool = True
    default: Any = None


@dataclass
class CapabilityResult:
    """能力返回结果定义"""

    fields: list[str]
    format: str = "json"  # "json" / "text" / "binary"


@dataclass
class Capability:
    """统一能力定义

    所有工作台的能力都统一为此格式
    包括: 工具、服务、模板、组合能力
    """

    capability_id: str  # 唯一标识,格式: "{workstation}:{name}"
    name: str
    description: str
    workstation_type: WorkstationType
    capability_type: CapabilityType

    # 输入输出契约
    input_schema: list[CapabilityParam] = field(default_factory=list)
    output_schema: CapabilityResult | None = None

    # 元数据
    version: str = "1.0.0"
    tags: list[str] = field(default_factory=list)  # 用于语义搜索
    author: str = ""
    status: str = "active"  # "active" / "inactive" / "deprecated"

    # 租户隔离
    tenant_id: str = ""  # 空字符串表示全局能力

    # 性能指标
    avg_latency_ms: int = 0
    # 注: success_rate 是只读 property（由调用统计计算），不能作为字段
    #     （字段与 property 同名会导致 dataclass __init__ 报 no setter）

    # 实际执行函数 (Python 侧)
    _executor: Callable[..., Any] | None = field(default=None, repr=False)

    # 组合能力的子能力列表
    sub_capabilities: list[str] = field(default_factory=list)

    # 执行耗时统计
    call_count: int = 0
    total_duration_ms: int = 0
    successful_calls: int = 0
    failed_calls: int = 0

    @property
    def avg_duration_ms(self) -> int:
        if self.call_count == 0:
            return self.avg_latency_ms
        return self.total_duration_ms // max(self.call_count, 1)

    @property
    def success_rate(self) -> float:
        """计算成功率 (避免使用默认值 1.0)"""
        if self.call_count == 0:
            return 1.0
        return self.successful_calls / max(self.call_count, 1)

    def record_call(self, duration_ms: int, success: bool = True) -> None:
        """记录一次调用,更新成功率和平均耗时"""
        self.call_count += 1
        self.total_duration_ms += duration_ms
        if success:
            self.successful_calls += 1
        else:
            self.failed_calls += 1
        # 更新 avg_latency_ms (移动平均值)
        self.avg_latency_ms = self.total_duration_ms // max(self.call_count, 1)


class CapabilitiesRegistry:
    """统一能力注册中心

    功能:
    1. 能力注册/注销/更新
    2. 基于关键词/标签的能力搜索
    3. 能力依赖分析 (组合型能力)
    4. 租户隔离 (自动过滤)

    使用示例:
        registry = CapabilitiesRegistry()

        # 注册能力
        await registry.register(Capability(
            capability_id="skill:execute_python",
            name="Execute Python Code",
            description="在沙箱中执行 Python 代码并返回结果",
            workstation_type=WorkstationType.SKILL,
            capability_type=CapabilityType.TOOL,
            input_schema=[
                CapabilityParam("code", "string", "Python 代码", required=True),
                CapabilityParam("timeout", "number", "执行超时(秒)", required=False, default=30),
            ],
            tags=["python", "sandbox", "execution"],
            tenant_id="tenant_001",
            _executor=execute_python_executor,
        ))

        # 搜索能力
        results = await registry.search("执行 Python 代码")

        # 获取能力详情
        cap = await registry.get_by_id("skill:execute_python")
    """

    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}  # capability_id -> Capability
        self._index_by_tags: dict[str, list[str]] = {}  # tag -> [capability_id]
        self._index_by_description: list[tuple[str, str]] = (
            []
        )  # [(capability_id, desc)]

    async def register(self, capability: Capability) -> None:
        """注册能力"""
        # 生成完整 ID (带租户前缀)
        full_id = (
            f"{capability.tenant_id}:{capability.capability_id}"
            if capability.tenant_id
            else capability.capability_id
        )

        # 检查 ID 冲突
        if full_id in self._capabilities:
            existing = self._capabilities[full_id]
            if existing.version != capability.version:
                logger.warning(
                    "Capability version conflict: %s v%s vs v%s",
                    full_id,
                    existing.version,
                    capability.version,
                )
            # 更新
            capability._executor = existing._executor  # 保留执行函数
            self._capabilities[full_id] = capability
            logger.info("Capability updated: %s v%s", full_id, capability.version)
        else:
            self._capabilities[full_id] = capability
            logger.info(
                "Capability registered: %s v%s (%s)",
                full_id,
                capability.version,
                capability.workstation_type.value,
            )

        # 更新标签索引
        for tag in capability.tags:
            self._index_by_tags.setdefault(tag, []).append(full_id)

        # 更新描述索引 (简单分词)
        desc_words = self._tokenize(
            capability.description + " " + " ".join(capability.tags)
        )
        for word in desc_words:
            self._index_by_description.append((word, full_id))

    async def unregister(self, capability_id: str, tenant_id: str = "") -> bool:
        """注销能力"""
        full_id = f"{tenant_id}:{capability_id}" if tenant_id else capability_id
        if full_id in self._capabilities:
            del self._capabilities[full_id]
            logger.info("Capability unregistered: %s", full_id)
            return True
        return False

    async def get_by_id(
        self, capability_id: str, tenant_id: str = ""
    ) -> Capability | None:
        """根据 ID 获取能力

        查找顺序：租户专属能力 → 全局能力（tenant_id 为空）。
        未做全局回退时，预注册的全局能力对租户请求永远不可见。
        """
        if tenant_id:
            full_id = f"{tenant_id}:{capability_id}"
            if full_id in self._capabilities:
                return self._capabilities[full_id]
        return self._capabilities.get(capability_id)

    async def list_by_workstation(
        self,
        workstation_type: WorkstationType,
        tenant_id: str = "",
        status: str | None = None,
    ) -> list[Capability]:
        """列出某工作台的所有能力"""
        results = []
        for cap in self._capabilities.values():
            if cap.workstation_type != workstation_type:
                continue
            # 全局能力（tenant_id 为空）对所有租户可见
            if tenant_id and cap.tenant_id not in ("", tenant_id):
                continue
            if status and cap.status != status:
                continue
            results.append(cap)
        return results

    async def search(
        self,
        query: str,
        tenant_id: str = "",
        workstation_type: WorkstationType | None = None,
        capability_type: CapabilityType | None = None,
        limit: int = 10,
    ) -> list[Capability]:
        """搜索能力 (基于关键词 + 标签 + 描述)

        类似搜索引擎,根据相关性打分排序
        """
        query_words = self._tokenize(query)
        scores: dict[str, float] = {}

        for cap_id, cap in self._capabilities.items():
            # 租户隔离：租户专属能力仅本租户可见；全局能力（tenant_id 为空）对所有租户可见
            if tenant_id and cap.tenant_id not in ("", tenant_id):
                continue

            # 工作台类型过滤
            if workstation_type and cap.workstation_type != workstation_type:
                continue

            # 能力类型过滤
            if capability_type and cap.capability_type != capability_type:
                continue

            score = 0

            # 精确匹配 capability_id
            if query.lower() in cap.capability_id.lower():
                score += 10

            # 标签匹配
            for tag in cap.tags:
                if tag in query_words:
                    score += 5

            # 描述匹配
            for word in query_words:
                if word in cap.description.lower():
                    score += 3

            # 名称匹配
            for word in query_words:
                if word in cap.name.lower():
                    score += 7

            if score > 0:
                scores[cap_id] = score

        # 按分数排序
        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)[
            :limit
        ]
        return [
            self._capabilities[cid] for cid in sorted_ids if cid in self._capabilities
        ]

    async def find_best_match(
        self,
        intent: str,
        tenant_id: str,
        available_workstations: list[WorkstationType] | None = None,
    ) -> Capability | None:
        """找到最匹配意图的能力

        用于 TaskRouter 自动编排
        """
        caps = await self.search(
            query=intent,
            tenant_id=tenant_id,
            workstation_type=None,  # 搜索所有工作台
            limit=5,
        )

        if not caps:
            return None

        # 返回分数最高的
        best = caps[0]

        # 如果有可用工作台限制,优先选择在工作台上执行
        if available_workstations:
            for cap in caps:
                if cap.workstation_type in available_workstations:
                    return cap

        return best

    async def resolve_dependencies(
        self, capability_id: str, tenant_id: str = ""
    ) -> list[Capability]:
        """解析组合能力的依赖链"""
        cap = await self.get_by_id(capability_id, tenant_id)
        if not cap or cap.capability_type != CapabilityType.COMPOSITE:
            return [cap] if cap else []

        # BFS 遍历子能力
        resolved = []
        queue = list(cap.sub_capabilities)
        visited = set()

        while queue:
            sub_id = queue.pop(0)
            if sub_id in visited:
                continue
            visited.add(sub_id)

            sub_cap = await self.get_by_id(sub_id, tenant_id)
            if sub_cap:
                resolved.append(sub_cap)
                if sub_cap.sub_capabilities:
                    queue.extend(sub_cap.sub_capabilities)

        return resolved

    def _tokenize(self, text: str) -> list[str]:
        """简单分词器 (中英文通用)"""
        import re

        # 提取所有单词和中文词语 (简化版)
        words = re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]+", text.lower())
        return [w for w in words if len(w) > 1]


# ── 全局单例 ───────────────────────────────────────────────────────
_global_registry: CapabilitiesRegistry | None = None


def get_registry() -> CapabilitiesRegistry:
    """获取全局能力注册中心 (单例模式)"""
    global _global_registry
    if _global_registry is None:
        _global_registry = CapabilitiesRegistry()
    return _global_registry


# ── 预注册标准能力 ─────────────────────────────────────────────────


def _tool_executor(tool_name: str) -> Any:
    """工具型能力执行器工厂 — 委托 tools registry（自带 owner/可见性校验与沙箱）"""

    async def _execute(**params: Any) -> Any:
        from app.tools.context import get_user_id
        from app.tools.registry import registry

        return await registry.execute(tool_name, params, user_id=get_user_id() or "")

    return _execute


def _llm_executor(system_prompt: str) -> Any:
    """LLM 型能力执行器工厂 (Agent 工作台) — 懒取 gateway，未初始化时 fail loud"""

    async def _execute(task: str, **kwargs: Any) -> dict[str, Any]:
        from app.gateway.provider import ChatMessage
        from app.main import get_gateway

        gateway = await get_gateway()

        response_text = ""
        # 注意：gateway 缓存层要求 ChatMessage 对象（m.to_dict()），裸 dict 会在
        # _exact_key 处抛 AttributeError（冒烟测试 2026-08-21 实测踩坑）
        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=task),
        ]
        async for chunk in gateway.chat_stream(
            messages=messages, model=kwargs.get("model", "")
        ):
            if getattr(chunk, "content", ""):
                response_text += chunk.content
        if not response_text:
            raise RuntimeError(
                f"LLM capability returned empty response (task: {task[:80]})"
            )
        return {"result": response_text}

    return _execute


async def preload_default_capabilities(
    registry: CapabilitiesRegistry | None = None,
) -> Any:
    """预加载默认能力 (系统启动时执行)

    注册六大工作台的核心能力，全部挂真实执行器：
    - Skill: read_file / execute_python (tools registry 沙箱工具)
    - Knowledge: kb_list / kb_search
    - Agent: general_chat / analyzer / requirement_parser (LLM via gateway)
    - Workflow: run (workflow_run DAG 引擎)
    - Plugin: list_tools (tools registry 全量发现)
    - Dialogue: quick_execute (TaskRouter 统一编排，见 task_router.quick_execute)
    """
    reg = registry or get_registry()

    # 1. Skill 工作台 — 工具型能力（执行器委托 tools registry，沙箱/校验由其负责）
    await reg.register(
        Capability(
            capability_id="skill:read_file",
            name="Read File Content",
            description="读取文件内容并返回文本 read file content",
            workstation_type=WorkstationType.SKILL,
            capability_type=CapabilityType.TOOL,
            input_schema=[
                CapabilityParam("path", "string", "文件路径", required=True),
            ],
            output_schema=CapabilityResult(fields=["output"]),
            tags=["file", "read", "io"],
            _executor=_tool_executor("read_file"),
        )
    )

    await reg.register(
        Capability(
            capability_id="skill:execute_python",
            name="Execute Python Code",
            description="在沙箱环境中执行 Python 代码并返回输出 execute python code sandbox",
            workstation_type=WorkstationType.SKILL,
            capability_type=CapabilityType.TOOL,
            input_schema=[
                CapabilityParam("code", "string", "Python 代码", required=True),
            ],
            output_schema=CapabilityResult(fields=["output", "error"]),
            tags=["python", "sandbox", "execution", "coding"],
            _executor=_tool_executor("execute_python"),
        )
    )

    # 2. Knowledge 工作台 — 知识库服务
    await reg.register(
        Capability(
            capability_id="knowledge:kb_list",
            name="List Knowledge Bases",
            description="列出当前用户可见的知识库 list knowledge bases",
            workstation_type=WorkstationType.KNOWLEDGE,
            capability_type=CapabilityType.SERVICE,
            input_schema=[
                CapabilityParam(
                    "query", "string", "名称过滤", required=False, default=""
                ),
                CapabilityParam(
                    "limit", "number", "返回数量", required=False, default=20
                ),
            ],
            output_schema=CapabilityResult(fields=["count", "knowledge_bases"]),
            tags=["knowledge", "list", "kb"],
            _executor=_tool_executor("kb_list"),
        )
    )

    await reg.register(
        Capability(
            capability_id="knowledge:kb_search",
            name="Search Knowledge Base",
            description="在指定知识库中检索相关文档片段 search knowledge base rag documents",
            workstation_type=WorkstationType.KNOWLEDGE,
            capability_type=CapabilityType.SERVICE,
            input_schema=[
                CapabilityParam(
                    "kb_id", "string", "知识库 ID（可先用 kb_list 获取）", required=True
                ),
                CapabilityParam("query", "string", "查询关键词", required=True),
                CapabilityParam(
                    "top_k", "number", "返回结果数", required=False, default=5
                ),
            ],
            output_schema=CapabilityResult(fields=["results", "count"]),
            tags=["knowledge", "rag", "search", "vector"],
            _executor=_tool_executor("kb_search"),
        )
    )

    # 3. Agent 工作台 — LLM 能力（执行器懒取 gateway）
    await reg.register(
        Capability(
            capability_id="agent:general_chat",
            name="General Chat Assistant",
            description="通用 AI 助手对话，处理任意自然语言任务 general chat assistant llm",
            workstation_type=WorkstationType.AGENT,
            capability_type=CapabilityType.SERVICE,
            input_schema=[
                CapabilityParam("task", "string", "任务描述/用户输入", required=True),
            ],
            output_schema=CapabilityResult(fields=["result"]),
            tags=["agent", "chat", "llm", "assistant", "general"],
            _executor=_llm_executor(
                "你是一个多功能 AI 助手，请根据任务描述给出清晰、准确的回答。"
            ),
        )
    )

    await reg.register(
        Capability(
            capability_id="agent:analyzer",
            name="Content Analyzer",
            description="分析内容并提取关键信息 analyze content extract insights",
            workstation_type=WorkstationType.AGENT,
            capability_type=CapabilityType.SERVICE,
            input_schema=[
                CapabilityParam("task", "string", "待分析的内容与要求", required=True),
            ],
            output_schema=CapabilityResult(fields=["result"]),
            tags=["agent", "analyze", "analysis", "insights"],
            _executor=_llm_executor(
                "你是一个专业分析助手。请分析给定内容，输出结构化的关键发现、数据要点和结论。"
            ),
        )
    )

    await reg.register(
        Capability(
            capability_id="agent:requirement_parser",
            name="Requirement Parser",
            description="解析用户需求，拆解为可执行的步骤 parse requirements plan",
            workstation_type=WorkstationType.AGENT,
            capability_type=CapabilityType.SERVICE,
            input_schema=[
                CapabilityParam("task", "string", "需求描述", required=True),
            ],
            output_schema=CapabilityResult(fields=["result"]),
            tags=["agent", "requirements", "parse", "plan"],
            _executor=_llm_executor(
                "你是一个需求分析专家。请把用户需求拆解为清晰、可执行的实施步骤，并指出关键约束。"
            ),
        )
    )

    # 4. Workflow 工作台 — DAG 工作流执行
    async def _workflow_run(
        graph_json: dict[str, Any],
        initial_state: dict[str, Any] | None = None,
        name: str = "",
    ) -> Any:
        from app.workflow.tools import workflow_run

        return await workflow_run(graph_json, initial_state, name)

    await reg.register(
        Capability(
            capability_id="workflow:run",
            name="Run Workflow",
            description="执行 DAG 工作流图 run workflow dag orchestration",
            workstation_type=WorkstationType.WORKFLOW,
            capability_type=CapabilityType.SERVICE,
            input_schema=[
                CapabilityParam(
                    "graph_json", "object", "工作流图定义 (nodes/edges)", required=True
                ),
                CapabilityParam(
                    "initial_state", "object", "初始状态", required=False, default=None
                ),
            ],
            output_schema=CapabilityResult(
                fields=["workflow", "instance_id", "status", "output"]
            ),
            tags=["workflow", "dag", "orchestration", "automation"],
            _executor=_workflow_run,
        )
    )

    # 5. Plugin 工作台 — 工具发现（注册表全量查询，含 MCP 插件工具）
    async def _list_tools(query: str = "") -> Any:
        from app.tools.context import get_user_id
        from app.tools.registry import registry as tool_registry

        user_id = get_user_id() or ""
        q = (query or "").lower()
        tools = []
        for name in tool_registry.list_names():
            tool = tool_registry.get(name)
            if tool is None:
                continue
            # owner 工具仅对本人可见（与 registry._visible 口径一致）
            if tool.owners and user_id not in tool.owners:
                continue
            if q and q not in name.lower() and q not in tool.description.lower():
                continue
            tools.append({"name": tool.name, "description": tool.description})
        return {"count": len(tools), "tools": tools}

    await reg.register(
        Capability(
            capability_id="plugin:list_tools",
            name="Discover Available Tools",
            description="发现系统中所有可用工具（含 MCP 插件工具） discover tools plugins list",
            workstation_type=WorkstationType.PLUGIN,
            capability_type=CapabilityType.SERVICE,
            input_schema=[
                CapabilityParam(
                    "query", "string", "名称过滤", required=False, default=""
                ),
            ],
            output_schema=CapabilityResult(fields=["count", "tools"]),
            tags=["plugin", "tools", "discovery", "mcp"],
            _executor=_list_tools,
        )
    )

    # 6. Dialogue 工作台 — 入口本身即 TaskRouter 统一编排（见 task_router.quick_execute），
    #    不注册独立能力，避免自引用循环。

    logger.info(
        "Preloaded %d default capabilities across workstations: %s",
        len(reg._capabilities),
        sorted({c.workstation_type.value for c in reg._capabilities.values()}),
    )


# ── Go 侧 API 路由示例 ─────────────────────────────────────────────
"""
Go gateway_router.go 中的路由:

// 能力注册表 API (供前端查询可用车)
mux.Handle("GET /v1/capabilities", authMW(http.HandlerFunc(capabilityListHandler)))
mux.Handle("POST /v1/capabilities/search", authMW(http.HandlerFunc(capabilitySearchHandler)))
mux.Handle("GET /v1/capabilities/{id}", authMW(http.HandlerFunc(capabilityGetHandler)))

// 快捷执行 API (前端发送自然语言,系统自动匹配能力执行)
mux.Handle("POST /v1/quick-execute", authMW(http.HandlerFunc(quickExecuteHandler)))
"""
