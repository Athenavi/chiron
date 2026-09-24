"""
Engine module — 旧版 Agent 引擎，现为 runtime 模块的别名。

所有公共类已统一到 app.agent.runtime 中。
此模块保留为向后兼容的 re-export 层。
"""

from __future__ import annotations

from app.agent.prompt_engine import PromptEngine
from app.agent.runtime import (
    AgentEvent,
    AgentTask,
    CompactionConfig,
    _estimate_tokens,
    _snip_tool_results,
    _truncate_text,
    _truncate_tool_result,
)
from app.agent.runtime import AgentRuntime as AgentEngine
from app.agent.runtime import (
    _compact_messages as compress_messages,
)

# 旧版 ContextManager 由 CompactionConfig 替代
ContextManager = CompactionConfig

# 本模块是 re-export 层，导入的名字**就是**对外 API，Ruff 的 F401 无法区分
# 「未使用」与「转给他人用」。这里显式声明一次：
#
#   本模块曾因 F401 自动修复被清空全部导出（`AgentEngine` 等 9 个名字消失），
#   导致 28 个测试模块在收集阶段 ImportError（app/agent/__init__.py 依赖
#   `from app.agent.engine import AgentEngine`）。新增导出请同时登记到 __all__。
__all__ = [
    "AgentEngine",
    "AgentEvent",
    "AgentTask",
    "CompactionConfig",
    "ContextManager",
    "PromptEngine",
    "compress_messages",
    "_estimate_tokens",
    "_snip_tool_results",
    "_truncate_text",
    "_truncate_tool_result",
]
