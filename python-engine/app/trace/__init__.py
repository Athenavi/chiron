"""Trace module — 分布式链路追踪 (SaaS: 跨实例无状态扩展)."""

from app.trace.writer import TRACES_STREAM_TPL, TraceWriter, get_tenant_stream, record_span

__all__ = ["TraceWriter", "record_span", "get_tenant_stream", "TRACES_STREAM_TPL"]
