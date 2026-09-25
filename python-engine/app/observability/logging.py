# 结构化日志 — structlog + trace_id 注入
from __future__ import annotations

import contextvars
import logging
from collections.abc import MutableMapping
from typing import Any

import structlog

# 请求上下文变量（middleware 层注入）
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)
tenant_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tenant_id", default=""
)
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default=""
)
span_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("span_id", default="")


def configure_logging(level: str = "INFO") -> None:
    """配置 structlog，输出 JSON 格式到 stdout"""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _add_request_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
    )


def _add_request_context(
    logger: Any, method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """注入 request_id / tenant_id / trace_id 到每条日志"""
    req_id = request_id_var.get("")
    if req_id:
        event_dict["request_id"] = req_id

    tid = tenant_id_var.get("")
    if tid:
        event_dict["tenant_id"] = tid

    trace = trace_id_var.get("")
    if trace:
        event_dict["trace_id"] = trace

    span = span_id_var.get("")
    if span:
        event_dict["span_id"] = span

    return event_dict
