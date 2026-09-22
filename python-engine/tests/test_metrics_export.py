"""引擎 Prometheus 指标导出前置的回归测试。

背景：指标只在 `app/observability/metrics.py` 里**定义**过，没有任何导出路由，
于是队列积压（queue_depth）、DLQ 增长（queue_dlq_total）、重试、处理时长、
租户 token 预算全都没有采集入口 —— 多实例扩展性测试时看不到任何队列行为。

这里不去启动整个 FastAPI app（它需要 DB/Redis/Gateway 才能构造），只钉住
**"指标可被 prometheus_client 导出成文本"** 这个核心事实：路由本身只是三行
标准代码（`generate_latest()` + `CONTENT_TYPE_LATEST`）。
"""
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest


def test_metrics_module_registers_exportable_metrics():
    # 导入即注册（prometheus_client 的默认 registry）
    from app.observability import metrics  # noqa: F401

    text = generate_latest().decode("utf-8", "ignore")
    # 队列与实例相关的关键指标必须出现在导出文本里
    for name in ("queue_depth", "queue_dlq_total", "queue_retry_total"):
        assert name in text, f"{name} 未出现在 /metrics 导出内容中"


def test_metrics_content_type_is_prometheus_text():
    assert CONTENT_TYPE_LATEST.startswith("text/plain")
