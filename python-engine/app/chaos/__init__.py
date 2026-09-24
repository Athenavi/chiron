"""混沌工程 — 故障注入与系统韧性验证。

支持故障类型：
- latency: 网络延迟注入（网关/LLM 调用）
- error: 错误注入（HTTP 500/503/429）
- timeout: 超时注入
- resource: 资源耗尽模拟（CPU/内存）

用法：
  from app.chaos import ChaosEngine
  engine = ChaosEngine()
  await engine.inject("latency", target="gateway", duration_ms=2000)
"""

from __future__ import annotations

from .engine import ChaosEngine, Experiment, ExperimentStatus, FaultType

__all__ = ["ChaosEngine", "Experiment", "ExperimentStatus", "FaultType"]
