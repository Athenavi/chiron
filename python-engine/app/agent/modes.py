"""Agent 运行模式 — 声明式配置表（基于 deepseek-harness agent-presets 语义）

两种模式对应官方 preset：standard(常规) / minimal(极简)。
模式 = ModeConfig（persona 策略 + 工具集 + 特殊能力 + 上下文/压缩开关），
新增模式只需在 _BASE_MODES 加一条条目。mode_overrides.json（运维手工维护）在加载时叠加覆盖。

── 两种模式的明确差异（前端 ChatView 的模式选择器按此口径呈现）──

| 模式 | 一句话定位 | 工具集 | 注入记忆/技能/RAG | 压缩 | 输出参数 |
| --- | --- | --- | --- | --- | --- |
| normal 常规 | 通用助手：日常问答 + 轻任务 | 核心 17 个 | 是 | 开 | temp 0.6 / max_tokens 按模型 |
| minimal 极简 | 最短路径：只读、改、跑 | 3 个（read_file / edit_file / shell_exec） | 否 | 关 | temp 0.2 / max_tokens 按模型 |

差异必须**可感知**：切换模式后，模型看到的工具清单、persona、注入的上下文与压缩策略
都应随之改变（见 runtime.py 的 _get_core_tools / persona 覆盖 / include_context /
enable_compaction）。若某模式在这些维度上与 normal 完全相同，用户在界面上就会觉得
"切了没变化"—— 这正是 2026-09 实测暴露的问题。

历史：PTC（run_code persona）与 creative（mode_list/mode_edit）两种模式已移除。
max_tokens 不再按模式硬编码 —— 输出上限是**按模型**的属性（见 model_window.max_output_tokens），
同一模式换模型时上限本就该变（例如 DeepSeek chat 与 reasoner 的输出/窗口比相差 8 倍）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)

# ── 核心工具列表（Token Economy：只暴露这些给 LLM，其余按需激活） ──
#
# `media_create` 的加入理由：它是 agent 产出**用户能打开/下载的文件**的唯一正道
# （媒体库 = 对象存储 + media_assets，与用户直传同一条落库路径）。此前它不在核心集里，
# 模型默认看不见它，于是遇到"帮我建个 word/CSV/报告文件"就去用 `execute_python` /
# `write_file` 写宿主路径 —— 前者被沙箱拒绝，后者写出的文件用户根本拿不到。
# "工具注册了但默认不可见"在这里的代价是**模型必然走错路**，不是偶发。
CORE_TOOL_NAMES = frozenset(
    {
        "recall",  # 检索记忆（用户偏好/事实）
        "remember",  # 保存新事实
        "skill_list",  # 列出可用技能
        "skill_run",  # 执行技能
        "search_files",  # 获取外部信息（原为 "web_fetch"：该名字**从未注册**，等于白列）
        "shell_exec",  # 执行命令
        "execute_python",  # 执行 Python
        "git_status",  # 查看项目状态
        "read_file",  # 读取文件
        "ls",  # 列目录（非递归）
        "write_file",  # 写入/保存文件
        # 精确查找替换：**这是长任务的默认编辑方式**。此前它只在 MINIMAL 模式可见，
        # normal 模式下的模型只剩"整篇 write_file"一条路 —— 改一行也要重写全文，
        # 既烧 output token，又必然撞上 max_tokens 截断（半截 JSON ⇒ 工具被拒 ⇒ 重试
        # ⇒ 又被 loop_guard 当重复调用拦住）。缺它不是"少一个便利工具"，是**少了唯一的
        # 低风险编辑路径**。
        "edit_file",  # 精确编辑（old_string→new_string / 行范围替换）
        "delete_file",  # 删除文件（delete 级：需人工确认 + 二次校验）
        "grep_files",  # 搜索文件
        # 任务清单：长任务的"计划"必须能被模型自己读写。此前它不在这个白名单里，
        # normal 模式**只经 tool_search 才可能拿到** —— 模型压根不知道有这个工具，
        # 于是"读用户那条 todo_write 问题反馈"就变成了"工具不存在"。而增量更新、
        # 未完成阻断全都以它为前提。
        "todo_write",  # 维护任务清单（分步计划 / 进度）
        "media_create",  # 产出可下载的资产（媒体库；含二进制——见 app/tools/media.py）
        "subagent",  # 子 agent 委派（多 agent 协作核心）
        "use_capability",  # P7：外部能力（MCP / 插件）的单一代理入口
        "best_of_n",  # 差距D：多条路并排比较后选优（重构/修 bug/设计取舍）
    }
)

# 极简模式：仅信息读取 + 编辑 + shell（deepseek minimal 的 persistent-bash + str_replace_editor 语义）
MINIMAL_TOOL_NAMES = frozenset({"read_file", "edit_file", "shell_exec"})


class AgentMode(StrEnum):
    NORMAL = "normal"
    MINIMAL = "minimal"


@dataclass(frozen=True)
class ModeConfig:
    mode: AgentMode
    persona: str | None = None  # None = 用现有默认 persona；str = 固定完整 persona
    include_context: bool = True  # 是否注入记忆/skills/RAG/git 上下文段
    include_tools: frozenset[str] = frozenset(CORE_TOOL_NAMES)  # 模式可见工具
    extra_tools: frozenset[str] = frozenset()  # 模式额外注册的工具
    enable_compaction: bool = True  # 是否启用上下文压缩
    compaction: dict[str, Any] | None = (
        None  # SaaS：截断策略配置（strategy/max_messages/max_context_tokens/
    )
    #        threshold_ratio/snipe_ratio/tool_result_max_chars 等），
    #        租户/模式可手动确认；None = 默认策略


_BASE_MODES: dict[AgentMode, ModeConfig] = {
    AgentMode.NORMAL: ModeConfig(mode=AgentMode.NORMAL),
    AgentMode.MINIMAL: ModeConfig(
        mode=AgentMode.MINIMAL,
        persona="You are a helpful software engineer assistant.",
        include_context=False,
        include_tools=frozenset(MINIMAL_TOOL_NAMES),
        enable_compaction=False,
    ),
}


def _overrides_path() -> Path:
    return Path(__file__).resolve().parent / "mode_overrides.json"


def _load_overrides() -> dict[str, Any]:
    try:
        return cast("dict[str, Any]", json.loads(_overrides_path().read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _apply_overrides(cfg: ModeConfig, overrides: dict[str, Any]) -> ModeConfig:
    """按 overrides 字段合并（persona/include_context/include_tools/extra_tools/enable_compaction/compaction）。"""
    o = overrides.get(cfg.mode.value)
    if not isinstance(o, dict) or not o:
        return cfg
    return ModeConfig(
        mode=cfg.mode,
        persona=o.get("persona", cfg.persona),
        include_context=o.get("include_context", cfg.include_context),
        include_tools=frozenset(o.get("include_tools", list(cfg.include_tools))),
        extra_tools=frozenset(o.get("extra_tools", list(cfg.extra_tools))),
        enable_compaction=o.get("enable_compaction", cfg.enable_compaction),
        compaction=o.get("compaction", cfg.compaction),
    )


def get_mode_config(mode: str | None) -> ModeConfig:
    """未知/空模式回退 NORMAL；叠加 mode_overrides.json。"""
    try:
        base = _BASE_MODES[AgentMode(mode or "normal")]
    except (ValueError, KeyError):
        base = _BASE_MODES[AgentMode.NORMAL]
    try:
        return _apply_overrides(base, _load_overrides())
    except Exception as e:
        logger.warning("mode overrides load failed: %s", e)
        return base
