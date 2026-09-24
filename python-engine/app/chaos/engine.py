"""混沌工程的**契约常量**（故障类型与实验状态）。

本模块只保留与存储层 / Go 侧对齐的枚举值，供校验与文档引用。

## 为什么删掉了原来的 ChaosEngine

原实现是一份**演示性的假注入**：``_inject_latency`` / ``_inject_timeout`` 只是自己
``asyncio.sleep``，``_inject_error`` 只打一行日志然后写 ``{"injected": True}`` ——
**没有任何一处作用到 target 上**。它也不需要存在：

* **创建/回滚/查询实验** → Go 网关的 ``internal/api/ent_chaos_handler.go``（直写
  ``ent_chaos_experiments``，权限 ``chaos:manage``）；
* **真正施加故障** → :mod:`app.chaos.injector` 的 ASGI 中间件（Python 请求路径）
  与 Go 侧的同名中间件（网关请求路径）。

留着它只会让人以为"注入已经实现了" —— 与 Go 侧那六个长期返回 501 的端点一样，
都是"看起来有、实际没有"的同一类问题。
"""

from __future__ import annotations

from enum import StrEnum


class FaultType(StrEnum):
    """故障类型。与 ``ent_chaos_experiments.fault_type`` 的取值一一对应。"""

    LATENCY = "latency"
    ERROR = "error"
    TIMEOUT = "timeout"
    RESOURCE = "resource"


class ExperimentStatus(StrEnum):
    """实验状态。与 ``ent_chaos_experiments.status`` 的取值一一对应。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


#: 有「进行中」语义的状态 —— 注入中间件只关心这些（对应迁移里的部分索引）。
ACTIVE_STATUSES = frozenset({ExperimentStatus.PENDING, ExperimentStatus.RUNNING})


__all__ = ["ACTIVE_STATUSES", "ExperimentStatus", "FaultType"]
