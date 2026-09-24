"""混沌工程引擎 — 故障注入的核心执行器。

故障注入策略：
1. latency: 向目标注入指定延迟（asyncio.sleep）
2. error: 使目标返回指定错误
3. timeout: 模拟超时
4. resource: 消耗 CPU/内存模拟资源瓶颈
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class FaultType(StrEnum):
    LATENCY = "latency"
    ERROR = "error"
    TIMEOUT = "timeout"
    RESOURCE = "resource"


class ExperimentStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


@dataclass
class Experiment:
    """一次混沌实验。"""
    id: str
    fault_type: FaultType
    target: str  # gateway / llm / db / redis
    duration_ms: int
    intensity: float = 0.5  # 0.0-1.0
    status: ExperimentStatus = ExperimentStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    config: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)


class ChaosEngine:
    """混沌工程引擎 — 故障注入与回滚。"""

    def __init__(self):
        self._experiments: dict[str, Experiment] = {}
        self._active: set[str] = set()
        self._cancel_hooks: dict[str, asyncio.Task] = {}

    async def inject(
        self,
        fault_type: str,
        target: str,
        duration_ms: int = 1000,
        intensity: float = 0.5,
        **config: Any,
    ) -> Experiment:
        """注入故障并启动计时器自动回滚。

        Args:
            fault_type: latency / error / timeout / resource
            target: 故障目标
            duration_ms: 持续时间（毫秒）
            intensity: 强度 0.0-1.0
            **config: 故障特定配置

        Returns:
            Experiment: 实验对象
        """
        ft = FaultType(fault_type)
        exp = Experiment(
            id=uuid.uuid4().hex[:12],
            fault_type=ft,
            target=target,
            duration_ms=duration_ms,
            intensity=intensity,
            status=ExperimentStatus.RUNNING,
            started_at=datetime.now(UTC),
            config=config,
        )
        self._experiments[exp.id] = exp
        self._active.add(exp.id)

        try:
            match ft:
                case FaultType.LATENCY:
                    await self._inject_latency(exp)
                case FaultType.ERROR:
                    await self._inject_error(exp)
                case FaultType.TIMEOUT:
                    await self._inject_timeout(exp)
                case FaultType.RESOURCE:
                    await self._inject_resource(exp)
        except Exception as e:
            logger.error("Chaos injection failed: %s", e)
            exp.status = ExperimentStatus.FAILED
            exp.result = {"error": str(e)}
            return exp

        # 自动回滚定时器
        task = asyncio.create_task(self._auto_rollback(exp))
        self._cancel_hooks[exp.id] = task
        return exp

    async def rollback(self, experiment_id: str) -> Experiment | None:
        """手动回滚实验。"""
        exp = self._experiments.get(experiment_id)
        if not exp:
            return None
        if exp.id in self._cancel_hooks:
            self._cancel_hooks[exp.id].cancel()
            del self._cancel_hooks[exp.id]
        return await self._do_rollback(exp)

    async def _auto_rollback(self, exp: Experiment) -> None:
        """定时自动回滚。"""
        await asyncio.sleep(exp.duration_ms / 1000)
        if exp.id in self._active:
            await self._do_rollback(exp)

    async def _do_rollback(self, exp: Experiment) -> Experiment:
        """执行回滚。"""
        logger.info("Chaos rollback: %s/%s", exp.fault_type.value, exp.target)
        if exp.id in self._cancel_hooks:
            del self._cancel_hooks[exp.id]
        self._active.discard(exp.id)
        exp.status = ExperimentStatus.ROLLED_BACK
        exp.completed_at = datetime.now(UTC)
        exp.result["rolled_back"] = True
        return exp

    async def _inject_latency(self, exp: Experiment) -> None:
        """延迟注入。"""
        delay = exp.duration_ms / 1000 * exp.intensity
        logger.info("Chaos: latency %s -> %.1fs", exp.target, delay)
        await asyncio.sleep(delay)
        exp.result = {"delay_ms": delay * 1000}

    async def _inject_error(self, exp: Experiment) -> None:
        """错误注入（模拟在目标上产生错误状态）。"""
        error_code = exp.config.get("error_code", 500)
        logger.info("Chaos: error %s -> HTTP %d", exp.target, error_code)
        exp.result = {"error_code": error_code, "injected": True}

    async def _inject_timeout(self, exp: Experiment) -> None:
        """超时注入（模拟慢响应）。"""
        timeout = exp.duration_ms / 1000 * exp.intensity * 2
        logger.info("Chaos: timeout %s -> %.1fs", exp.target, timeout)
        await asyncio.sleep(timeout)
        exp.result = {"timeout_ms": timeout * 1000}

    async def _inject_resource(self, exp: Experiment) -> None:
        """资源耗尽模拟。"""
        resource_type = exp.config.get("resource_type", "cpu")
        if resource_type == "cpu":
            logger.info("Chaos: CPU spike on %s", exp.target)
            end = time.monotonic() + exp.duration_ms / 1000 * exp.intensity
            while time.monotonic() < end:
                _ = [random.random() for _ in range(10000)]
                await asyncio.sleep(0)
            exp.result = {"cpu_spike_ms": exp.duration_ms * exp.intensity}
        elif resource_type == "memory":
            size_mb = int(exp.config.get("size_mb", 100))
            _ = bytearray(size_mb * 1024 * 1024)
            logger.info("Chaos: memory alloc %d MB on %s", size_mb, exp.target)
            exp.result = {"memory_mb": size_mb}
        else:
            exp.result = {"resource_type": resource_type, "injected": True}

    def get_experiment(self, experiment_id: str) -> Experiment | None:
        return self._experiments.get(experiment_id)

    def list_active(self) -> list[Experiment]:
        return [self._experiments[eid] for eid in self._active]

    def list_all(self) -> list[Experiment]:
        return list(self._experiments.values())

    def status(self) -> dict[str, Any]:
        return {
            "active_count": len(self._active),
            "total_experiments": len(self._experiments),
            "active_ids": list(self._active),
        }


# 全局单例
_engine: ChaosEngine | None = None


def get_engine() -> ChaosEngine:
    global _engine
    if _engine is None:
        _engine = ChaosEngine()
    return _engine
