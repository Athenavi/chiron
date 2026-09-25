# Prometheus 指标定义
from __future__ import annotations

import os

import psutil
from prometheus_client import Counter, Gauge, Histogram, Info

# ── HTTP 请求级 ──
HTTP_REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration",
    ["method", "path"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

# ── LLM Gateway 级 ──
LLM_REQUESTS = Counter(
    "llm_requests_total",
    "Total LLM requests",
    ["provider", "model", "status"],
)
LLM_REQUEST_DURATION = Histogram(
    "llm_request_duration_seconds",
    "LLM request duration",
    ["provider", "model"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)
LLM_TOKENS = Counter(
    "llm_tokens_total",
    "Total LLM tokens consumed",
    ["provider", "model", "direction"],
)
LLM_CACHE_HITS = Counter(
    "llm_cache_hits_total",
    "LLM cache hits",
    ["level"],  # l1 / l2 / l3
)
LLM_CACHE_MISSES = Counter("llm_cache_misses_total", "LLM cache misses")
LLM_CIRCUIT_STATE = Gauge(
    "llm_circuit_breaker_state",
    "Circuit breaker state (0=closed, 1=open)",
    ["provider"],
)

# ── 预算级 ──
TOKEN_BUDGET_USED = Gauge(
    "token_budget_used",
    "Token budget used this month",
    ["tenant_id"],
)
TOKEN_BUDGET_LIMIT = Gauge(
    "token_budget_limit",
    "Token budget monthly limit",
    ["tenant_id"],
)

# ── 队列级 ──
# ── MCP 插件连接池（B1）：多实例下的连接放大观测与预算 ──
MCP_POOL_CONNECTIONS = Gauge(
    "mcp_pool_connections", "MCP shared connections held by this engine instance"
)
MCP_POOL_USERS = Gauge(
    "mcp_pool_users", "Active users with MCP connections on this instance"
)
MCP_POOL_REJECTED = Counter(
    "mcp_pool_rejected_total",
    "MCP users/servers skipped because the instance connection budget was reached",
)

#: 限流降级计数（X1）：Redis 不可用时租户限流退回**进程内**实现 → 多副本下额度会放大 N 倍。
#: 这个数必须能被告警 —— 降级悄悄发生，就等于限流悄悄失效（"看起来有、实际没有"）。
#: 正常部署下它应恒为 0；一旦增长，说明 Redis 通路有问题，限流的全局保护已经名存实亡。
RATE_LIMIT_DEGRADED = Counter(
    "rate_limit_degraded_total",
    "Times tenant rate limiting fell back to the in-process limiter (Redis unavailable)",
)

#: 工作流入队失败转为"待执行"（X3）：入队失败不再静默本地跑（那种任务会随进程一起消失），
#: 而是标记 `queued_pending`、等启动时拉起。这个数应恒为 0 —— 一旦增长，说明 Redis/队列
#: 有过不可达，且有任务的完成时间被推迟到了下一次进程重启。
WORKFLOW_ENQUEUE_PENDING = Counter(
    "workflow_enqueue_pending_total",
    "Workflow instances that could not be enqueued and were marked queued_pending",
)

QUEUE_DEPTH = Gauge(
    "queue_depth",
    "Task queue depth",
    ["stream"],
)
QUEUE_PROCESSING_DURATION = Histogram(
    "queue_processing_duration_seconds",
    "Task processing duration",
    ["task_type"],
    buckets=[0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0],
)
QUEUE_DLQ_TOTAL = Counter(
    "queue_dlq_total",
    "Total tasks moved to dead letter queue",
    ["task_type"],
)
QUEUE_RETRY_TOTAL = Counter(
    "queue_retry_total",
    "Total tasks re-queued for retry",
    ["task_type"],
)

# ── 子 Agent 作业级（P1-3：状态转移的观测面）──
#
# 为什么需要：子 Agent 的终态写入点不止一处（正常收尾 / 取消收尾 / 超时定时器 /
# 僵尸收口器），历史上多次出现"某条路径忘了写终态"——DB 里留下 status='running'，
# 前端永久显示"运行中"，而**没有任何指标能反映它**（只能靠人去看 DB）。
# 这几项把"状态机是否收敛"变成可告警信号：
#
#   started - terminal{所有 status} ≈ 仍在运行的 run 数（本实例视角）
#
# 这些数只在**权威写入点**（subagent_runs 真写成功）递增，因此指标不会说谎：
# 指标涨了而 DB 没变，就说明持久化本身出了问题（见 SUBAGENT_PERSIST_FAILED）。
SUBAGENT_RUN_STARTED = Counter(
    "subagent_run_started_total",
    "Subagent runs that entered 'running' (counted only when the DB row was written)",
)
SUBAGENT_RUN_TERMINAL = Counter(
    "subagent_run_terminal_total",
    "Subagent runs that reached a terminal state",
    ["status"],  # completed / failed / cancelled / lost
)
SUBAGENT_RUN_UNFINALIZED = Gauge(
    "subagent_runs_unfinalized",
    "Runs this instance started but has not finalized (persistent growth means a "
    "finalize path is missing; see docs/subagent-interaction-redesign.md P1-3)",
)
SUBAGENT_PERSIST_FAILED = Counter(
    "subagent_persist_failed_total",
    "Subagent state persistence failures (run state may be lost permanently)",
    ["component", "op"],  # component: db / redis
)

# ── 实例级 ──
INSTANCE_ACTIVE_REQUESTS = Gauge(
    "instance_active_requests",
    "Active requests on this instance",
)
INSTANCE_UPTIME = Gauge(
    "instance_uptime_seconds",
    "Instance uptime in seconds",
)
ENGINE_INFO = Info("engine", "Python AI Engine info")

# ── 系统资源指标 ──
PROCESS_CPU_PERCENT = Gauge(
    "process_cpu_percent",
    "Current CPU usage percentage",
)
PROCESS_MEMORY_RSS = Gauge(
    "process_memory_rss_bytes",
    "Resident set size in bytes",
)
PROCESS_MEMORY_VMS = Gauge(
    "process_memory_vms_bytes",
    "Virtual memory size in bytes",
)
PROCESS_THREADS = Gauge(
    "process_threads",
    "Number of threads",
)


def record_process_metrics() -> None:
    """记录进程资源使用指标"""
    try:
        process = psutil.Process(os.getpid())
        PROCESS_CPU_PERCENT.set(process.cpu_percent(interval=None))
        mem_info = process.memory_info()
        PROCESS_MEMORY_RSS.set(mem_info.rss)
        PROCESS_MEMORY_VMS.set(mem_info.vms)
        PROCESS_THREADS.set(process.num_threads())
    except Exception:
        # 静默失败，避免影响主流程
        pass


def get_active_requests() -> int:
    """当前实例的在途 HTTP 请求数（由 MetricsMiddleware 维护）。"""
    try:
        return int(INSTANCE_ACTIVE_REQUESTS._value.get())
    except Exception:
        return 0


def get_process_resources() -> tuple[float, float]:
    """返回 (CPU 占用百分比, 常驻内存 MB)。

    ``cpu_percent(interval=None)`` 返回「自上次调用以来」的平均占用：首次调用因无基线
    返回 0.0，属预期行为。
    """
    try:
        process = psutil.Process(os.getpid())
        cpu = float(process.cpu_percent(interval=None))
        rss_mb = process.memory_info().rss / (1024 * 1024)
        return cpu, rss_mb
    except Exception:
        return 0.0, 0.0
