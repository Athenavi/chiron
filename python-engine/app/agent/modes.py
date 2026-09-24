"""Agent 运行模式 — 声明式配置表（基于 deepseek-harness agent-presets 语义）

四种模式对应官方 preset：standard(常规) / minimal(极简) / code(PTC) / cordis(创造)。
模式 = ModeConfig（persona 策略 + 工具集 + 特殊能力 + 上下文/压缩开关），
新增模式只需在 _MODE_CONFIGS 加一条条目。mode_overrides.json（可被创造
模式的 mode_edit 工具写入）在加载时叠加覆盖。

── 四种模式的明确差异（前端 ChatView 的模式选择器按此口径呈现）──

| 模式 | 一句话定位 | 工具集 | 注入记忆/技能/RAG | 压缩 | 输出参数（前端预设） |
| --- | --- | --- | --- | --- | --- |
| normal 常规 | 通用助手：日常问答 + 轻任务 | 核心 12 个 | 是 | 开 | temp 0.6 / max_tokens 4096 |
| minimal 极简 | 最短路径：只读、改、跑 | 3 个（read_file / edit_file / shell_exec） | 否 | 关 | temp 0.2 / 1024 |
| ptc PTC | 程序化工具调用：多步操作写成一段程序一次执行 | 核心 12 个 + run_code（persona 引导"写代码而非逐步调用"） | 是 | 开 | temp 0.4 / 4096 |
| creative 创造 | 自我改造：可读写平台自身的模式/技能定义 | 核心 12 个 + mode_list / mode_edit | 是 | 开 | temp 1.0 / 8192 |

差异必须**可感知**：切换模式后，模型看到的工具清单、persona、注入的上下文与压缩策略
都应随之改变（见 runtime.py 的 _filter_tools / persona 覆盖 / include_context /
enable_compaction）。若某模式在这些维度上与 normal 完全相同，用户在界面上就会觉得
"切了没变化"—— 这正是 2026-09 实测暴露的问题（PTC 与 CREATIVE 当时与 normal 无实质差异）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 核心工具列表（Token Economy：只暴露这些给 LLM，其余按需激活） ──
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
        "write_file",  # 写入/保存文件
        "grep_files",  # 搜索文件
        "subagent",  # 子 agent 委派（多 agent 协作核心）
    }
)

# 极简模式：仅信息读取 + 编辑 + shell（deepseek minimal 的 persistent-bash + str_replace_editor 语义）
MINIMAL_TOOL_NAMES = frozenset({"read_file", "edit_file", "shell_exec"})

# 模式额外工具
#
# ⚠️ 这里的名字必须**真实存在于** app/tools 的注册表里，否则等于白列。
# 校验方式：
#   python -c "import app.tools; from app.tools.registry import registry; print(sorted(registry.list_names()))"
#
# 历史缺陷（已修）：常规模式白列了 "web_fetch" 与 "subagent"，PTC 白列 "run_code"，
# 而创造模式的两个额外工具（mode_list / mode_edit）**全部落空** —— 后者正是
# "切到创造模式感觉毫无变化"的根因：persona 里写着要用它们，但工具根本不存在。
#
# PTC 的额外工具是 run_code：让"多步任务写成一段程序一次执行"成为**工具层面的能力**，
# 而不只是 persona 里的建议。（run_code.py 此前漏 import，导致这个工具从未注册。）
PTC_EXTRA_TOOLS = frozenset({"run_code"})
CREATIVE_EXTRA_TOOLS = frozenset({"mode_list", "mode_edit"})  # 这两个工具已补齐（见 app/tools/mode_admin.py）

# 创造模式 persona（deepseek cordis：可读取并定制运行平台的模式与技能定义）
CREATIVE_PERSONA = (
    "You are a coding agent on the Chiron platform. "
    "You can read and modify the agent mode definitions this platform runs on: "
    "each mode is a declared configuration (persona, tool set, context policy). "
    "Use mode_list to inspect the current modes and mode_edit to adjust them "
    "(e.g. add a tool to minimal, tune a persona). Prefer reading before editing, "
    "and keep changes reversible."
)

# PTC 模式 persona（Program-aided Tool Calling）：这是 PTC 与常规模式的**实质差异** ——
# 差别不在"能用哪些工具"，而在**用工具的方式**：多步任务优先写成一段程序一次执行，
# 而不是一轮一轮地调用工具（更少往返、更省 token、更容易验证）。
# 此前 PTC 的工具集与常规模式完全相同且没有 persona，导致"切换 PTC 感觉没有任何变化"。
PTC_PERSONA = (
    "You are a program-aided agent on the Chiron platform. "
    "When a task needs more than one step over files or data, prefer writing ONE short "
    "program and running it with run_code instead of calling tools one by one: it is "
    "faster, cheaper and easier to verify. Read before you write, keep the program "
    "self-contained, and print the exact evidence your conclusion relies on. "
    "Fall back to single tool calls only for genuinely interactive or one-off actions."
)


class AgentMode(StrEnum):
    NORMAL = "normal"
    MINIMAL = "minimal"
    PTC = "ptc"
    CREATIVE = "creative"


@dataclass(frozen=True)
class ModeConfig:
    mode: AgentMode
    persona: str | None = None  # None = 用现有默认 persona；str = 固定完整 persona
    include_context: bool = True  # 是否注入记忆/skills/RAG/git 上下文段
    include_tools: frozenset = frozenset(CORE_TOOL_NAMES)  # 模式可见工具
    extra_tools: frozenset = frozenset()  # 模式额外注册的工具
    enable_compaction: bool = True  # 是否启用上下文压缩
    compaction: dict | None = (
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
    AgentMode.PTC: ModeConfig(
        mode=AgentMode.PTC,
        persona=PTC_PERSONA,  # 与常规模式的实质差异：用代码一次性完成多步操作
        include_tools=frozenset(CORE_TOOL_NAMES),
        extra_tools=frozenset(PTC_EXTRA_TOOLS),
    ),
    AgentMode.CREATIVE: ModeConfig(
        mode=AgentMode.CREATIVE,
        persona=CREATIVE_PERSONA,
        include_tools=frozenset(CORE_TOOL_NAMES),
        extra_tools=frozenset(CREATIVE_EXTRA_TOOLS),
    ),
}


def _overrides_path() -> Path:
    return Path(__file__).resolve().parent / "mode_overrides.json"


def _load_overrides() -> dict:
    try:
        return json.loads(_overrides_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _apply_overrides(cfg: ModeConfig, overrides: dict) -> ModeConfig:
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
