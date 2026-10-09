"""批 G：生命周期 hooks（方案 03 §3）—— 沙箱化 + 默认关。

职责：提供**默认关、沙箱执行**的生命周期 hook 机制。对外只暴露四样东西：
- 事件常量模块 `events`（**8 个**引擎侧事件 + `matcher` 适用性规则）；
- 门面单例 `hooks`（注册 + 事件方法，runtime 唯一接触点）；
- `warn_if_user_defined_enabled`（启动告警，供 `app/main.py` 启动流程调用）；
- `load_hooks_config`（批 G+ 批次 1：部署级声明文件 `hooks.json`，见 `app/hooks/config.py`）。

设计依据与三条硬约束见 `app/hooks/manager.py` 顶部；沙箱复用与 `command` 形态（批次 2b）
见 `app/hooks/runner.py`；声明面纪律见 `docs/hook-protocol-design.md` §4.2。
"""

from __future__ import annotations

from app.hooks import events
from app.hooks.config import LoadResult, load_hooks_config
from app.hooks.manager import (
    DECISION_ASK,
    DECISION_DENY,
    HANDLER_COMMAND,
    HANDLER_PYTHON,
    HANDLER_WEBHOOK,
    HANDLERS,
    OWNER_DEPLOYMENT,
    OWNER_USER,
    Hook,
    HookDecision,
    HookManager,
    hooks,
    warn_if_user_defined_enabled,
)
from app.hooks.runner import (
    HookOutcome,
    command_policy_error,
    run_command_hook,
    run_hook,
    run_webhook_hook,
    webhook_target_error,
)

__all__ = [
    "DECISION_ASK",
    "DECISION_DENY",
    "HANDLER_COMMAND",
    "HANDLER_PYTHON",
    "HANDLER_WEBHOOK",
    "HANDLERS",
    "OWNER_DEPLOYMENT",
    "OWNER_USER",
    "Hook",
    "HookDecision",
    "HookManager",
    "HookOutcome",
    "LoadResult",
    "command_policy_error",
    "events",
    "hooks",
    "load_hooks_config",
    "run_command_hook",
    "run_hook",
    "run_webhook_hook",
    "warn_if_user_defined_enabled",
    "webhook_target_error",
]
