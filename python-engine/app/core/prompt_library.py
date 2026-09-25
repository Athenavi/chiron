"""Prompt 模板库 (Prompt Template Library)

设计目标:
- 预定义常见场景的 Prompt 模板
- 支持一键触发快捷命令
- 可参数化替换 ({{variable}} 语法)
- 按工作台分类 (对话/Agent/工作流/技能)

架构思想:
类比 Kubernetes 的 Deployment Template
- 模板定义 + 实例化 = 最终 Prompt
- 支持版本管理和灰度发布
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class PromptCategory(StrEnum):
    """Prompt 分类"""

    ANALYSIS = "analysis"  # 分析类
    CODE_GENERATION = "code_gen"  # 代码生成类
    DATA_PROCESSING = "data_proc"  # 数据处理类
    DOCUMENT_SUMMARY = "summary"  # 文档总结类
    DEBUGGING = "debug"  # 调试类
    OPTIMIZATION = "optimization"  # 优化类
    GENERAL = "general"  # 通用类


class TargetWorkstation(StrEnum):
    """目标工作台"""

    DIALOGUE = "dialogue"
    AGENT = "agent"
    WORKFLOW = "workflow"
    SKILL = "skill"
    KNOWLEDGE = "knowledge"


@dataclass
class PromptTemplate:
    """Prompt 模板

    类似 Kubernetes Deployment 的 YAML 定义
    - template_id: 模板唯一标识
    - variables: 必填变量列表
    - system_prompt: 系统提示词
    - user_prompt: 用户提示词
    - tags: 标签 (用于搜索)
    """

    template_id: str  # "{category}:{scenario}"
    name: str  # 显示名称
    category: PromptCategory  # 分类
    target_workstation: TargetWorkstation  # 目标工作台
    version: str = "1.0.0"  # 语义版本

    # Prompt 模板 (Jinja2 风格)
    system_prompt: str = ""  # 系统角色设定
    user_prompt: str = ""  # 用户指令

    # 元数据
    description: str = ""
    tags: list[str] = field(default_factory=list)
    variables: list[str] = field(default_factory=list)  # 必填变量
    default_params: dict[str, Any] = field(default_factory=dict)  # 默认参数

    # 统计信息
    usage_count: int = 0
    last_used_at: float = 0

    # 实际执行函数 (Python 侧)
    _executor: Callable[..., Any] | None = field(default=None, repr=False)

    def render(self, **kwargs: Any) -> tuple[str, str]:
        """渲染 Prompt (替换 {{variable}})

        Returns:
            (system_prompt, user_prompt)
        """
        # 合并默认参数
        params = {**self.default_params, **kwargs}

        # 检查必填变量
        missing = set(self.variables) - set(params.keys())
        if missing:
            raise ValueError(f"Missing required variables: {missing}")

        # 替换变量
        system_prompt = self.system_prompt
        user_prompt = self.user_prompt

        for key, value in params.items():
            system_prompt = system_prompt.replace(f"{{{{{key}}}}}", str(value))
            user_prompt = user_prompt.replace(f"{{{{{key}}}}}", str(value))

        return system_prompt, user_prompt


class PromptLibrary:
    """Prompt 模板库

    功能:
    1. 模板注册/注销/更新
    2. 基于关键词/标签的模板搜索
    3. 模板实例化 (渲染 Prompt)
    4. 使用统计和热排序
    """

    def __init__(self) -> None:
        self._templates: dict[str, PromptTemplate] = {}
        self._index_by_tags: dict[str, list[str]] = {}  # tag -> template_ids

    async def register(self, template: PromptTemplate) -> None:
        """注册 Prompt 模板"""
        self._templates[template.template_id] = template

        # 更新标签索引
        for tag in template.tags:
            self._index_by_tags.setdefault(tag, []).append(template.template_id)

        logger.info(
            f"Registered prompt template: {template.template_id} ({template.name})"
        )

    async def unregister(self, template_id: str) -> bool:
        """注销 Prompt 模板"""
        if template_id not in self._templates:
            return False

        template = self._templates.pop(template_id)

        # 清理标签索引
        for tag in template.tags:
            if tag in self._index_by_tags:
                self._index_by_tags[tag].remove(template_id)

        logger.info(f"Unregistered prompt template: {template_id}")
        return True

    async def get(self, template_id: str) -> PromptTemplate | None:
        """获取模板"""
        return self._templates.get(template_id)

    async def search(
        self,
        query: str,
        category: PromptCategory | None = None,
        workstation: TargetWorkstation | None = None,
        limit: int = 10,
    ) -> list[PromptTemplate]:
        """搜索模板 (基于关键词 + 标签 + 分类)"""
        results = []

        for template in self._templates.values():
            # 分类过滤
            if category and template.category != category:
                continue

            # 工作台过滤
            if workstation and template.target_workstation != workstation:
                continue

            # 关键词匹配 (名称 + 描述 + 标签)
            query_lower = query.lower()
            if (
                query_lower in template.name.lower()
                or query_lower in template.description.lower()
                or any(query_lower in tag.lower() for tag in template.tags)
            ):
                results.append(template)

        # 按使用次数排序
        results.sort(key=lambda t: t.usage_count, reverse=True)

        return results[:limit]

    async def render_prompt(
        self,
        template_id: str,
        **kwargs: Any,
    ) -> tuple[str, str]:
        """实例化并渲染 Prompt

        Returns:
            (system_prompt, user_prompt)
        """
        template = await self.get(template_id)
        if not template:
            raise ValueError(f"Template not found: {template_id}")

        # 更新统计
        template.usage_count += 1
        template.last_used_at = time.time()

        return template.render(**kwargs)

    async def preload_default_templates(self) -> None:
        """预加载默认模板"""

        # ── 分析类模板 ───────────────────────────────────────
        await self.register(
            PromptTemplate(
                template_id="analysis:sales_report",
                name="销售报告分析",
                category=PromptCategory.ANALYSIS,
                target_workstation=TargetWorkstation.AGENT,
                description="分析销售数据文件并生成详细报告",
                tags=["销售", "报告", "分析", "CSV"],
                variables=["file_path"],
                system_prompt="""你是一个资深数据分析师。请分析指定的销售数据文件,提取关键指标:
