"""混沌工程 — 故障注入与系统韧性验证。

## 两个层次

* **真实注入通道**（:mod:`app.chaos.injector`）：ASGI 中间件从 Redis/PostgreSQL 读
  活跃实验，命中就对**引擎请求路径**施加 latency / error。这是实际生效的部分。
* **实验的增删改查**：由 Go 网关的 ``internal/api/ent_chaos_handler.go`` 承担，
  权威存储是 ``ent_chaos_experiments``（迁移 ``0002_ent_chaos_experiments``）。
  本包只保留契约常量（:mod:`app.chaos.engine`）。

支持的作用面：``engine``（本进程的请求路径）与 ``gateway``（Go 网关的请求路径）。
``llm`` / ``db`` / ``redis`` 需要在各自调用链上插桩，目前**明确不支持** ——
创建这类实验会被拒绝（见 :func:`app.chaos.injector.unsupported_reason`）。

## 历史

本模块原先的 ``ChaosEngine`` 是**演示性假注入**（自己 sleep、只打日志），而 Go 侧六个
端点长期返回 501 —— 两端都没能真正注入故障。现已删除假实现，改为上面的真实通道。
"""

from __future__ import annotations

from .engine import ACTIVE_STATUSES, ExperimentStatus, FaultType
from .injector import (
    ACTIVE_TTL_SECONDS,
    MAX_INJECT_MS,
    MIDDLEWARE_FAULT_TYPES,
    SUPPORTED_TARGETS,
    ChaosInjectionMiddleware,
    invalidate,
    load_active_faults,
    pick_fault,
    unsupported_reason,
)

__all__ = [
    "ACTIVE_STATUSES",
    "ACTIVE_TTL_SECONDS",
    "MAX_INJECT_MS",
    "MIDDLEWARE_FAULT_TYPES",
    "SUPPORTED_TARGETS",
    "ChaosInjectionMiddleware",
    "ExperimentStatus",
    "FaultType",
    "invalidate",
    "load_active_faults",
    "pick_fault",
    "unsupported_reason",
]
