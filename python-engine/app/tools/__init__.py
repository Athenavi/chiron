"""工具模块

包含：
- SystemToolClient / ToolDiscovery（兼容旧 Go 调用链）
- 本地 Python 工具注册表（registry）与核心工具实现

**这里是工具注册的唯一入口。** 每个自带 ``registry.register`` 的模块都必须在此导入 ——
漏 import 就等于工具从未注册（历史缺陷：run_code / subagent / read_subagent_result /
run_in_background / job_output / job_kill / persistent_shell 都曾因此不存在，而
``modes.py`` 的清单仍然写着它们）。
排查方法：``python -c "import app.tools; from app.tools.registry import registry; print(sorted(registry.list_names()))"``

⚠️ 不要把注册清单复制到别处（``app/main.py`` 曾经另有一份、且两份都不完整，
于是"哪些工具存在"取决于从哪个入口 import —— 见 vendor/规划.md §4 的"注册 ≠ 可见"）。
"""

import app.tools.ask_user  # noqa: F401 — ask_user（占位 handler，真逻辑在 runtime）
import app.tools.browser  # noqa: F401
import app.tools.core  # noqa: F401
import app.tools.edit_file  # noqa: F401
import app.tools.git_tools  # noqa: F401
import app.tools.glob_tools  # noqa: F401
import app.tools.graph  # noqa: F401
import app.tools.jobs  # noqa: F401
import app.tools.kb  # noqa: F401
import app.tools.media  # noqa: F401
import app.tools.memory  # noqa: F401
import app.tools.mode_admin  # noqa: F401
import app.tools.pm  # noqa: F401
import app.tools.rag_query  # noqa: F401
import app.tools.run_code  # noqa: F401
import app.tools.skill  # noqa: F401
import app.tools.subagent  # noqa: F401
import app.tools.subagent_list  # noqa: F401 — list_subagent_runs（主 Agent 主动感知）
import app.tools.subagent_rerun  # noqa: F401 — rerun_subagent（重跑已结束的子 Agent）
import app.tools.subagent_result  # noqa: F401 — read_subagent_result
import app.tools.subagent_resume  # noqa: F401 — resume_subagent（R5(b)：带步骤继续 / 分叉）
import app.tools.terminal  # noqa: F401
import app.tools.todo  # noqa: F401 — write_todos（S6a 计划状态）
import app.tools.tool_result  # noqa: F401 — 取回被结构摘要替换的工具结果原文
import app.tools.tool_search  # noqa: F401 — 按需激活入口（Token Economy）
import app.tools.web  # noqa: F401 — web_search / web_fetch

# ⚠️ app.workflow.tools 注册的 workflow_run 会**覆盖** app.tools.graph 的同名工具
# （后者已删除，见 graph.py 的说明）。新增工具前先在注册表里查重名。
import app.workflow.tools  # noqa: F401 — workflow_run / workflow_status
from app.tools.client import SystemToolClient
from app.tools.discovery import ToolDiscovery
from app.tools.registry import ToolRegistry, registry

__all__ = ["SystemToolClient", "ToolDiscovery", "ToolRegistry", "registry"]