1. 总销售额、平均订单价值
2. Top 10 产品销售排名
3. 月度销售趋势
4. 区域销售分布
5. 异常点检测""",
                user_prompt="请分析文件 {{file_path}} 的销售数据,生成详细报告。",
            )
        )

        await self.register(
            PromptTemplate(
                template_id="analysis:gantt_chart",
                name="甘特图生成",
                category=PromptCategory.ANALYSIS,
                target_workstation=TargetWorkstation.SKILL,
                description="从任务依赖关系生成 Mermaid 甘特图代码",
                tags=["甘特图", "项目计划", "依赖关系", "Mermaid"],
                variables=["file_path"],
                system_prompt="""你是一个项目管理专家。请分析任务依赖关系文件,生成 Mermaid 甘特图代码:
1. 识别关键路径
2. 估算各任务时间
3. 标注依赖关系
4. 输出完整 Mermaid 代码""",
                user_prompt="请分析文件 {{file_path}} 中的任务依赖,生成甘特图代码。",
            )
        )

        # ── 代码生成类模板 ──────────────────────────────────
        await self.register(
            PromptTemplate(
                template_id="code_gen:python_script",
                name="Python 脚本生成",
                category=PromptCategory.CODE_GENERATION,
                target_workstation=TargetWorkstation.AGENT,
                description="根据自然语言描述生成 Python 脚本",
                tags=["Python", "脚本", "代码生成"],
                variables=["description"],
                system_prompt="""你是一个资深 Python 工程师。请根据描述生成高质量 Python 代码:
1. 遵循 PEP 8 规范
2. 添加类型注解和 docstring
3. 包含错误处理
4. 添加单元测试用例""",
                user_prompt="请根据以下描述生成 Python 脚本:\n\n{{description}}",
            )
        )

        await self.register(
            PromptTemplate(
                template_id="code_gen:mermaid_diagram",
                name="Mermaid 图表生成",
                category=PromptCategory.CODE_GENERATION,
                target_workstation=TargetWorkstation.SKILL,
                description="根据需求描述生成 Mermaid 图表",
                tags=["Mermaid", "流程图", "架构图", "图表"],
                variables=["description", "diagram_type"],
                default_params={"diagram_type": "flowchart"},
                system_prompt="""你是一个技术文档专家。请根据描述生成 Mermaid 图表代码:
- 流程图: 使用 flowchart TD
- 时序图: 使用 sequenceDiagram
- 类图: 使用 classDiagram
- 甘特图: 使用 gantt

要求:
1. 节点命名清晰
2. 布局合理
3. 添加注释""",
                user_prompt="请根据以下描述生成 {{diagram_type}} 代码:\n\n{{description}}",
            )
        )

        # ── 数据处理类模板 ──────────────────────────────────
        await self.register(
            PromptTemplate(
                template_id="data_proc:csv_clean",
                name="CSV 数据清洗",
                category=PromptCategory.DATA_PROCESSING,
                target_workstation=TargetWorkstation.SKILL,
                description="自动检测并清洗 CSV 数据中的问题",
                tags=["CSV", "数据清洗", "缺失值", "去重"],
                variables=["input_file", "output_file"],
                system_prompt="""你是一个数据清洗专家。请对 CSV 文件执行以下操作:
