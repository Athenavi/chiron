# OpenTelemetry 分布式追踪
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def configure_tracing(
    service_name: str = "python-engine",
    otlp_endpoint: str = "",
    insecure: bool | None = None,
) -> None:
    """
    配置 OpenTelemetry TracerProvider

    如果 otlp_endpoint 为空，使用无操作 tracer（开发环境）。
    生产环境通过 OTLP exporter 将 spans 发送到 collector。
    insecure 参数覆盖 OTLP gRPC TLS 设置，默认从环境变量
    OTEL_EXPORTER_OTLP_INSECURE 读取（true=明文，false=TLS）。
    """
    if not otlp_endpoint:
        logger.info("OTel tracing disabled (no OTLP endpoint configured)")
        return

    try:
        import os

        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        use_insecure = (
            insecure
            if insecure is not None
            else os.getenv("OTEL_EXPORTER_OTLP_INSECURE", "true").lower()
            in ("1", "true", "yes")
        )
        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=use_insecure)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        logger.info("OTel tracing enabled → %s", otlp_endpoint)
    except ImportError:
        logger.warning("OTel packages not installed, tracing disabled")
    except Exception as e:
        logger.warning("OTel tracing setup failed: %s", e)
