"""批 G：生命周期 hooks（方案 03 §3）—— 沙箱化 + 默认关。

职责：提供**默认关、沙箱执行**的生命周期 hook 机制。对外只暴露三样东西：
- 事件常量模块 `events`（6 个引擎侧事件）；
- 门面单例 `hooks`（注册 + 事件方法，runtime 唯一接触点）；
- `warn_if_user_defined_enabled`（启动告警，供 `app/main.py` 启动流程调用）。

设计依据与三条硬约束见 `app/hooks/manager.py` 顶部；沙箱复用见 `app/hooks/runner.py`。
"""

from __future__ import annotations

from app.hooks import events
from app.hooks.manager import (
    OWNER_DEPLOYMENT,
    OWNER_USER,
    Hook,
    HookManager,
    hooks,
    warn_if_user_defined_enabled,
)
from app.hooks.runner import HookOutcome, run_hook

__all__ = [
    "OWNER_DEPLOYMENT",
    "OWNER_USER",
    "Hook",
    "HookManager",
    "HookOutcome",
    "events",
    "hooks",
    "run_hook",
    "warn_if_user_defined_enabled",
]