1. 检测缺失值并填充 (均值/中位数/众数)
2. 去除重复行
3. 修正数据类型
4. 检测异常值并标记
5. 输出清洗报告""",
                user_prompt="请清洗文件 {{input_file}},将结果保存到 {{output_file}}。",
            )
        )

        # ── 文档总结类模板 ──────────────────────────────────
        await self.register(
            PromptTemplate(
                template_id="summary:technical_doc",
                name="技术文档摘要",
                category=PromptCategory.DOCUMENT_SUMMARY,
                target_workstation=TargetWorkstation.DIALOGUE,
                description="提取技术文档的关键信息并生成摘要",
                tags=["技术文档", "摘要", "关键点"],
                variables=["file_path"],
                system_prompt="""你是一个技术文档专家。请读取文档并生成结构化摘要:
1. 文档主题 (1 句话)
2. 关键技术点 (3-5 条)
3. API 接口列表 (如有)
4. 待办事项/注意事项""",
                user_prompt="请为文件 {{file_path}} 生成技术文档摘要。",
            )
        )

        # ── 调试类模板 ──────────────────────────────────────
        await self.register(
            PromptTemplate(
                template_id="debug:error_analysis",
                name="错误日志分析",
                category=PromptCategory.DEBUGGING,
                target_workstation=TargetWorkstation.AGENT,
                description="分析错误日志并定位根因",
                tags=["调试", "错误日志", "根因分析"],
                variables=["error_log", "context"],
                system_prompt="""你是一个资深运维工程师。请分析错误日志:
1. 错误类型分类
2. 根因分析 (5 Whys)
3. 影响范围评估
4. 修复建议 (具体步骤)
5. 预防措施""",
                user_prompt="请分析以下错误日志:\n\n```\\n{{error_log}}\\n```\n\n上下文: {{context}}",
            )
        )

        # ── 优化类模板 ──────────────────────────────────────
        await self.register(
            PromptTemplate(
                template_id="optimization:code_review",
                name="代码审查",
                category=PromptCategory.OPTIMIZATION,
                target_workstation=TargetWorkstation.AGENT,
                description="全面审查代码质量并提供优化建议",
                tags=["代码审查", "质量", "优化", "Best Practice"],
                variables=["code", "language"],
                default_params={"language": "python"},
                system_prompt="""你是一个代码审查专家。请按以下维度审查 {language} 代码:

## 正确性
- 逻辑是否正确?
- 有无边界条件遗漏?

## 性能
- 时间复杂度是否最优?
- 有无内存泄漏风险?

## 可读性
- 命名是否清晰?
- 函数是否单一职责?

## 安全性
- SQL 注入风险?
- 敏感数据泄露?

## 建议改进
- 具体代码片段重构建议""",
                user_prompt="请审查以下 {{language}} 代码:\n\n```{{language}}\n{{code}}\n```",
            )
        )

        # ── 通用类模板 ──────────────────────────────────────
        await self.register(
            PromptTemplate(
                template_id="general:free_form",
                name="自由表单",
                category=PromptCategory.GENERAL,
                target_workstation=TargetWorkstation.DIALOGUE,
                description="通用对话模板 (无特定场景)",
                tags=["通用", "对话", "问答"],
                variables=["question"],
                system_prompt="你是一个智能助手。请用简洁准确的语言回答用户问题。",
                user_prompt="{{question}}",
            )
        )

        logger.info(f"Preloaded {len(self._templates)} default prompt templates")

    def get_stats(self) -> dict[str, Any]:
        """获取模板库统计信息"""
        total = len(self._templates)
        by_category: dict[str, int] = {}
        by_workstation: dict[str, int] = {}

        for template in self._templates.values():
            by_category[template.category.value] = (
                by_category.get(template.category.value, 0) + 1
            )
            by_workstation[template.target_workstation.value] = (
                by_workstation.get(template.target_workstation.value, 0) + 1
            )

        return {
            "total_templates": total,
            "by_category": by_category,
            "by_workstation": by_workstation,
            "most_used": sorted(
                self._templates.values(),
                key=lambda t: t.usage_count,
                reverse=True,
            )[:5],
        }


# ── 全局单例 ────────────────────────────────────────────────────────
_global_library: PromptLibrary | None = None


def get_library() -> PromptLibrary:
    """获取全局 Prompt 库单例"""
    global _global_library

    if _global_library is None:
        _global_library = PromptLibrary()

    return _global_library


async def init_library() -> PromptLibrary:
    """初始化 Prompt 库 (预加载模板)"""
    library = get_library()
    await library.preload_default_templates()
    return library


async def quick_execute_with_template(
    template_id: str,
    tenant_id: str,
    **kwargs: Any,
) -> tuple[str, str]:
    """使用模板快速执行

    Args:
        template_id: 模板 ID (如 "analysis:sales_report")
        tenant_id: 租户 ID
        **kwargs: 模板变量

    Returns:
        (system_prompt, user_prompt)
    """
    library = get_library()
    return await library.render_prompt(template_id, **kwargs)
