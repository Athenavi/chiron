"""工具模块

包含：
- SystemToolClient / ToolDiscovery（兼容旧 Go 调用链）
- 本地 Python 工具注册表（registry）与核心工具实现（core / memory / pm / skill / graph / agent / browser / media）
"""

import app.tools.agent  # noqa: F401
import app.tools.ask_user  # noqa: F401
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
# ⚠️ 以下几个模块都自带 registry.register，**漏 import 就等于工具从未注册**。
# 此前的真实缺陷：下面 5 个模块没被导入，导致 run_code / subagent /
# read_subagent_result / run_in_background / job_output / job_kill / persistent_shell
# 这 7 个工具在注册表里根本不存在 —— 而 modes.py 的 CORE_TOOL_NAMES 写着 "subagent"、
# PTC_EXTRA_TOOLS 写着 "run_code"，于是那些配置全是空头支票。
# 排查方法：`python -c "import app.tools; from app.tools.registry import registry; print(sorted(registry.list_names()))"`
import app.tools.mode_admin  # noqa: F401
import app.tools.pm  # noqa: F401
import app.tools.rag_query  # noqa: F401
import app.tools.run_code  # noqa: F401
import app.tools.skill  # noqa: F401
import app.tools.subagent  # noqa: F401
import app.tools.subagent_result  # noqa: F401
import app.tools.terminal  # noqa: F401
import app.tools.tool_search  # noqa: F401 — 按需激活入口（Token Economy，见该模块文档）
from app.tools.client import SystemToolClient
from app.tools.discovery import ToolDiscovery
from app.tools.registry import ToolRegistry, registry

__all__ = ["SystemToolClient", "ToolDiscovery", "ToolRegistry", "registry"]
